"""VT-471 — the Accounting specialist lane (v1 PREPARE-ONLY).

VT-604 Package 1 UPDATE (2026-07-05): this lane is NOT a roster specialist. The
verified Phase-1 runtime scope is exactly three specialists (sales_recovery /
integration / onboarding_conductor); this module's ``SPECIALIST_SPEC`` (bottom of
file) is no longer appended to ``agent/roster.py``'s ``ROSTER`` — there is no spawn
tool, no graph node, no route for ``accounting_lane``. A curated subset of its
``@tool`` functions is instead exposed DIRECTLY to the Manager as an advisory
capability — see ``agent/advisory_registry.py`` for the exact subset. This lane was
ALREADY prepare-only by charter (below); the only change is WHO calls the tool (the
Manager itself, not a spawned sub-graph).

The fourth of the six business-manager lanes (design §8, ratified 2026-06-29). The
manager hands an accounting-shaped {situation, desired_outcome, context_slice, data}
envelope HERE; this specialist owns the ACTION using accounting domain expertise. Its
v1 remit (charter):

  - bookkeeping / categorization   — organize the ledger + imported transactions into a
                                     categorized income/expense view.
  - GST + tax-summary PREPARATION  — read the verified GST status + the period's sales
                                     and PREPARE a tax-liability summary the owner files.
  - invoice / expense organization — summarize invoices/expenses for the period.
  - reconciliation                 — match imported bank/UPI transactions to the ledger
                                     and report matched / unmatched / discrepancies.

------------------------------------------------------------------------------
THE HARD RAIL — v1 PREPARES / SUMMARIZES, it does NOT file / submit / transact
------------------------------------------------------------------------------

This lane produces ADVISORY OUTPUT ONLY: categorized books, a tax summary, an
invoice/expense summary, a reconciliation report. It has NO tool that files a return,
submits to the GST/IT portal, moves money, raises a real invoice, pays a vendor, or
writes the owner's accounts book / ledger. The boundary is enforced two ways:

  1. CAPABILITY — every tool below is a pure READ + a SUMMARIZE/REPORT. There is no
     file/submit/transact/write tool on the surface to call.
  2. FAIL-CLOSED — ``assert_agent_tools_safe`` runs at build (VT-268): if a future change
     ever adds a send/ledger-write/spend/config-write/commitment tool to this lane, the
     graph build RAISES rather than silently opening the boundary. The companion
     ``tests/agent/test_accounting_lane.py`` + ``test_no_write_tool_surface.py`` pin the
     exact allowlist so a tool addition trips CI review.

This is a REGULATORY boundary, not a preference: filing/submitting a GST or income-tax
return requires explicit Fazal grant + regulatory authorization that does not exist (see
the FUTURE-GATED FILING SEAM comment below). v1 prepares the numbers; the owner (or their
CA) files them.

REUSE (no duplication, reuse-first): this lane is a thin COMPOSITION over the existing
accounting substrate — it builds no new store and no new write path:
  - ``customer_ledger_entries`` (migration 061) — the bookkeeping ledger (sale/payment).
  - ``imported_transactions`` (migration 062) — raw credit/debit txns (for categorization
    + reconciliation), read via the RLS-scoped ``tenant_connection``.
  - ``match_transactions`` (VT-275) — the DETERMINISTIC reconciliation matcher, REUSED as
    a read-only scorer (txn → ledger).
  - ``read_business_context`` (VT-466) — the GST status + verified identity, for the
    tax-summary preparation.

SHAPE — mirrors ``integration_agent.build_integration_agent`` / ``onboarding_conductor``
byte-for-byte (langchain ``create_agent`` sub-graph + Opus + ``cache_control`` per VT-194),
registered as a ``SpecialistSpec`` (exported as ``SPECIALIST_SPEC``) the coordinator appends
to ``agent/roster.py``'s ROSTER centrally — this module performs NO graph surgery and edits
no shared file. NO send/write/file tool (VT-268 ``assert_agent_tools_safe`` at build).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain.agents import AgentState, create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool, tool

from orchestrator.agent.lane_tenant import lane_tenant_error, resolve_lane_tenant
from orchestrator.llm.provider import resolve_chat_model
from orchestrator.types.trigger_reason import TriggerReason

logger = logging.getLogger("orchestrator.agent.accounting")

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "accounting_lane_system.md"
ACCOUNTING_LANE_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# VT-194 prompt caching — the cached prefix amortises the system prompt + tool inventory
# across dispatches (parity with orchestrator_agent / integration_agent / conductor).
ACCOUNTING_LANE_SYSTEM_MESSAGE = SystemMessage(
    content=[
        {
            "type": "text",
            "text": ACCOUNTING_LANE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
)

# VT-619b — the specialist model routes through the multi-provider seam. Tier "specialist"
# (default claude-sonnet-5, was opus-4-7) is env-driven via TEAM_MODEL_SPECIALIST so a claude-* ↔
# gpt-5.6-* swap is a Railway env change. max_tokens + sampling_kwargs now live inside the seam.
_MODEL: BaseChatModel = resolve_chat_model("specialist", agent="accounting_lane")


# ---------------------------------------------------------------------------
# Tools — every one is a pure READ + SUMMARIZE/REPORT. NO file/submit/transact/write tool
# exists on this surface (the v1 PREPARE-only rail). They compose over the EXISTING
# accounting substrate (ledger / imported_transactions / GST identity / the reconciliation
# matcher) — no new store, no new write path.
# ---------------------------------------------------------------------------


def _read_ledger_summary(tenant_id: UUID) -> dict[str, Any]:
    """RLS-scoped READ of ``customer_ledger_entries`` aggregated into a books view.

    Reuse-first: reads the canonical ledger (migration 061) directly through
    ``tenant_connection`` (the RLS seam) — sale vs payment totals + counts + the date span.
    Aggregates SERVER-SIDE (no per-customer PII to the LLM; CL-390) — the LLM sees counts +
    rupee totals, never a customer_id / phone / name.
    """
    from orchestrator.db.tenant_connection import tenant_connection

    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            """
            SELECT
                entry_type,
                COUNT(*)            AS n,
                COALESCE(SUM(amount_paise), 0) AS total_paise,
                MIN(entry_date)     AS first_date,
                MAX(entry_date)     AS last_date
            FROM customer_ledger_entries
            WHERE tenant_id = %s
            GROUP BY entry_type
            """,
            (str(tenant_id),),
        ).fetchall()
    by_type: dict[str, dict[str, Any]] = {}
    for r in row:
        et = r["entry_type"] if isinstance(r, dict) else r[0]
        n = r["n"] if isinstance(r, dict) else r[1]
        total = r["total_paise"] if isinstance(r, dict) else r[2]
        first = r["first_date"] if isinstance(r, dict) else r[3]
        last = r["last_date"] if isinstance(r, dict) else r[4]
        by_type[str(et)] = {
            "count": int(n),
            "total_paise": int(total),
            "total_inr": round(int(total) / 100.0, 2),
            "first_date": first.isoformat() if isinstance(first, date) else None,
            "last_date": last.isoformat() if isinstance(last, date) else None,
        }
    return by_type


@tool
def accounting_categorize_books(tenant_id: str) -> dict[str, Any]:
    """PREPARE a categorized books view for the owner — bookkeeping/categorization (READ-ONLY).

    REUSE: aggregates the canonical ``customer_ledger_entries`` (migration 061) into an
    income (``sale``) vs received (``payment``) view — counts, rupee totals, and the covered
    date span. Counts/totals ONLY (no customer PII reaches the LLM; CL-390).

    This ORGANIZES the books for the owner to review; it does NOT write/modify the ledger
    (the lane holds no ledger-write tool — VT-268). Returns
    ``{by_entry_type: {sale|payment: {count, total_inr, total_paise, first_date, last_date}},
    note}`` — a PREPARED summary, never a filed/finalized statement.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="accounting_categorize_books")
    if resolved is None:
        return lane_tenant_error("accounting_categorize_books")
    tenant_id = str(resolved)

    summary = _read_ledger_summary(UUID(tenant_id))
    logger.info(
        "accounting_categorize_books tenant=%s entry_types=%d (read-only summary)",
        tenant_id, len(summary),
    )
    return {
        "by_entry_type": summary,
        "note": "PREPARED categorized books for owner review — not a filed/finalized statement.",
    }


