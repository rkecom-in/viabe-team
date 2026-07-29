"""VT-721 — the rolling 7-day plan store + the deterministic revision gate (S1, no LLM).

The plan object sits between the §7A monthly roadmap and the daily initiative pick: a durable
per-tenant 7-day action list, revised once a day as a NEW chained row (append-only, mirroring
manager_asserted_facts). Every action carries the §0.1d hand-off triple (objective, directive,
inputs) plus who executes it; every revision carries WHY-notes per change.

PLAN IS NOT EFFECT (ARCHITECTURE §0.1.1): nothing in this module schedules, sends, or spends.
``gate_revision`` is the deterministic floor every proposed revision passes BEFORE persisting —
it can only NORMALIZE and REJECT, never authorize: a money/send-class action is stamped
``requires_approval=True`` unconditionally (the planner cannot pre-authorize an effect), the
action list is capped, and unknown statuses/sources are rejected. The LLM revision pass (S2)
composes proposals; this gate decides what a plan row may contain.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

#: Longest action list a 7-day small-business plan may carry (design note §3.3).
MAX_ACTIONS = 10

_ACTION_STATUSES = frozenset({"planned", "in_flight", "done", "dropped"})
_ACTION_SOURCES = frozenset({"roadmap_item", "reactive", "carryover"})
_CHANGE_KINDS = frozenset({"keep", "drop", "resequence", "add", "amend"})

#: Action classes whose EXECUTION touches an effect gate — the plan may schedule them, but the
#: gate stamps requires_approval unconditionally (§0.1.1: plan-approval never carries an effect
#: past a gate). Matched against the action's ``action_class`` field (finite enum, not prose).
EFFECT_ACTION_CLASSES = frozenset(
    {"customer_message", "campaign_send", "spend", "commitment", "settings_change"}
)


@dataclass(frozen=True)
class WeekPlan:
    tenant_id: UUID
    plan_date: date
    horizon_start: date
    horizon_end: date
    actions: list[dict[str, Any]]
    revision_notes: list[dict[str, Any]]
    generated_by: str = "manager"
    model_id: str | None = None
    prev_plan_id: UUID | None = None
    plan_id: UUID | None = None
    created_at: Any = None


class RevisionRejected(ValueError):
    """A proposed revision violated the deterministic floor (shape/cap/enum). The caller keeps
    yesterday's plan; rejection is loud in logs, never silent."""


def gate_revision(actions: list[dict[str, Any]], notes: list[dict[str, Any]]) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]]
]:
    """The deterministic floor for a proposed revision. Returns (normalized_actions, notes).

    Enforces, in order:
      1. CAP — at most ``MAX_ACTIONS`` actions (reject, don't truncate: silent truncation would
         hide the planner over-promising).
      2. SHAPE — every action carries non-empty ``key``, ``objective``, ``directive`` and an
         ``assigned_to``; status/source must be known enums (default planned/reactive).
      3. §0.1.1 — an action whose ``action_class`` is effect-touching gets
         ``requires_approval=True`` stamped UNCONDITIONALLY, overwriting whatever the proposal
         said. The gate can only ADD the approval requirement, never remove one: a proposal
         carrying requires_approval=True keeps it regardless of class.
      4. NOTES — each note names a known change kind and a non-empty reason.
    Raises ``RevisionRejected`` on any violation it cannot normalize.
    """
    if len(actions) > MAX_ACTIONS:
        raise RevisionRejected(f"{len(actions)} actions exceeds the {MAX_ACTIONS}-action cap")
    seen_keys: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for a in actions:
        key = str(a.get("key") or "").strip()
        objective = str(a.get("objective") or "").strip()
        directive = str(a.get("directive") or "").strip()
        assigned = str(a.get("assigned_to") or "").strip()
        if not key or not objective or not directive or not assigned:
            raise RevisionRejected(f"action missing key/objective/directive/assigned_to: {a!r}")
        if key in seen_keys:
            raise RevisionRejected(f"duplicate action key {key!r}")
        seen_keys.add(key)
        status = str(a.get("status") or "planned")
        source = str(a.get("source") or "reactive")
        if status not in _ACTION_STATUSES:
            raise RevisionRejected(f"unknown action status {status!r}")
        if source not in _ACTION_SOURCES:
            raise RevisionRejected(f"unknown action source {source!r}")
        out = {
            "key": key,
            "objective": objective,
            "directive": directive,
            "inputs": a.get("inputs") or {},
            "assigned_to": assigned,
            "expected_outcome": str(a.get("expected_outcome") or "").strip(),
            "action_class": str(a.get("action_class") or "").strip() or None,
            "status": status,
            "source": source,
            # §0.1.1: sticky-true — the gate may only ADD the requirement, never clear it.
            "requires_approval": bool(a.get("requires_approval"))
            or (str(a.get("action_class") or "") in EFFECT_ACTION_CLASSES),
        }
        normalized.append(out)
    clean_notes: list[dict[str, Any]] = []
    for n in notes:
        change = str(n.get("change") or "").strip()
        reason = str(n.get("reason") or "").strip()
        if change not in _CHANGE_KINDS:
            raise RevisionRejected(f"unknown change kind {change!r}")
        if not reason:
            raise RevisionRejected(f"revision note without a reason: {n!r}")
        clean_notes.append(
            {"action_key": str(n.get("action_key") or "").strip(), "change": change, "reason": reason}
        )
    return normalized, clean_notes


