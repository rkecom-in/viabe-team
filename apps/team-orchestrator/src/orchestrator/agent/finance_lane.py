"""VT-470 — the Finance specialist lane (ADVISORY ALWAYS).

The Finance lane the Team-Manager hands a finance OUTCOME to (design §7/§8). Per the
ratified charter (design §8, VT-470) Finance is **ADVISORY — and stays advisory even in
future scope**: cash-flow analysis, receivables/payables, margin/pricing input, and
loss/debt identification. It SUGGESTS money movement and IDENTIFIES losses/debt; it
**NEVER MOVES MONEY** — there is no money-movement tool in this module, not even a gated
one. Payment reminders ARE customer sends, so they route through the EXISTING send rail
(``agents/customer_send.agent_send_draft`` — consent/caps + the VT-474 decaying
checkpoint); this lane only PROPOSES the reminder draft, it never sends and never
persists a draft itself.

SHAPE — mirrors ``onboarding_conductor.build_onboarding_conductor_agent`` /
``integration_agent.build_integration_agent`` byte-for-byte (langchain ``create_agent``
sub-graph + Opus + ``cache_control`` per VT-194), and exports a ``SpecialistSpec``
(``SPECIALIST_SPEC``) the coordinator registers centrally on the roster spine (VT-465).
This module edits ONLY itself + its prompt + its tests — it does NOT touch ``roster.py``,
``handoffs.py``, ``routing.py``, ``supervisor.py``, ``activation_registry.py`` (the
coordinator/VT-474 own the central wiring + rail internals).

DIVISION OF INTELLIGENCE (design §7, 211500Z): the manager reads the SITUATION + decides
the OUTCOME; THIS specialist owns the ACTION — and the Finance lane's "action" is the
ADVICE / analysis it produces from {situation, outcome, context_slice, data}, plus
(optionally) a payment-reminder PROPOSAL. The handoff is TWO-WAY: if the desired outcome
is infeasible/unwise in-lane (e.g. "collect receivables" with none overdue), the
specialist PUSHES BACK and proposes a better outcome (``finance_pushback``) rather than
fabricating an action.

REUSE, no duplication (the data layer already exists — do NOT rebuild it):
  - ``customer_ledger_entries`` (entry_type 'sale'/'payment', amount_paise, entry_date,
    customer_id) + ``imported_transactions`` (direction 'credit'/'debit') are the
    cash-flow / receivables substrate. Read directly via ``tenant_connection`` (RLS) —
    these are NOT ``no-direct-tenant-db-access`` watched hot tables (same allowlisted
    pattern ``context_builder._build_ledger_summary`` uses; ``customers`` IS watched and
    is read ONLY through ``db.wrappers.CustomersWrapper``).
  - ``db.wrappers.CustomersWrapper`` — ``top_customers_by_spend`` / ``count_all`` for the
    customer-side aggregates (the sanctioned wrapper for the ``customers`` hot table).

VT-268 fail-CLOSED guardrail (``assert_agent_tools_safe`` at build): the Finance agent
holds NO direct send tool, NO ledger/accounts-book-write tool, and NO money-movement /
spend tool — it advises; the deterministic rails own every side-effect.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain.agents import AgentState, create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool, tool

from orchestrator.db import tenant_connection
from orchestrator.types.trigger_reason import TriggerReason

logger = logging.getLogger("orchestrator.agent.finance")

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "finance_lane_system.md"
FINANCE_LANE_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# VT-194 prompt caching — cached prefix amortises the system prompt + tool inventory
# across dispatches (parity with orchestrator_agent / integration_agent / conductor).
FINANCE_LANE_SYSTEM_MESSAGE = SystemMessage(
    content=[
        {
            "type": "text",
            "text": FINANCE_LANE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
)

# mypy --strict needs the call-arg ignore for ChatAnthropic's pydantic kwargs (parity
# with the orchestrator / integration / conductor agents).
_MODEL = ChatAnthropic(model="claude-opus-4-7", max_tokens=4096)  # type: ignore[call-arg]


def _col(row: Any, key: str, idx: int) -> Any:
    """Read a column from a psycopg row that may be a dict or a tuple."""
    if row is None:
        return None
    return row[key] if isinstance(row, dict) else row[idx]


# -----------------------------------------------------------------
# Tools — READ-ONLY finance analysis (delegating to the EXISTING ledger/transaction
# substrate) + a reminder-draft PROPOSAL + the two-way pushback/escalate seams.
#
# NO send tool. NO ledger/accounts-book-write tool. NO money-movement / spend tool.
# Every tool here either READS (aggregate, PII-free) or RETURNS A PROPOSAL — none takes
# a side-effecting action. The side-effects (persist a draft, run the gated send) are
# the deterministic rails' job, never an agent tool (VT-268 / VT-460 / VT-474).
# -----------------------------------------------------------------


@tool
def analyze_cash_flow(tenant_id: str) -> dict[str, Any]:
    """Cash-flow summary for the tenant — inflow vs outflow + a net/trend signal (ADVISORY).

    REUSE: reads the EXISTING ``customer_ledger_entries`` (entry_type 'sale'/'payment')
    + ``imported_transactions`` (direction 'credit'/'debit') substrate directly via the
    RLS-scoped ``tenant_connection`` — the same allowlisted direct-read pattern
    ``context_builder._build_ledger_summary`` uses (these are NOT watched hot tables).

    Aggregate-only, NO customer PII (CL-390): returns totals/counts the specialist
    reasons over, never raw rows. ``inflow_paise`` = recorded sales; ``collected_paise``
    = recorded payments; ``outstanding_paise`` = sales not yet matched by a payment
    (the receivable signal). ``credit_paise`` / ``debit_paise`` characterise imported
    transaction direction (the cash-movement signal). Best-effort: a read miss / absent
    table degrades to zeros (the specialist advises on what data exists, never crashes).
    """
    tid = str(UUID(tenant_id))
    out: dict[str, Any] = {
        "inflow_paise": 0,
        "collected_paise": 0,
        "outstanding_paise": 0,
        "sale_count": 0,
        "payment_count": 0,
        "credit_paise": 0,
        "debit_paise": 0,
        "window_days": 90,
    }
    try:
        with tenant_connection(tid) as conn:
            ledger = conn.execute(
                "SELECT "
                "  COALESCE(SUM(amount_paise) FILTER (WHERE entry_type = 'sale'), 0) AS inflow, "
                "  COALESCE(SUM(amount_paise) FILTER (WHERE entry_type = 'payment'), 0) AS collected, "
                "  COUNT(*) FILTER (WHERE entry_type = 'sale') AS sale_count, "
                "  COUNT(*) FILTER (WHERE entry_type = 'payment') AS payment_count "
                "FROM customer_ledger_entries WHERE tenant_id = %s "
                "  AND entry_date >= CURRENT_DATE - INTERVAL '90 days'",
                (tid,),
            ).fetchone()
            out["inflow_paise"] = int(_col(ledger, "inflow", 0) or 0)
            out["collected_paise"] = int(_col(ledger, "collected", 1) or 0)
            out["sale_count"] = int(_col(ledger, "sale_count", 2) or 0)
            out["payment_count"] = int(_col(ledger, "payment_count", 3) or 0)
            out["outstanding_paise"] = max(out["inflow_paise"] - out["collected_paise"], 0)
            try:
                txn = conn.execute(
                    "SELECT "
                    "  COALESCE(SUM(amount_paise) FILTER (WHERE direction = 'credit'), 0) AS credit, "
                    "  COALESCE(SUM(amount_paise) FILTER (WHERE direction = 'debit'), 0) AS debit "
                    "FROM imported_transactions WHERE tenant_id = %s "
                    "  AND txn_date >= CURRENT_DATE - INTERVAL '90 days'",
                    (tid,),
                ).fetchone()
                out["credit_paise"] = int(_col(txn, "credit", 0) or 0)
                out["debit_paise"] = int(_col(txn, "debit", 1) or 0)
            except Exception:  # noqa: BLE001 — imported_transactions may be absent; ledger still answers
                logger.debug("analyze_cash_flow: imported_transactions read skipped tenant=%s", tid)
    except Exception:  # noqa: BLE001 — advisory read is best-effort; degrade to zeros
        logger.warning("analyze_cash_flow: read failed tenant=%s (advisory zeros returned)", tid)
    out["net_paise"] = out["credit_paise"] - out["debit_paise"]
    logger.info(
        "finance.analyze_cash_flow tenant=%s sales=%d payments=%d outstanding_paise=%d",
        tid, out["sale_count"], out["payment_count"], out["outstanding_paise"],
    )
    return out


@tool
def analyze_receivables(tenant_id: str) -> dict[str, Any]:
    """Outstanding-receivables identification — money owed TO the business (ADVISORY).

    REUSE: per-customer SALE-vs-PAYMENT aggregation over the EXISTING
    ``customer_ledger_entries`` substrate via RLS-scoped ``tenant_connection``. A
    customer's receivable = (their recorded sales) − (their recorded payments); a
    positive balance with an aged last sale is the overdue signal the specialist
    advises a reminder on.

    Aggregate + customer-id only, NO raw PII (CL-390): returns counts, total
    outstanding paise, and the top overdue ``customer_id``s (UUID strings — never
    phone/email/name; the manager/rail resolve identity downstream). The specialist
    proposes a reminder for an overdue ``customer_id`` via ``propose_payment_reminder``;
    it never sends. Best-effort: a read miss degrades to an empty/zero result.
    """
    tid = str(UUID(tenant_id))
    out: dict[str, Any] = {
        "overdue_count": 0,
        "total_outstanding_paise": 0,
        "overdue_customer_ids": [],
        "overdue_threshold_days": 30,
    }
    try:
        with tenant_connection(tid) as conn:
            rows = conn.execute(
                "WITH per_customer AS ("
                "  SELECT customer_id, "
                "    COALESCE(SUM(amount_paise) FILTER (WHERE entry_type = 'sale'), 0) "
                "      - COALESCE(SUM(amount_paise) FILTER (WHERE entry_type = 'payment'), 0) "
                "      AS outstanding_paise, "
                "    MAX(entry_date) FILTER (WHERE entry_type = 'sale') AS last_sale_date "
                "  FROM customer_ledger_entries WHERE tenant_id = %s "
                "  GROUP BY customer_id) "
                "SELECT customer_id, outstanding_paise, "
                "  (CURRENT_DATE - last_sale_date) AS days_since_last_sale "
                "FROM per_customer "
                "WHERE outstanding_paise > 0 "
                "  AND last_sale_date IS NOT NULL "
                "  AND (CURRENT_DATE - last_sale_date) >= 30 "
                "ORDER BY outstanding_paise DESC "
                "LIMIT 50",
                (tid,),
            ).fetchall()
            ids: list[str] = []
            total = 0
            for r in rows:
                cid = _col(r, "customer_id", 0)
                amt = int(_col(r, "outstanding_paise", 1) or 0)
                total += amt
                ids.append(str(cid))
            out["overdue_count"] = len(ids)
            out["total_outstanding_paise"] = total
            out["overdue_customer_ids"] = ids
    except Exception:  # noqa: BLE001 — advisory read is best-effort; degrade to empty
        logger.warning("analyze_receivables: read failed tenant=%s (empty result returned)", tid)
    logger.info(
        "finance.analyze_receivables tenant=%s overdue=%d outstanding_paise=%d",
        tid, out["overdue_count"], out["total_outstanding_paise"],
    )
    return out


@tool
def pricing_margin_input(tenant_id: str) -> dict[str, Any]:
    """Margin / pricing SIGNALS the data supports — input for a pricing SUGGESTION (ADVISORY).

    REUSE: ``db.wrappers.CustomersWrapper`` (the sanctioned wrapper for the ``customers``
    watched hot table) — ``count_all`` + ``top_customers_by_spend`` give the customer-base
    size + the top-line spend distribution. The specialist reasons a pricing/margin
    SUGGESTION from these; it does NOT set prices and does NOT move money.

    Aggregate-only, NO raw PII (CL-390): top customers are returned as ``customer_id`` +
    ``spend_paise`` ONLY (the wrapper's display_name/phone are dropped here — they never
    enter the specialist's reasoning). Best-effort: a read miss degrades to zeros.
    """
    from orchestrator.db.wrappers import CustomersWrapper

    out: dict[str, Any] = {"customer_count": 0, "top_spend": []}
    try:
        wrapper = CustomersWrapper()
        out["customer_count"] = wrapper.count_all(tenant_id)
        top = wrapper.top_customers_by_spend(tenant_id, limit=10)
        # PII-safe projection: customer_id + spend_paise only (drop display_name/phone).
        out["top_spend"] = [
            {"customer_id": str(r.get("id")), "spend_paise": int(r.get("spend_paise") or 0)}
            for r in top
        ]
    except Exception:  # noqa: BLE001 — advisory read is best-effort; degrade to zeros
        logger.warning("pricing_margin_input: read failed tenant=%s (zeros returned)", tenant_id)
    logger.info(
        "finance.pricing_margin_input tenant=%s customers=%d top=%d",
        tenant_id, out["customer_count"], len(out["top_spend"]),
    )
    return out


@tool
def propose_payment_reminder(
    tenant_id: str, customer_id: str, reason: str, reminder_text: str
) -> dict[str, Any]:
    """PROPOSE a payment-reminder draft for an overdue receivable — does NOT send/persist.

    A payment reminder IS a customer send, so it is governed by the EXISTING deterministic
    send rail (``agents/customer_send.agent_send_draft``): consent allowlist + opt-out
    re-read, send caps/suppression, the onboarded-gate, the WABA-live gate, and the
    VT-474 SEND DECAYING CHECKPOINT. This tool does NONE of that — it RETURNS A STRUCTURED
    PROPOSAL (which customer, why, the reminder text) back to the manager. The manager/rail
    own persisting an ``agent_drafts`` row and running the gated send; the Finance
    specialist holds NO send tool and NO draft-write tool (VT-268), exactly like
    Sales-Recovery drafts and the choke point that sends them.

    Use ONLY for a genuinely-overdue receivable surfaced by ``analyze_receivables`` (a
    ``customer_id`` from its ``overdue_customer_ids``). The proposal is advisory output:
    nothing reaches a customer until the rail sends it, gated.
    """
    proposal = {
        "kind": "payment_reminder_proposal",
        "tenant_id": str(UUID(tenant_id)),
        "customer_id": str(UUID(customer_id)),
        "reason": reason,
        "reminder_text": reminder_text,
        # Explicit, machine-readable: this is a PROPOSAL, not a send. The rail decides.
        "routes_through": "agents.customer_send.agent_send_draft",
        "sent": False,
        "persisted": False,
    }
    logger.info(
        "finance.propose_payment_reminder tenant=%s customer=%s (PROPOSAL — not sent/persisted)",
        proposal["tenant_id"], proposal["customer_id"],
    )
    return proposal


@tool
def finance_pushback(
    desired_outcome: str, reason: str, proposed_outcome: str
) -> dict[str, Any]:
    """Two-way handoff PUSHBACK (design §7) — refuse an infeasible/unwise finance outcome.

    When the manager's ``desired_outcome`` is infeasible or unwise in-lane (e.g. "collect
    receivables" with none overdue, or a pricing move the margin data contradicts), the
    specialist does NOT fabricate an action — it pushes back with the ``reason`` and a
    better ``proposed_outcome``. The manager re-frames or escalates. Returns the structured
    pushback envelope; takes no side effect.
    """
    logger.info("finance.pushback desired=%r -> proposed=%r", desired_outcome, proposed_outcome)
    env = {
        "pushback": True,
        "desired_outcome": desired_outcome,
        "reason": reason,
        "proposed_outcome": proposed_outcome,
    }
    # VT-549 (B3-wiring 2): run the manager decision loop on this REAL finance pushback + record the
    # decision to tm_audit (OBSERVE-ONLY — routing unchanged; same proven bridge as the sales lane).
    from orchestrator.agent.specialist_return import observe_specialist_return

    observe_specialist_return(env, agent="finance")
    return env


@tool
def finance_escalate_to_fazal(run_id: str, reason: str, context: str) -> str:
    """Escalate to Fazal — last-resort, EXTREME criteria only (design §6/§8 A3).

    Concrete deterministic triggers ONLY: an anomaly, an irreversible/high-stakes decision
    outside policy, or a money-movement request the advisory lane must REFUSE. WhatsApp-only,
    concise. Logs + returns an ack; takes no other action.
    """
    logger.warning(
        "FINANCE_ESCALATE run_id=%s reason=%s context=%s", run_id, reason, context
    )
    return f"[escalated] reason={reason}"


FINANCE_LANE_TOOLS: list[BaseTool] = [
    analyze_cash_flow,
    analyze_receivables,
    pricing_margin_input,
    propose_payment_reminder,
    finance_pushback,
    finance_escalate_to_fazal,
]


class FinanceLaneState(AgentState, total=False):
    """State schema for the finance_lane sub-graph (mirrors IntegrationAgentState /
    OnboardingConductorState). Carries the run-identity fields into the sub-graph so a
    future handoff tool's ``InjectedState`` can read them (parity with the other lanes;
    the current tool set keys on ``tenant_id`` passed as a tool arg)."""

    run_id: UUID | None
    tenant_id: UUID | None
    trigger_reason: TriggerReason | None


def build_finance_lane_agent(
    model: ChatAnthropic = _MODEL,
    *,
    extra_tools: Sequence[BaseTool] = (),
) -> Any:
    """Build the Finance ADVISORY specialist sub-graph (mirrors ``build_onboarding_conductor_agent``).

    VT-268 fail-CLOSED guardrail: the Finance agent must never hold a direct send /
    accounts-book-write / ledger-write / money-movement (spend/charge/pay/transfer) tool
    (raises at build if it does). Finance is ADVISORY — it READS the ledger and PROPOSES;
    the deterministic rails own every side-effect.
    """
    tools = [*FINANCE_LANE_TOOLS, *extra_tools]
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    assert_agent_tools_safe(tools, surface="finance_lane")
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=FINANCE_LANE_SYSTEM_MESSAGE,
        name="finance_lane",
        state_schema=FinanceLaneState,
    )


def _build_finance_lane_node(model: Any) -> Any:
    """Return the finance_lane sub-graph (a CompiledStateGraph) for the roster ``node_builder``.

    REUSE: ``build_finance_lane_agent`` unchanged. ``wrap_node=False`` — a compiled
    sub-graph must NOT be function-wrapped (VT-183 / VT-206), same as integration /
    onboarding_conductor.
    """
    return build_finance_lane_agent(model=model)


# === The roster registration (VT-465 spine) ================================
#
# Exported declaratively; the COORDINATOR registers this centrally on ROSTER (this module
# must NOT edit roster.py / handoffs.py). Mirrors the integration / onboarding_conductor
# entries: a CompiledStateGraph sub-graph (wrap_node=False), edge_to=None (-> END), and
# prereq=None — the ADVISORY lane itself has no activation bar (the manager may seek
# finance advice anytime; the reminder SEND, separately, still passes the send rail's
# onboarded-gate inside agent_send_draft). update_builder=None: the standard envelope's
# context_slice + the tenant_id in state are sufficient; the lane self-fetches its data.
#
# Lazy import so a module that only needs SPECIALIST_SPEC's data fields does not pull the
# roster (and its langchain deps) at import; resolved when the coordinator builds the spec.
def _make_specialist_spec() -> Any:
    from orchestrator.agent.roster import SpecialistSpec

    return SpecialistSpec(
        name="finance_lane",
        agent_name="finance_lane",
        spawn_tool_name="spawn_finance_lane",
        route_key="spawn_finance_lane",
        node_builder=_build_finance_lane_node,
        description=(
            "Hand off to the Finance specialist (ADVISORY) for the owner's money picture: "
            "cash-flow analysis, receivables/payables, margin/pricing input, and "
            "loss/debt identification — and to PROPOSE payment reminders for overdue "
            "receivables. Use when the desired outcome is a finance read/advice or a "
            "payment-collection nudge. Finance ADVISES and proposes reminders; it NEVER "
            "moves money, and reminder sends go through the gated customer-send rail."
        ),
        update_builder=None,
        prereq=None,
        edge_to=None,  # END — the advisory sub-graph emits no campaign plan to collapse.
        wrap_node=False,
        default_outcome="advise on the business's cash-flow and receivables",
    )


# The declarative spec the coordinator registers centrally (VT-465). Built eagerly so the
# coordinator can simply import ``SPECIALIST_SPEC``; the lazy import inside the factory
# keeps the roster import contained to this one call.
SPECIALIST_SPEC = _make_specialist_spec()


finance_lane = build_finance_lane_agent(_MODEL)


__all__ = [
    "FINANCE_LANE_SYSTEM_MESSAGE",
    "FINANCE_LANE_SYSTEM_PROMPT",
    "FINANCE_LANE_TOOLS",
    "FinanceLaneState",
    "SPECIALIST_SPEC",
    "build_finance_lane_agent",
    "finance_lane",
]