@tool
def accounting_prepare_tax_summary(tenant_id: str) -> dict[str, Any]:
    """PREPARE a GST / tax-liability SUMMARY for the owner to file — does NOT file/submit.

    REUSE: reads the tenant's VERIFIED GST status + identity via ``read_business_context``
    (VT-466) and the period sales total via the ledger aggregate. Produces an ESTIMATE the
    owner (or their CA) reviews and files: taxable turnover (sales), the GST status, and what
    is missing. NO filing, NO submission — there is deliberately no portal/submit tool here.

    Returns ``{gst_status, gst_verified, gstin_present, business_name, taxable_turnover_inr,
    period, note, next_step_for_owner}``. ``next_step_for_owner`` is advisory text — the
    owner FILES; this lane only PREPARES (the v1 PREPARE-only rail; filing is the FUTURE-gated
    seam below).
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="accounting_prepare_tax_summary")
    if resolved is None:
        return lane_tenant_error("accounting_prepare_tax_summary")
    tenant_id = str(resolved)

    from orchestrator.knowledge.business_context import read_business_context

    ctx = read_business_context(tenant_id)
    identity = ctx.identity or {}
    ledger = _read_ledger_summary(UUID(tenant_id))
    sale = ledger.get("sale", {})
    taxable_turnover_inr = sale.get("total_inr", 0.0)
    period = {
        "first_date": sale.get("first_date"),
        "last_date": sale.get("last_date"),
    }
    logger.info(
        "accounting_prepare_tax_summary tenant=%s gst_verified=%s (prepared estimate, not filed)",
        tenant_id, identity.get("gst_verified"),
    )
    return {
        "gst_status": identity.get("gst_status"),
        "gst_verified": bool(identity.get("gst_verified")),
        "gstin_present": bool(identity.get("gstin_present")),
        "business_name": identity.get("business_name"),
        "taxable_turnover_inr": taxable_turnover_inr,
        "period": period,
        "note": (
            "PREPARED tax summary (estimate from recorded sales) for the owner/CA to review "
            "and FILE — this lane does NOT file or submit any return."
        ),
        "next_step_for_owner": (
            "Review the taxable turnover above with your CA and file your GST return on the "
            "portal. Viabe prepared these numbers; it did not file them."
        ),
    }


@tool
def accounting_organize_invoices_expenses(tenant_id: str) -> dict[str, Any]:
    """ORGANIZE invoices/expenses for the period into a SUMMARY (READ-ONLY).

    REUSE: reads ``imported_transactions`` (migration 062) via the RLS-scoped
    ``tenant_connection`` and splits by ``direction`` — ``credit`` (money in / invoices) vs
    ``debit`` (money out / expenses) — into counts + rupee totals + date span. Aggregated
    server-side (counts/totals only; no PII to the LLM, CL-390).

    Advisory ORGANIZATION only: it does NOT raise a real invoice, pay an expense, or write
    anything (no such tool exists on this surface). Returns ``{credits_invoices, debits_expenses,
    note}`` — each ``{count, total_inr, total_paise, first_date, last_date}`` — a PREPARED view
    the owner reconciles, never a committed/transacted action.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="accounting_organize_invoices_expenses")
    if resolved is None:
        return lane_tenant_error("accounting_organize_invoices_expenses")
    tenant_id = str(resolved)

    from orchestrator.db.tenant_connection import tenant_connection

    with tenant_connection(UUID(tenant_id)) as conn:
        rows = conn.execute(
            """
            SELECT
                direction,
                COUNT(*)            AS n,
                COALESCE(SUM(amount_paise), 0) AS total_paise,
                MIN(txn_date)       AS first_date,
                MAX(txn_date)       AS last_date
            FROM imported_transactions
            WHERE tenant_id = %s
            GROUP BY direction
            """,
            (str(UUID(tenant_id)),),
        ).fetchall()
    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        direction = r["direction"] if isinstance(r, dict) else r[0]
        n = r["n"] if isinstance(r, dict) else r[1]
        total = r["total_paise"] if isinstance(r, dict) else r[2]
        first = r["first_date"] if isinstance(r, dict) else r[3]
        last = r["last_date"] if isinstance(r, dict) else r[4]
        buckets[str(direction)] = {
            "count": int(n),
            "total_paise": int(total),
            "total_inr": round(int(total) / 100.0, 2),
            "first_date": first.isoformat() if isinstance(first, date) else None,
            "last_date": last.isoformat() if isinstance(last, date) else None,
        }
    logger.info(
        "accounting_organize_invoices_expenses tenant=%s directions=%d (read-only summary)",
        tenant_id, len(buckets),
    )
    return {
        "credits_invoices": buckets.get("credit", {"count": 0, "total_inr": 0.0}),
        "debits_expenses": buckets.get("debit", {"count": 0, "total_inr": 0.0}),
        "note": (
            "PREPARED invoice/expense organization for owner review — this lane does not "
            "raise invoices, pay expenses, or write any record."
        ),
    }