def write_revision(
    tenant_id: UUID | str,
    actions: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    *,
    plan_date: date | None = None,
    generated_by: str = "manager",
    model_id: str | None = None,
) -> UUID | None:
    """Gate + persist one daily revision (chained to the latest prior row). Returns the new
    plan id, or None when today's revision already exists (idempotent — the DB uniq is the
    backstop) or on a gated rejection (logged loud; yesterday's plan stands)."""
    from orchestrator.db import tenant_connection

    today = plan_date or date.today()
    try:
        norm_actions, norm_notes = gate_revision(actions, notes)
    except RevisionRejected as exc:
        logger.warning("week_plan: revision REJECTED tenant=%s: %s", tenant_id, exc)
        return None
    try:
        prior = latest_plan(tenant_id)
        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "INSERT INTO tenant_week_plans "
                "(tenant_id, plan_date, horizon_start, horizon_end, actions, revision_notes, "
                " generated_by, model_id, prev_plan_id) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s) "
                "ON CONFLICT (tenant_id, plan_date) DO NOTHING RETURNING id",
                (
                    str(tenant_id), today, today, today + timedelta(days=6),
                    json.dumps(norm_actions), json.dumps(norm_notes),
                    generated_by, model_id,
                    str(prior.plan_id) if prior and prior.plan_id else None,
                ),
            ).fetchone()
        if row is None:
            return None  # today's revision already exists
        return row["id"] if isinstance(row, dict) else row[0]
    except Exception:  # noqa: BLE001 — a plan-write failure never breaks the daily fire
        logger.warning("week_plan: revision write failed (fail-soft) tenant=%s", tenant_id, exc_info=True)
        return None


def latest_plan(tenant_id: UUID | str) -> WeekPlan | None:
    """The newest revision for a tenant, or None. Fail-soft → None."""
    from orchestrator.db import tenant_connection

    try:
        with tenant_connection(tenant_id) as conn:
            r = conn.execute(
                "SELECT id, plan_date, horizon_start, horizon_end, actions, revision_notes, "
                "       generated_by, model_id, prev_plan_id, created_at "
                "FROM tenant_week_plans WHERE tenant_id = %s "
                "ORDER BY plan_date DESC LIMIT 1",
                (str(tenant_id),),
            ).fetchone()
        if r is None:
            return None
        get = (lambda k, i: r[k]) if isinstance(r, dict) else (lambda k, i: r[i])
        return WeekPlan(
            tenant_id=UUID(str(tenant_id)),
            plan_id=get("id", 0),
            plan_date=get("plan_date", 1),
            horizon_start=get("horizon_start", 2),
            horizon_end=get("horizon_end", 3),
            actions=list(get("actions", 4) or []),
            revision_notes=list(get("revision_notes", 5) or []),
            generated_by=get("generated_by", 6),
            model_id=get("model_id", 7),
            prev_plan_id=get("prev_plan_id", 8),
            created_at=get("created_at", 9),
        )
    except Exception:  # noqa: BLE001 — advisory read, never a gate
        logger.warning("week_plan: latest read failed (fail-soft) tenant=%s", tenant_id, exc_info=True)
        return None


__all__ = [
    "EFFECT_ACTION_CLASSES",
    "MAX_ACTIONS",
    "RevisionRejected",
    "WeekPlan",
    "gate_revision",
    "latest_plan",
    "write_revision",
]
