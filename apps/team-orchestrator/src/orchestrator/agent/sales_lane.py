"""VT-468 — the SALES specialist lane (Team-Manager rebuild, design §8).

The Sales lane is the customer-relationship REVENUE specialist. The Team-Manager
(VT-461) frames a SITUATION + a desired OUTCOME and hands it here; THIS lane owns
the ACTION — its domain expertise picks WHICH sales play serves the outcome
(design §6/§7 division of intelligence).

v1 scope (Fazal/Cowork ratified charter, design §8):
  win-back lapsed  -> repeat / upsell / re-engage.
v1 = advise / act-within-policy. NO future-autonomy is built here.

REUSE — win-back is NOT rebuilt. The existing Sales-Recovery pipeline
(``agent/sales_recovery.run_sales_recovery_agent`` for the SDK loop +
``agents/sales_recovery_executor.SalesRecoveryAgent`` for the deterministic
detect -> draft -> arm pipeline) OWNS win-back. When win-back is the right play,
this lane RECOMMENDS the ``winback`` play (an INTENT) and Sales-Recovery does the
detection / drafting / arming. The NEW reasoning this module adds is identifying
the repeat-purchase / upsell / re-engagement OPPORTUNITY from the customer-ledger
slice — the plays Sales-Recovery does not cover.

SHAPE — mirrors ``integration_agent.build_integration_agent`` /
``onboarding_conductor.build_onboarding_conductor_agent`` byte-for-byte (langchain
``create_agent`` sub-graph + Opus + VT-194 ``cache_control``), registered as ONE
``SpecialistSpec`` in ``agent/roster.py`` (the coordinator does that registration
centrally — see ``SPECIALIST_SPEC`` at the bottom of this module).

THE RAIL — the brain has NO send/write tool (VT-268). This lane reasons and emits
INTENTS / drafts; it NEVER sends. Every customer send routes through the existing
deterministic choke point (``agents/customer_send.agent_send_draft`` via the
``agent_draft`` path), which independently re-runs consent / opt-out / onboarded /
caps / suppression at send time, and to which the VT-474 decaying-checkpoint
owner-visibility curve applies AT THE RAIL LAYER. ``assert_agent_tools_safe`` at
build refuses to start if a send/write tool is ever added to this surface.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from langchain.agents import AgentState, create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool, tool

from orchestrator.types.trigger_reason import TriggerReason

logger = logging.getLogger("orchestrator.agent.sales_lane")

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "sales_lane_system.md"
SALES_LANE_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# VT-194 prompt caching — the cached prefix amortises the system prompt + tool
# inventory across dispatches (parity with orchestrator / integration / onboarding
# agents).
SALES_LANE_SYSTEM_MESSAGE = SystemMessage(
    content=[
        {
            "type": "text",
            "text": SALES_LANE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
)

# mypy --strict needs the call-arg ignore for ChatAnthropic's pydantic kwargs
# (parity with the orchestrator / integration / onboarding agents).
_MODEL = ChatAnthropic(model="claude-opus-4-7", max_tokens=4096)  # type: ignore[call-arg]


# The four v1 sales plays (design §8 charter). ``winback`` delegates to the
# EXISTING Sales-Recovery pipeline; the other three are the new opportunity types
# this lane reasons about. A Literal so the recommendation tool's contract is
# typed + the play set cannot silently drift.
SalesPlay = Literal["winback", "repeat_purchase", "upsell", "re_engage"]

# The play that REUSES the existing Sales-Recovery pipeline (NOT rebuilt here).
# A recommendation of this play is an INTENT routed to Sales-Recovery; this lane
# does not detect / draft / arm it.
WINBACK_DELEGATES_TO = "sales_recovery"


# -----------------------------------------------------------------
# Tools — reasoning GROUNDING only. Every tool returns a typed recommendation /
# opportunity DESCRIPTION (an INTENT). NONE sends, drafts-to-DB, or writes the
# ledger. The customer-facing effect of a recommendation runs LATER through the
# deterministic send rail (agents/customer_send.agent_send_draft) — never here.
# NO send tool. NO write tool. (VT-268 assert_agent_tools_safe enforces this.)
# -----------------------------------------------------------------


@tool
def recommend_sales_play(
    play: SalesPlay,
    target_framing: str,
    reasoning: str,
    confidence: str = "low",
) -> dict[str, Any]:
    """Record the SALES PLAY this lane recommends for the manager's desired outcome (an INTENT).

    This is the lane's primary action: having identified the opportunity, it picks ONE play and
    frames WHO + WHY. This is a RECOMMENDATION ONLY — it does NOT send, draft-to-DB, or arm
    anything. The customer-facing effect runs later through the deterministic send rail
    (``agents/customer_send.agent_send_draft`` via the ``agent_draft`` path), which independently
    re-runs every compliance gate at send time and to which the VT-474 decaying-checkpoint applies.

    ``play`` — one of winback / repeat_purchase / upsell / re_engage.
      A ``winback`` recommendation DELEGATES to the EXISTING Sales-Recovery pipeline (not rebuilt
      here): the deterministic detector + drafter + send rail run it. The other three plays are the
      opportunity types this lane reasons about.
    ``target_framing`` — WHO the play targets + WHY (the cohort/customer framing), NOT the literal
      message text. The drafter (Sales-Recovery for win-back) + the rail own the wording + the send.
    ``reasoning`` — the ledger-grounded signal that justifies the play (e.g. "cadence ~30d, last
      order 52d ago"). Grounded, not invented.
    ``confidence`` — low / medium / high; default low. No point estimates of revenue.

    Returns the structured INTENT (``kind='sales_play_recommendation'``) for the manager to monitor.
    """
    intent: dict[str, Any] = {
        "kind": "sales_play_recommendation",
        # VT-554 (B3 action-path): the UNIFORM action-return envelope — pushback=False + action_taken
        # + outcome, so this ACTION flows through the SAME specialist_return bridge as a pushback and
        # the manager decision loop observes it (ACCEPT / NEXT_SPECIALIST), not just pushbacks.
        "pushback": False,
        "action_taken": f"recommended {play} play",
        "outcome": target_framing,
        "play": play,
        "delegates_to": WINBACK_DELEGATES_TO if play == "winback" else None,
        "target_framing": target_framing,
        "reasoning": reasoning,
        "confidence": confidence,
    }
    logger.info(
        "sales_lane: recommend_sales_play play=%s confidence=%s (intent only — no send)",
        play,
        confidence,
    )
    # Observe the manager decision on this real action (config-gated enforce; default observe-only).
    from orchestrator.agent.specialist_return import handle_specialist_return

    handle_specialist_return(intent, agent="sales")
    return intent


@tool
def identify_repeat_upsell_opportunity(
    customer_ledger_slice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Identify repeat-purchase / upsell / re-engagement OPPORTUNITY from a customer-ledger slice.

    This is the NEW reasoning the Sales lane adds beyond win-back (which Sales-Recovery owns). It
    inspects a SCOPED ledger slice the manager hands the lane and surfaces a structured opportunity
    DESCRIPTION the lane can then turn into a ``recommend_sales_play`` call. It is pure reasoning
    grounding — it reads NOTHING from the DB itself (the slice is provided), and it emits NO send /
    draft / write.

    ``customer_ledger_slice`` — the lane-scoped slice the manager passed in the handoff
    (``SpecialistHandoff.context_slice`` / ``data``). When absent / empty, the lane has no grounding
    and must push back (see ``push_back_to_manager``) rather than invent an opportunity.

    Returns ``{grounded: bool, candidate_plays: [...], note: str}``. ``grounded=False`` signals the
    lane to push back — never to fabricate a customer, a cadence, or a spend figure.
    """
    slice_ = customer_ledger_slice or {}
    if not slice_:
        logger.info("sales_lane: identify_opportunity called with empty slice — not grounded")
        return {
            "grounded": False,
            "candidate_plays": [],
            "note": (
                "No customer-ledger slice supplied — cannot ground a repeat/upsell/re-engage "
                "opportunity. Push back to the manager for the data slice; do NOT invent one."
            ),
        }
    # Grounded: the candidate plays this lane can reason toward from a non-empty slice. The MODEL
    # decides which fits using its domain expertise + the slice content; this tool just affirms the
    # menu so the reasoning stays inside the v1 charter (no fabricated play type).
    return {
        "grounded": True,
        "candidate_plays": ["repeat_purchase", "upsell", "re_engage"],
        "note": (
            "Ledger slice present — reason over the cadence / spend / recency signals in it to pick "
            "the best-fit play, then call recommend_sales_play. Win-back (lapsed) delegates to "
            "Sales-Recovery."
        ),
    }


@tool
def push_back_to_manager(reason: str, proposed_outcome: str) -> dict[str, Any]:
    """PUSH BACK to the manager when the desired outcome is infeasible / unwise IN-LANE (design §7).

    The handoff is TWO-WAY: if the manager's desired outcome cannot or should not be served by a
    sales action (no consent on the cohort, the opportunity does not exist in the slice, a win-back
    is asked for customers merely cooling, an over-contact suppression window, etc.), the lane does
    NOT silently force a bad action and does NOT silently refuse — it returns a structured pushback
    so the manager can re-frame or escalate.

    ``reason`` — why the desired outcome is infeasible / unwise in this lane.
    ``proposed_outcome`` — the better outcome the lane proposes instead (the manager re-frames + re-
    dispatches, or escalates; it does NOT force the original action).

    Returns the structured pushback envelope (``kind='sales_lane_pushback'``). This is the
    ``SpecialistReturn(pushback=True, ...)`` seam (roster.py); it carries NO action effect.
    """
    logger.info("sales_lane: push_back_to_manager (no action taken) reason=%s", reason[:120])
    env = {
        "kind": "sales_lane_pushback",
        "pushback": True,
        "reason": reason,
        "proposed_outcome": proposed_outcome,
    }
    # VT-526 (B3) graph-wiring + VT-554 (config-gated enforce): run the manager decision loop on this
    # REAL pushback — observe-only by default (routing unchanged); when MANAGER_ENFORCE_ROUTING is on,
    # a no-path ESCALATE is acted on deterministically. Lazy import keeps deps off the lane's surface.
    from orchestrator.agent.specialist_return import handle_specialist_return

    handle_specialist_return(env, agent="sales_lane")
    return env


@tool
def sales_lane_escalate_to_fazal(run_id: str, reason: str) -> str:
    """Escalate to Fazal — last-resort, EXTREME scenarios only (design §6/§8 escalation).

    Not the default for routine sales actions (the lane is biased to ACT within policy). Reserved
    for the deterministic extreme triggers (anomaly, repeated failure, out-of-policy irreversible
    attempt). Log + return ack; the escalation channel itself is WhatsApp-only, owned elsewhere."""
    logger.warning("SALES_LANE_ESCALATE run_id=%s reason=%s", run_id, reason)
    return f"[escalated] reason={reason}"


SALES_LANE_TOOLS: list[BaseTool] = [
    recommend_sales_play,
    identify_repeat_upsell_opportunity,
    push_back_to_manager,
    sales_lane_escalate_to_fazal,
]


class SalesLaneState(AgentState, total=False):
    """State schema for the sales_lane sub-graph (mirrors IntegrationAgentState /
    OnboardingConductorState). Carries run-identity into the sub-graph so a future handoff tool's
    ``InjectedState`` can read it (parity; the current tool set keys on args)."""

    run_id: UUID | None
    tenant_id: UUID | None
    trigger_reason: TriggerReason | None


def build_sales_lane_agent(
    model: ChatAnthropic = _MODEL,
    *,
    extra_tools: Sequence[BaseTool] = (),
) -> Any:
    """Build the Sales-lane specialist sub-graph (mirrors ``build_onboarding_conductor_agent``).

    VT-268 fail-CLOSED guardrail: the Sales lane must NEVER hold a direct customer-send /
    accounts-book-write / ledger-write tool (raises at build if it does). It reasons about WHICH
    sales play serves the outcome + emits INTENTS; every customer send routes through the existing
    deterministic send rail (``agents/customer_send.agent_send_draft``), never a tool here.
    """
    tools = [*SALES_LANE_TOOLS, *extra_tools]
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    assert_agent_tools_safe(tools, surface="sales_lane")
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=SALES_LANE_SYSTEM_MESSAGE,
        name="sales_lane",
        state_schema=SalesLaneState,
    )


