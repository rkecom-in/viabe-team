"""VT-473 — the Cost-Optimisation specialist lane (v1 ADVISE).

The sixth lane of the Team-Manager rebuild (design §8 ratified-charters table). The manager hands
this lane a {situation, desired_outcome, context_slice, data} envelope; the lane reads the business's
REAL cost/spend/ROI substrate and returns ADVICE — wasteful spend, redundant subscriptions/vendor
cost, low-ROI marketing, and SUGGESTED resource recalibration (sharing / sharding / parallel /
full-utilization of human + non-human resources). It is **ADVISE-only**: it SUGGESTS; acting on a
recalibration is owner-gated (business-impact) and is FUTURE scope — this module does NOT build the
acting, only documents the seam.

CHARTER (design §8, VT-473 Cost-Opt — v1 ADVISE):
    | v1 scope | wasteful spend, subscriptions/vendor cost, marketing ROI; resource recalibration
    |          | (human+non-human: sharing/sharding/parallel/full-utilization)
    | Rail     | v1 SUGGEST; acting owner-gated
    | FUTURE   | act on recalibration (owner-gated, expandable) — documented, NOT built here

SHAPE — mirrors ``integration_agent.build_integration_agent`` / ``onboarding_conductor`` byte-for-byte
(langchain ``create_agent`` sub-graph + Opus + ``cache_control`` per VT-194), registered as a
``SpecialistSpec`` (``SPECIALIST_SPEC``) the coordinator appends to ``agent/roster.py``'s ``ROSTER``.
This module is DISJOINT — it owns no edit to roster.py / supervisor.py / routing.py; the coordinator
registers it centrally.

REUSE (no duplication, Fazal standing) — the lane's tools delegate to the EXISTING cost/spend/ROI
substrate; they build NO parallel aggregation:
  - ``observability.cost_dashboard.get_tenant_cost``         — per-tenant spend by category (the
                                                               wasteful-spend + vendor/subscription
                                                               cost substrate; VT-103).
  - ``observability.cost_dashboard.get_tenant_unit_economics`` — ARRR / cost ratio (is the spend
                                                               justified by the plan revenue?).
  - ``observability.cost_dashboard.detect_cost_anomalies``    — baseline-relative spend spikes
                                                               (the manager reads ONE tenant; the
                                                               lane filters the workspace scan to it).
  - ``observability.cost_dashboard.runaway_alert_candidates`` — spend as a fraction of the plan fee
                                                               (a subscription-cost over-spend flag).
  - ``agent.tools.get_attribution_data.get_attribution_data`` — campaign ARRR vs send (the
                                                               marketing-ROI substrate; VT-43).
  - ``knowledge.business_context.read_business_context``      — the manager-held objective + the
                                                               cost-relevant profile slice (context).

NO ACT TOOL (VT-268 ``assert_agent_tools_safe`` at build, fail-CLOSED): the lane holds NO send /
ledger-write / accounts-book-write / spend-execute / commitment / config-write tool. It produces
ADVICE — a structured ``CostOptAdvice`` object handed back to the manager. Acting on any suggestion
(cancel a subscription, cut an ad campaign, re-allocate a resource) is a business-impact effect that
MUST route through the owner-gated guarded-tool framework (VT-467) — see ``FUTURE_ACT_SEAM`` below;
this module deliberately does NOT wire it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain.agents import AgentState, create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.types.trigger_reason import TriggerReason

logger = logging.getLogger("orchestrator.agent.cost_opt_lane")

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "cost_opt_lane_system.md"
COST_OPT_LANE_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# VT-194 prompt caching — the cached prefix amortises the system prompt + tool inventory across
# dispatches (parity with orchestrator_agent / integration_agent / onboarding_conductor).
COST_OPT_LANE_SYSTEM_MESSAGE = SystemMessage(
    content=[
        {
            "type": "text",
            "text": COST_OPT_LANE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
)

# mypy --strict needs the call-arg ignore for ChatAnthropic's pydantic kwargs (parity with the
# orchestrator / integration / onboarding agents).
_MODEL = ChatAnthropic(model="claude-opus-4-7", max_tokens=4096)  # type: ignore[call-arg]

# Default look-back window for the cost/spend reads when the manager does not frame one. 30 days
# matches the monthly subscription/plan cadence the spend is measured against.
_DEFAULT_WINDOW_DAYS = 30


# ===========================================================================
# The FUTURE-ACT seam (documented, NOT built — design §8 "act on recalibration,
# owner-gated, expandable").
# ===========================================================================
#
# v1 is ADVISE-only. The lane SUGGESTS a recalibration; it does NOT act. When a future row turns
# on acting (cancel a subscription, pause an ad campaign, re-allocate a resource), that effect is a
# VT-467 BUSINESS-IMPACT action (spend/commitment/config change) and MUST plug into the EXISTING
# owner-gated guarded-tool framework — never a direct tool on THIS surface:
#
#   1. The act tool routes through ``agents.business_impact_choke.assert_or_gate_business_action``
#      with a ``business_action_context`` (the same deterministic gate the supervisor's send-path
#      uses). The owner-approval is THRESHOLD-based + DECAYING-HITL (it loosens as the owner grants
#      autonomy + the lane earns trust — reuse the existing VTR decay, per design §7 rails).
#   2. The tool NAME will (by construction) match a ``FORBIDDEN_CAPABILITY_SUBSTRINGS`` entry
#      (``execute_spend`` / ``commit_spend`` / ``apply_config_change`` …), so it is STRUCTURALLY
#      barred from this ADVISE surface — adding it here would raise at build (VT-268). The act tool
#      lives behind the gate, on a DIFFERENT (owner-gated) surface, by design.
#
# This constant is the documented marker of that seam. It carries NO capability; it exists so the
# boundary is explicit + greppable, and so the v1 surface stays provably ADVISE-only.
FUTURE_ACT_SEAM = (
    "v1 ADVISE-only. Acting on a cost recalibration (cancel subscription / cut campaign / "
    "re-allocate resource) is owner-gated business-impact (VT-467): a guarded tool behind "
    "assert_or_gate_business_action + decaying-HITL approval, on a separate owner-gated surface — "
    "NOT a tool on this advise surface. Documented seam; deliberately NOT built in v1."
)


# ===========================================================================
# The ADVICE output shape — what the lane hands BACK to the manager.
# ===========================================================================


class CostOptSuggestion(BaseModel):
    """One advisory suggestion (NOT an action — the lane never acts on it)."""

    model_config = ConfigDict(frozen=True)

    category: str = Field(
        ...,
        description=(
            "the suggestion class: 'wasteful_spend' | 'redundant_subscription' | "
            "'vendor_cost' | 'low_roi_marketing' | 'resource_recalibration'"
        ),
    )
    finding: str = Field(..., description="the observed cost/ROI issue, grounded in the read data")
    suggestion: str = Field(..., description="the SUGGESTED recalibration (advisory; owner decides)")
    # The recalibration LEVER (design §8): sharing / sharding / parallel / full-utilization of a
    # human or non-human resource. Empty for a pure spend/ROI flag with no resource lever.
    recalibration_lever: str = Field(
        default="",
        description=(
            "the resource-recalibration lever, if any: 'sharing' | 'sharding' | 'parallel' | "
            "'full_utilization' | '' (a pure spend/ROI flag with no resource lever)"
        ),
    )
    est_monthly_saving_paise: int | None = Field(
        default=None,
        ge=0,
        description="estimated monthly saving in paise if grounded in real numbers; None if not",
    )
    owner_gated: bool = Field(
        default=True,
        description="ALWAYS True in v1 — acting on this is owner-gated (business-impact); the lane only suggests",
    )


class CostOptAdvice(BaseModel):
    """The lane's structured return to the manager — ADVICE ONLY (design §8 v1 ADVISE).

    This object is the lane's WHOLE output surface. It carries NO side-effect, no act handle — the
    manager reads it + (in a FUTURE row) routes any chosen recalibration through the owner-gated
    guarded-tool gate. ``acted`` is pinned False: v1 NEVER acts.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    suggestions: list[CostOptSuggestion] = Field(default_factory=list)
    summary: str = ""
    acted: bool = Field(default=False, description="ALWAYS False in v1 — ADVISE-only, no acting")
    notes: list[str] = Field(default_factory=list)


