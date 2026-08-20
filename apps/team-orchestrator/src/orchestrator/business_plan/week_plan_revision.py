"""VT-721 S2 — the daily 7-day-plan REVISION pass (LLM proposes; the S1 gate decides).

Runs inside the existing §7A daily fire, per tenant, behind ``TEAM_WEEK_PLAN``
(off = byte-identical today; shadow = write revision rows, alter nothing else; active = S3
additionally lets the daily pick read the plan). One LLM call per tenant per day:

  collect (deterministic)  →  propose (LLM, strict JSON)  →  gate (S1, deterministic)  →  write

COLLECT gathers only real substrate: yesterday's plan row, the accepted roadmap items (the
monthly ground truth), the last 24h of manager_tasks terminal outcomes, and the asserted-facts
ledger (a plan told to the owner is a commitment — flips must be owned, VT-719). The prompt
forbids inventing actions outside that substrate. PLAN IS NOT EFFECT: whatever the model says,
``gate_revision`` stamps sticky-true approval on every effect class (§0.1.1) and rejects
over-cap or malformed output — a rejected proposal keeps yesterday's plan (loud, never silent).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# 4096: a full 10-action revision + notes overruns 2048 and truncates mid-JSON (canary day-2
# finding — day 1's short first plan parsed, every later revision failed).
_MAX_OUTPUT_TOKENS = 4096
_LLM_TIMEOUT_SECONDS = 90.0


def week_plan_mode() -> str:
    """``off`` (default) | ``shadow`` | ``active`` — the VT-721 rollout flag."""
    v = os.environ.get("TEAM_WEEK_PLAN", "").strip().lower()
    return v if v in {"shadow", "active"} else "off"


# --- collect (deterministic) ---------------------------------------------------------------


def _recent_outcomes(tenant_id: UUID | str, *, hours: int = 24) -> list[dict[str, Any]]:
    """Terminal manager_tasks outcomes since the last cycle — the 'previous results' leg of the
    COO mandate. Fail-soft → []."""
    from orchestrator.db import tenant_connection

    try:
        with tenant_connection(tenant_id) as conn:
            rows = conn.execute(
                "SELECT objective, status, terminal_outcome FROM manager_tasks "
                "WHERE tenant_id = %s AND status IN ('completed', 'failed', 'cancelled') "
                "AND updated_at > now() - %s::interval ORDER BY updated_at DESC LIMIT 20",
                (str(tenant_id), f"{int(hours)} hours"),
            ).fetchall()
        out = []
        for r in rows:
            obj, status, outcome = (
                (r["objective"], r["status"], r.get("terminal_outcome"))
                if isinstance(r, dict) else (r[0], r[1], r[2])
            )
            # objective is jsonb — a string for triage-minted tasks, an object for planner-shaped
            # ones; render either to a compact text head.
            text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
            out.append({"task": (text or "")[:160], "status": status, "outcome": outcome})
        return out
    except Exception:  # noqa: BLE001
        logger.warning("week_plan_revision: outcomes read failed (fail-soft) tenant=%s", tenant_id)
        return []


def _collect(tenant_id: UUID | str) -> dict[str, Any]:
    from orchestrator.business_plan.seams import items_for_agent
    from orchestrator.business_plan.store import OWNING_AGENTS
    from orchestrator.business_plan.week_plan import latest_plan
    from orchestrator.manager.asserted_facts import active_assertions

    items = []
    try:
        for agent in sorted(OWNING_AGENTS):
            for it in items_for_agent(tenant_id, agent, statuses=("accepted",)):
                items.append({
                    "item_id": it.item_id, "seq": it.seq, "objective": it.objective,
                    "owning_agent": it.owning_agent,
                })
        items.sort(key=lambda x: x["seq"])
    except Exception:  # noqa: BLE001
        logger.warning("week_plan_revision: roadmap read failed (fail-soft) tenant=%s", tenant_id)
    prior = latest_plan(tenant_id)
    return {
        "prior_actions": prior.actions if prior else [],
        "roadmap_items": items[:12],
        "outcomes_24h": _recent_outcomes(tenant_id),
        "commitments": active_assertions(tenant_id),
    }


# --- propose (LLM) -------------------------------------------------------------------------

_PROMPT = """You are the COO-Manager revising the next-7-day plan for one small Indian business.