sales_lane = build_sales_lane_agent(_MODEL)


def _build_sales_lane_node(model: Any) -> Any:
    """Roster ``node_builder`` adapter — return the Sales-lane sub-graph (a CompiledStateGraph).

    REUSE: ``build_sales_lane_agent`` unchanged. ``wrap_node=False`` in the SpecialistSpec — a
    compiled sub-graph must NOT be function-wrapped (VT-183 / VT-206), same as the integration +
    onboarding lanes. Provided here so the coordinator's ONE-line ROSTER append references it
    without re-deriving the node-builder.
    """
    return build_sales_lane_agent(model=model)


# === Registration handle for the coordinator (one-line ROSTER append) =========
#
# The coordinator registers this lane into ``agent/roster.py`` ROSTER centrally
# (this module does NOT edit roster.py — disjoint-module discipline). It builds a
# ``SpecialistSpec`` from these fields. ``update_builder`` is intentionally None:
# the Sales lane reads its scoped slice from the standard VT-465 handoff envelope
# (``SpecialistHandoff.context_slice`` populated by ``context_slice_for_lane`` —
# the coordinator adds a ``"sales_lane"`` key to ``_LANE_PROFILE_KEYS`` if a wider
# slice than the identity anchor is wanted); no bespoke data bundle is needed.
# ``edge_to=None`` -> END (a reasoning lane emits an intent, not a campaign plan to
# collapse — parity with integration / onboarding). ``prereq="sales_recovery"``
# ties the lane's activation bar to the existing Sales-Recovery prereq (win-back
# is its core v1 play and shares the same activation requirements).
SPECIALIST_SPEC: dict[str, Any] = {
    "name": "sales_lane",
    "agent_name": "sales_lane",
    "spawn_tool_name": "spawn_sales_lane",
    "route_key": "spawn_sales_lane",
    "node_builder": _build_sales_lane_node,
    "description": (
        "Hand off to the Sales specialist for customer-relationship REVENUE work: "
        "win-back of lapsed customers (delegates to Sales-Recovery), repeat-purchase "
        "nudges, upsell / cross-sell, and re-engagement of cooling customers. Use when "
        "the desired outcome is recovering, repeating, growing, or sustaining revenue "
        "from EXISTING customers."
    ),
    "update_builder": None,
    "prereq": "sales_recovery",
    "edge_to": None,  # END — a reasoning lane emits an intent, not a plan to collapse.
    "wrap_node": False,  # CompiledStateGraph — never function-wrapped.
    "default_outcome": "grow revenue from existing customers (win-back / repeat / upsell / re-engage)",
}


__all__ = [
    "SALES_LANE_SYSTEM_MESSAGE",
    "SALES_LANE_SYSTEM_PROMPT",
    "SALES_LANE_TOOLS",
    "SPECIALIST_SPEC",
    "WINBACK_DELEGATES_TO",
    "SalesLaneState",
    "SalesPlay",
    "build_sales_lane_agent",
    "sales_lane",
]