def _window(window_days: int) -> tuple[datetime, datetime]:
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=max(window_days, 1))
    return since, until


# ===========================================================================
# Tools — READ-ONLY data feeds. Each delegates to the EXISTING cost/spend/ROI
# substrate (no parallel aggregation). NONE acts. Names are deliberately read-ish
# (analyze_/read_/identify_) so the VT-268 guard never false-flags them.
# ===========================================================================


@tool
def analyze_tenant_spend(tenant_id: str, window_days: int = _DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Read the tenant's spend broken down by cost category for the window — the wasteful-spend +
    vendor/subscription cost substrate.

    REUSE: delegates to ``observability.cost_dashboard.get_tenant_cost`` (VT-103) — RLS-scoped, sums
    ``pipeline_log.external_api_call`` ``cost_paise`` by category (llm / twilio / razorpay / apify /
    infra). Use it to spot the BIGGEST cost buckets + obvious waste. Returns
    ``{total_paise, by_category, event_count, window_days}`` — counts + paise ONLY, no PII.
    """
    from orchestrator.observability.cost_dashboard import get_tenant_cost

    since, until = _window(window_days)
    b = get_tenant_cost(UUID(tenant_id), since, until)
    logger.info("cost_opt: analyze_tenant_spend tenant=%s total_paise=%d", tenant_id, b.total_paise)
    return {
        "total_paise": b.total_paise,
        "by_category": dict(b.by_category),
        "event_count": b.event_count,
        "window_days": window_days,
    }


@tool
def analyze_unit_economics(tenant_id: str, window_days: int = _DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Read the ARRR / cost ratio for the tenant — is the spend justified by the plan revenue?

    REUSE: delegates to ``observability.cost_dashboard.get_tenant_unit_economics`` (VT-103). A ratio
    < 1 means the tenant costs more to serve than the plan brings in — a strong wasteful-spend /
    recalibration signal. Returns ``{arrr_paise, cost_paise, ratio}``.
    """
    from orchestrator.observability.cost_dashboard import get_tenant_unit_economics

    since, until = _window(window_days)
    ue = get_tenant_unit_economics(UUID(tenant_id), since, until)
    logger.info("cost_opt: analyze_unit_economics tenant=%s ratio=%.3f", tenant_id, ue.ratio)
    return {"arrr_paise": ue.arrr_paise, "cost_paise": ue.cost_paise, "ratio": ue.ratio}


@tool
def identify_spend_anomaly(tenant_id: str) -> dict[str, Any]:
    """Check whether the tenant's recent spend spiked vs its own baseline — a runaway-cost flag.

    REUSE: filters the workspace-level scans ``detect_cost_anomalies`` (2x baseline) +
    ``runaway_alert_candidates`` (>50% of the monthly plan fee) to THIS tenant. The manager reads
    one tenant; the lane narrows the workspace scan. Returns ``{anomaly: {...}|null,
    runaway: {...}|null}`` — a populated value is a strong wasteful/over-spend suggestion.
    """
    from orchestrator.observability.cost_dashboard import (
        detect_cost_anomalies,
        runaway_alert_candidates,
    )

    tid = UUID(tenant_id)
    anomaly = next(
        (
            {
                "baseline_per_day_paise": a.reference_avg_per_day_paise,
                "window_per_day_paise": a.window_avg_per_day_paise,
                "multiplier": a.multiplier_observed,
            }
            for a in detect_cost_anomalies()
            if a.tenant_id == tid
        ),
        None,
    )
    runaway = next(
        (
            {
                "window_cost_paise": r.window_cost_paise,
                "plan_monthly_paise": r.plan_monthly_paise,
                "pct_of_plan": r.pct_observed,
            }
            for r in runaway_alert_candidates()
            if r.tenant_id == tid
        ),
        None,
    )
    logger.info(
        "cost_opt: identify_spend_anomaly tenant=%s anomaly=%s runaway=%s",
        tenant_id, anomaly is not None, runaway is not None,
    )
    return {"anomaly": anomaly, "runaway": runaway}


@tool
def analyze_marketing_roi(
    tenant_id: str, window_days: int = _DEFAULT_WINDOW_DAYS
) -> dict[str, Any]:
    """Read marketing ROI — campaign ARRR (attributed revenue) vs send volume for the window.

    REUSE: delegates to ``agent.tools.get_attribution_data.get_attribution_data`` (VT-43) in window
    mode — per-campaign attributed paise + transacting count. A campaign with high send volume +
    near-zero ARRR is low-ROI marketing (a cut/recalibrate suggestion). Returns the window summary
    ``{campaign_count, total_arrr_paise, total_transacting_count, per_campaign:[...]}`` — paise +
    counts ONLY, no PII (CL-390).
    """
    from orchestrator.agent.tools.get_attribution_data import (
        GetAttributionDataInput,
        get_attribution_data,
    )

    since, until = _window(window_days)
    out = get_attribution_data(
        GetAttributionDataInput(tenant_id=tenant_id, window_start=since, window_end=until)
    )
    w = out.window
    if w is None:
        return {"campaign_count": 0, "total_arrr_paise": 0, "total_transacting_count": 0, "per_campaign": []}
    logger.info(
        "cost_opt: analyze_marketing_roi tenant=%s campaigns=%d arrr=%d",
        tenant_id, w.campaign_count, w.total_arrr_paise,
    )
    return {
        "campaign_count": w.campaign_count,
        "total_arrr_paise": w.total_arrr_paise,
        "total_transacting_count": w.total_transacting_count,
        "per_campaign": [
            {
                "campaign_id": s.campaign_id,
                "status": s.attribution_status,
                "arrr_paise": s.arrr_paise,
                "transacting_count": s.transacting_count,
            }
            for s in w.per_campaign_summary
        ],
    }


@tool
def read_cost_context(tenant_id: str) -> dict[str, Any]:
    """Read the manager-held business objective + the cost-relevant profile slice (context).

    REUSE: delegates to ``knowledge.business_context.read_business_context`` (VT-466) — the ONE
    per-tenant context record (verified identity + the objective the manager holds). Use it to frame
    a suggestion against the owner's stated goal (e.g. "you're spending to grow — this ad campaign
    isn't converting"). Best-effort: a read miss yields ``{}``. No cross-tenant data.
    """
    try:
        from orchestrator.knowledge.business_context import read_business_context

        ctx = read_business_context(tenant_id)
        return {"objective": dict(ctx.objective), "identity": dict(getattr(ctx, "identity", {}) or {})}
    except Exception:  # noqa: BLE001 — context is enrichment; a miss yields {}
        logger.warning("cost_opt: read_cost_context miss tenant=%s", tenant_id)
        return {}


COST_OPT_LANE_TOOLS: list[BaseTool] = [
    analyze_tenant_spend,
    analyze_unit_economics,
    identify_spend_anomaly,
    analyze_marketing_roi,
    read_cost_context,
]


class CostOptLaneState(AgentState, total=False):
    """State schema for the cost_opt_lane sub-graph (mirrors IntegrationAgentState).

    Carries the run-identity fields into the sub-graph so a future handoff tool's ``InjectedState``
    can read them (parity with the integration / onboarding agents; the current tool set keys on
    ``tenant_id`` passed as a tool arg).
    """

    run_id: UUID | None
    tenant_id: UUID | None
    trigger_reason: TriggerReason | None


def build_cost_opt_lane_agent(
    model: ChatAnthropic = _MODEL,
    *,
    extra_tools: Sequence[BaseTool] = (),
) -> Any:
    """Build the Cost-Opt ADVISE specialist sub-graph (mirrors ``build_integration_agent``).

    VT-268 fail-CLOSED guardrail: the lane must NEVER hold a direct send / ledger-write /
    accounts-book-write / spend-execute / commitment / config-write tool (raises at build if it
    does). v1 is ADVISE-only — it SUGGESTS; acting on a recalibration is owner-gated business-impact
    (VT-467) and is NOT wired here (see ``FUTURE_ACT_SEAM``).
    """
    tools = [*COST_OPT_LANE_TOOLS, *extra_tools]
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    assert_agent_tools_safe(tools, surface="cost_opt_lane")
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=COST_OPT_LANE_SYSTEM_MESSAGE,
        name="cost_opt_lane",
        state_schema=CostOptLaneState,
    )