@tool
def accounting_reconcile_transactions(tenant_id: str, lookback_days: int = 90) -> dict[str, Any]:
    """PREPARE a reconciliation REPORT — match imported txns to the ledger (READ-ONLY).

    REUSE: pulls the tenant's credit ``imported_transactions`` (the bank/UPI side) for the
    last ``lookback_days`` via the RLS-scoped ``tenant_connection``, then runs the DETERMINISTIC
    ``match_transactions`` (VT-275) matcher against the ledger — a pure read-only scorer. It
    REPORTS what matched, what is unmatched, and the discrepancies the owner should review.

    It does NOT "fix" the books, attribute, or write anything (the WRITE counterpart
    ``attribute_imported_transactions`` is deliberately NOT called — this lane reports the
    mismatches; the owner/ingestion path owns any correction). Returns
    ``{matched_count, unmatched_count, unmatched_reasons, lookback_days, note}`` — counts +
    reasons only (no PII / no raw rows to the LLM; CL-390).
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="accounting_reconcile_transactions")
    if resolved is None:
        return lane_tenant_error("accounting_reconcile_transactions")
    tenant_id = str(resolved)

    from datetime import timedelta

    from orchestrator.agent.tools.match_transactions import (
        MatchTransactionsInput,
        TransactionInput,
        match_transactions,
    )
    from orchestrator.db.tenant_connection import tenant_connection
    from orchestrator.graph import get_pool

    cutoff = date.today() - timedelta(days=max(1, lookback_days))
    with tenant_connection(UUID(tenant_id)) as conn:
        rows = conn.execute(
            """
            SELECT id::text AS id, amount_paise, txn_date
            FROM imported_transactions
            WHERE tenant_id = %s
              AND direction = 'credit'
              AND txn_date >= %s
            ORDER BY txn_date DESC
            LIMIT 500
            """,
            (str(UUID(tenant_id)), cutoff),
        ).fetchall()

    txns: list[TransactionInput] = []
    for r in rows:
        txn_id = r["id"] if isinstance(r, dict) else r[0]
        amount = r["amount_paise"] if isinstance(r, dict) else r[1]
        txn_date = r["txn_date"] if isinstance(r, dict) else r[2]
        # The matcher windows on a timestamp; synthesise midnight of the txn DATE (parity
        # with match_transactions' own date→midnight handling of the canonical ledger).
        ts = datetime(txn_date.year, txn_date.month, txn_date.day)
        txns.append(
            TransactionInput(txn_id=str(txn_id), amount_paise=int(amount), timestamp=ts)
        )

    if not txns:
        logger.info("accounting_reconcile_transactions tenant=%s no credit txns in window", tenant_id)
        return {
            "matched_count": 0,
            "unmatched_count": 0,
            "unmatched_reasons": {},
            "lookback_days": lookback_days,
            "note": "No imported credit transactions in the window to reconcile.",
        }

    result = match_transactions(
        MatchTransactionsInput(tenant_id=str(UUID(tenant_id)), transactions=txns),
        pool=get_pool(),
    )
    reasons: dict[str, int] = {}
    for u in result.unmatched:
        reasons[u.reason] = reasons.get(u.reason, 0) + 1
    logger.info(
        "accounting_reconcile_transactions tenant=%s matched=%d unmatched=%d (read-only report)",
        tenant_id, len(result.matches), len(result.unmatched),
    )
    return {
        "matched_count": len(result.matches),
        "unmatched_count": len(result.unmatched),
        "unmatched_reasons": reasons,
        "lookback_days": lookback_days,
        "note": (
            "PREPARED reconciliation report for owner review — this lane reports mismatches; "
            "it does not attribute, correct, or write the books."
        ),
    }


@tool
def accounting_escalate_to_fazal(run_id: str, reason: str, owner_stuck_at: str) -> str:
    """Escalate to Fazal when the owner is stuck or asks for something out of the v1 rail.

    Last-resort. Use when the owner wants a return FILED / GST SUBMITTED (out of the v1
    PREPARE-only rail) and re-framing to a prepared summary did not satisfy them, or when the
    accounting data is too broken to summarize. Log + return ack.
    """
    logger.warning(
        "ACCOUNTING_ESCALATE run_id=%s reason=%s stuck_at=%s",
        run_id, reason, owner_stuck_at,
    )
    return f"[escalated] reason={reason}"


# === FUTURE-GATED FILING SEAM (architect-for, do NOT build now) =============
#
# The v1 lane PREPARES; it does not FILE / SUBMIT / TRANSACT. The future filing capability
# (file GST/income-tax returns, prepare a balance sheet, submit to the GST portal) is
# DELIBERATELY UNBUILT and is gated behind: an explicit Fazal grant + the regulatory
# authorization (GST suvidha provider / e-filing intermediary) that does not exist today.
#
# When that grant + auth land, the seam attaches HERE, WITHOUT reshaping this module:
#
#   1. Add a NEW guarded tool (e.g. ``accounting_file_gst_return``) that routes EVERY filing
#      side-effect through the deterministic business-impact gate
#      (``assert_or_gate_business_action`` + ``business_action_context``, VT-467) — exactly
#      like every other consequential effect. The brain MUST NOT hold a direct file/submit
#      tool; the filing must be an owner-approval-gated, threshold-checked, DECAYING-HITL
#      guarded tool (the same framework as the customer-send choke).
#   2. The tool's NAME must NOT collide with the VT-268 forbidden-capability substrings for
#      benign reasons — a real filing tool SHOULD be added to FORBIDDEN_CAPABILITY_SUBSTRINGS
#      (e.g. ``file_return`` / ``submit_gst`` / ``portal_submit``) so an UNGATED filing tool
#      can never reach this surface; the gated one is wired via the guarded-tool framework,
#      not handed to the agent directly.
#   3. Append it to ACCOUNTING_LANE_TOOLS below + update the allowlist test.
#
# Until ALL of that exists, this lane has NO path to file/submit/transact. The structure
# (a per-effect guarded tool + the gate) is the seam; the capability is absent by design.
# ===========================================================================


ACCOUNTING_LANE_TOOLS: list[BaseTool] = [
    accounting_categorize_books,
    accounting_prepare_tax_summary,
    accounting_organize_invoices_expenses,
    accounting_reconcile_transactions,
    accounting_escalate_to_fazal,
]


class AccountingLaneState(AgentState, total=False):
    """State schema for the accounting_lane sub-graph (mirrors IntegrationAgentState).

    Carries the run-identity fields into the sub-graph so a future handoff tool's
    ``InjectedState`` can read them (parity with the integration / conductor agents; the
    current tool set keys on ``tenant_id`` passed as a tool arg).
    """

    run_id: UUID | None
    tenant_id: UUID | None
    trigger_reason: TriggerReason | None


def build_accounting_lane_agent(
    model: BaseChatModel = _MODEL,
    *,
    extra_tools: Sequence[BaseTool] = (),
) -> Any:
    """Build the Accounting specialist sub-graph (mirrors ``build_integration_agent``).

    VT-268 fail-CLOSED guardrail: the accounting lane must never hold a direct send /
    accounts-book-write / ledger-write / spend / commitment / config-write tool (raises at
    build if it does). It PREPARES/SUMMARIZES — the side-effecting filing/submit/transact
    capability is the FUTURE-gated seam above (unbuilt), so v1 carries no such tool.
    """
    tools = [*ACCOUNTING_LANE_TOOLS, *extra_tools]
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    assert_agent_tools_safe(tools, surface="accounting_lane")
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=ACCOUNTING_LANE_SYSTEM_MESSAGE,
        name="accounting_lane",
        state_schema=AccountingLaneState,
    )


def _build_accounting_lane_node(model: Any) -> Any:
    """Roster ``node_builder`` adapter — returns the accounting_lane sub-graph.

    Mirrors ``roster._build_integration_node`` / ``_build_onboarding_conductor_node``:
    a CompiledStateGraph sub-graph (``wrap_node=False`` in the spec — a compiled sub-graph
    must NOT be function-wrapped, VT-183 / VT-206). The coordinator imports THIS to fill the
    spec's ``node_builder``.
    """
    return build_accounting_lane_agent(model=model)


accounting_lane = build_accounting_lane_agent(_MODEL)


# === SPECIALIST_SPEC — the declarative roster registration (coordinator appends centrally) ==
#
# Exported, NOT self-registered. This module performs NO graph surgery and edits no shared
# file (roster.py / supervisor.py / routing.py): the coordinator imports SPECIALIST_SPEC and
# appends it to roster.ROSTER. update_builder=None — the lane self-fetches via its tools
# (keyed on tenant_id), exactly like the integration / onboarding_conductor lanes; the
# generic build_handoff_update still composes the standard {situation, outcome, slice, data}
# envelope around it. wrap_node=False (compiled sub-graph). edge_to=None → END (the lane
# emits an advisory summary, not a campaign plan to collapse).
def _make_specialist_spec() -> Any:
    """Build the ``SpecialistSpec`` lazily (defers the roster import to call time).

    Importing ``roster`` at module top would couple this disjoint lane to the shared roster
    module's import surface; building the spec on demand keeps the coupling to call time (the
    coordinator calls this once to register), so importing ``accounting_lane`` never drags in
    the roster.
    """
    from orchestrator.agent.roster import SpecialistSpec

    return SpecialistSpec(
        name="accounting",
        agent_name="accounting_lane",
        spawn_tool_name="spawn_accounting",
        route_key="spawn_accounting",
        node_builder=_build_accounting_lane_node,
        description=(
            "Hand off to the Accounting Specialist to PREPARE/SUMMARIZE the owner's books: "
            "categorized bookkeeping, a GST/tax-liability summary to file, invoice/expense "
            "organization, and a transaction reconciliation report. Use when the owner wants "
            "their accounts organized, a tax summary prepared, or transactions reconciled. "
            "v1 PREPARES/SUMMARIZES only — it does NOT file returns, submit GST, or transact."
        ),
        update_builder=None,  # lane self-fetches via its tools (keyed on tenant_id)
        prereq=None,
        edge_to=None,  # END — the lane emits an advisory summary, not a campaign plan.
        wrap_node=False,  # compiled sub-graph — must not be function-wrapped (VT-183/206)
        default_outcome="prepare and summarize the owner's accounting",
    )


SPECIALIST_SPEC: Any = _make_specialist_spec()


__all__ = [
    "ACCOUNTING_LANE_SYSTEM_MESSAGE",
    "ACCOUNTING_LANE_SYSTEM_PROMPT",
    "ACCOUNTING_LANE_TOOLS",
    "SPECIALIST_SPEC",
    "AccountingLaneState",
    "accounting_lane",
    "build_accounting_lane_agent",
]