GROUND TRUTH (the ONLY substrate — never invent actions outside it):
PRIOR PLAN ACTIONS (yesterday's plan; [] on the first day):
{prior}

ACCEPTED ROADMAP ITEMS (the monthly plan — 7-day actions should trace to these or to carryover):
{roadmap}

OUTCOMES SINCE YESTERDAY (what actually happened):
{outcomes}

COMMITMENTS ALREADY MADE TO THE OWNER (never plan anything contradicting these):
{commitments}

Revise the 7-day plan: keep what is working, drop what outcomes killed, carry over what is
unfinished, add ONLY what a roadmap item or an outcome justifies. Max {cap} actions. Every
action needs: key (short slug), objective, directive (the instruction you would hand the
specialist), inputs (object), assigned_to (a specialist/tool name), expected_outcome, source
("roadmap_item"|"reactive"|"carryover"), action_class when it touches customers/money
("customer_message"|"campaign_send"|"spend"|"commitment"|"settings_change") else null.
Every change needs a note: {{"action_key", "change": "keep"|"drop"|"resequence"|"add"|"amend",
"reason"}} — the reason must cite the outcome/roadmap fact that justifies it.

Keep every string SHORT (directive <= 2 sentences, reason <= 1). Prefer FEWER, higher-value
actions over a full list. Reply with STRICT JSON only: {{"actions": [...], "notes": [...]}}"""


def _call_llm(prompt: str, model: str) -> str:
    """One non-streaming call through the tier seam (VT-732 — same tier as the plan generator,
    whose ``_resolve_plan_model`` this module already borrows). ``model`` stays in the signature
    for the injectable-``llm`` contract below; the tier decides which model runs."""
    from orchestrator.business_plan.generator import _PLAN_TIER
    from orchestrator.llm.structured import structured_text_call

    return structured_text_call(
        _PLAN_TIER,
        user=prompt,
        max_tokens=_MAX_OUTPUT_TOKENS,
        agent="business_plan",
        call_site="week_plan_revision",
        timeout_s=_LLM_TIMEOUT_SECONDS,
    ).strip()


def _parse(raw: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Strict-JSON parse with the house prose-wrap tolerance (first { .. last })."""
    text = raw.strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in revision reply")
        text = text[start : end + 1]
    doc = json.loads(text)
    return list(doc.get("actions") or []), list(doc.get("notes") or [])


# --- the pass ------------------------------------------------------------------------------


def revise_week_plan(
    tenant_id: UUID | str, *, now: datetime | None = None, llm: Any = None
) -> UUID | None:
    """One tenant's daily revision. Returns the new plan id, or None (flag off / already done
    today / LLM or gate rejection — yesterday's plan stands, loud in logs, never silent)."""
    if week_plan_mode() == "off":
        return None
    from orchestrator.business_plan.generator import _resolve_plan_model
    from orchestrator.business_plan.week_plan import latest_plan, write_revision

    today = (now or datetime.now(timezone.utc)) + timedelta(hours=5, minutes=30)  # IST day
    plan_date = today.date()
    prior = latest_plan(tenant_id)
    if prior is not None and prior.plan_date >= plan_date:
        return None  # today's revision already exists (cheap check; DB uniq is the backstop)
    substrate = _collect(tenant_id)
    from orchestrator.business_plan.week_plan import MAX_ACTIONS

    prompt = _PROMPT.format(
        prior=json.dumps(substrate["prior_actions"], ensure_ascii=False),
        roadmap=json.dumps(substrate["roadmap_items"], ensure_ascii=False),
        outcomes=json.dumps(substrate["outcomes_24h"], ensure_ascii=False),
        commitments=json.dumps(
            [{"fact": f.get("fact_key"), "value": f.get("fact_value")} for f in substrate["commitments"]],
            ensure_ascii=False, default=str,
        ),
        cap=MAX_ACTIONS,
    )
    call = llm or _call_llm
    model = _resolve_plan_model()
    try:
        actions, notes = _parse(call(prompt, model))
    except Exception:  # noqa: BLE001 — a failed proposal keeps yesterday's plan
        logger.warning("week_plan_revision: LLM/parse failed tenant=%s (plan unchanged)", tenant_id, exc_info=True)
        return None
    plan_id = write_revision(
        tenant_id, actions, notes, plan_date=plan_date, generated_by="manager", model_id=model
    )
    if plan_id is not None:
        # VT-721 × VT-719 (the Clau-named hole, closed): the plan is owner-visible (ask + console),
        # so each revision IS a commitment — record it in the asserted-facts ledger. The ledger's
        # append-only supersession makes every daily revision an OWNED change of yesterday's plan;
        # a brain reply stating a different plan trips contradiction_check. Fail-soft inside.
        try:
            from orchestrator.manager.asserted_facts import record_assertion

            gated = latest_plan(tenant_id)
            summary = [
                {"key": a.get("key"), "objective": a.get("objective"), "status": a.get("status")}
                for a in (gated.actions if gated else [])
            ]
            record_assertion(
                tenant_id, "week_plan",
                {"plan_date": str(plan_date), "actions": summary},
                statement_text=f"7-day plan revised {plan_date}: "
                + "; ".join(str(s["objective"]) for s in summary[:5]),
                surface="system",
                derived_from={"site": "week_plan_revision", "plan_id": str(plan_id)},
            )
        except Exception:  # noqa: BLE001 — the ledger never breaks the revision
            logger.warning("week_plan_revision: plan assertion failed (fail-soft) tenant=%s", tenant_id)
    return plan_id


__all__ = ["revise_week_plan", "week_plan_mode"]