def _build_cost_opt_lane_node(model: Any) -> Any:
    """Return the cost_opt_lane sub-graph (a CompiledStateGraph) — the roster ``node_builder``.

    REUSE: ``build_cost_opt_lane_agent`` unchanged. ``wrap_node=False`` in ``SPECIALIST_SPEC`` —
    a compiled sub-graph must NOT be function-wrapped (VT-183 / VT-206), same as integration /
    onboarding. Imported by ``agent/roster.py`` when the coordinator appends ``SPECIALIST_SPEC``.
    """
    return build_cost_opt_lane_agent(model=model)


# ===========================================================================
# The roster registration — ONE SpecialistSpec the coordinator appends to ROSTER.
# This module owns NO edit to roster.py; it EXPORTS the spec, the coordinator
# registers it centrally (design §7 "adding a lane = a sub-graph + a registry
# entry", not graph surgery).
# ===========================================================================


# The exported spec the coordinator appends to ``ROSTER`` (design §7 "adding a lane = a sub-graph +
# a registry entry"). Built at import as an INSTANCE — same as the existing ROSTER entries — so the
# coordinator's wiring is a one-line ``ROSTER.append(SPECIALIST_SPEC)``. The ``SpecialistSpec`` TYPE
# is imported from roster.py (a one-way edge: roster.py does NOT import this module — it imports the
# node builders lazily inside its own spec entries — so no import cycle). The ``node_builder`` itself
# stays a module-level callable (``_build_cost_opt_lane_node``) the coordinator-iterated graph build
# invokes; the langchain/Anthropic deps load only when that fires, not at roster iteration.
from orchestrator.agent.roster import SpecialistSpec  # noqa: E402 — placed after the node builder

SPECIALIST_SPEC = SpecialistSpec(
    name="cost_opt",
    agent_name="cost_opt_lane",
    spawn_tool_name="spawn_cost_opt",
    route_key="spawn_cost_opt",
    node_builder=_build_cost_opt_lane_node,
    description=(
        "Hand off to the Cost-Optimisation specialist (ADVISE-only) to surface wasteful spend, "
        "redundant subscriptions / vendor cost, and low-ROI marketing, and to SUGGEST resource "
        "recalibration (sharing / sharding / parallel / full-utilization of human + non-human "
        "resources). Use when the desired outcome is reducing cost or improving spend efficiency. "
        "It SUGGESTS only — acting on a recalibration is owner-gated."
    ),
    update_builder=None,  # the lane self-fetches via tenant_id; no pre-built data bundle.
    prereq=None,
    edge_to=None,  # END — the lane emits advice, not a campaign plan to collapse.
    wrap_node=False,  # CompiledStateGraph sub-graph — not function-wrapped (VT-183).
    default_outcome="reduce cost / improve spend efficiency (advise)",
)


cost_opt_lane = build_cost_opt_lane_agent(_MODEL)


__all__ = [
    "COST_OPT_LANE_SYSTEM_MESSAGE",
    "COST_OPT_LANE_SYSTEM_PROMPT",
    "COST_OPT_LANE_TOOLS",
    "CostOptAdvice",
    "CostOptLaneState",
    "CostOptSuggestion",
    "FUTURE_ACT_SEAM",
    "SPECIALIST_SPEC",
    "build_cost_opt_lane_agent",
    "cost_opt_lane",
]
