"""VT-469 — the MARKETING specialist lane (Team-Manager rebuild, design §8 charter).

The Marketing lane is one of the six manager specialists (design §7 "Division of intelligence",
211500Z). The MANAGER reads the business situation + decides the desired OUTCOME ("re-engage the
festival crowd", "drive repeat orders this Diwali") + hands off to THIS specialist; the SPECIALIST
takes {situation, outcome, context_slice, data} and decides the ACTION using its DOMAIN EXPERTISE —
campaigns, seasonal/festival offers, customer segments, content drafts (§8 VT-469 charter). It is
ACTION-accountable, lane-scoped, and holds NO cross-functional strategy.

SHAPE — mirrors ``integration_agent.build_integration_agent`` / ``onboarding_conductor`` byte-for-byte
(langchain ``create_agent`` sub-graph + Opus + ``cache_control`` per VT-194), registered via the
``SPECIALIST_SPEC`` dict the coordinator translates into a roster ``SpecialistSpec`` (VT-465). Adding
this lane = a sub-graph + ONE registry entry + its tool-set — NOT graph surgery (the roster spine).

THE CONTRACT — INTENTS through the RAILS, never a direct effect (design §4/§7, VT-268)
---------------------------------------------------------------------------
"Nothing hardcoded" = dynamic BEHAVIOUR; the safety/correctness RAILS stay DETERMINISTIC. The
specialist REASONS about marketing (the dynamic part) and produces campaign plans / content drafts /
segment selections as INTENTS. Every consequential effect routes through the EXISTING deterministic
rails — the specialist has NO code path around them and HOLDS NO tool that performs one:

  * SEND (a campaign / offer to customers) → the CUSTOMER-SEND rail. The specialist NEVER sends
    (VT-268: no send tool on its surface — graph build RAISES if one is added). It checks the send
    intent against the deterministic policy bound (``assert_within_policy`` for ``CUSTOMER_SEND`` —
    allowed segment + frequency cap, VT-474 A2) and reports the gate. The ACTUAL send runs through
    the EXISTING campaign + customer-send machinery (``campaign.execute.execute_approved_campaign`` →
    ``customer_send_choke.assert_customer_send_allowed`` consent/caps + the VT-474 decaying-checkpoint),
    which the deterministic (non-agent) path invokes after owner-approval/decay — never from a tool
    here. REUSE, not rebuild: the campaign schema (``CampaignPlan`` v1.0) + ``execute_approved_campaign``
    are the existing campaign machinery this lane plans INTO.

  * AD-SPEND (a paid boost / promotion budget) → the BUSINESS-IMPACT rail (VT-467, owner-gated). The
    specialist NEVER spends (VT-268: no spend tool). It checks the spend intent against the policy
    spend-ceiling bound, then routes the ``SPEND`` magnitude through ``assert_or_gate_business_action``
    — DETERMINISTICALLY autonomous-vs-owner-approval from {magnitude, the tenant's autonomy tier}. An
    at/above-threshold or low-autonomy tenant ⇒ REQUIRES_OWNER_APPROVAL (the decaying-HITL). The
    actual payment effect is a non-agent path inside ``business_action_context`` AFTER the gate.

So this lane's tools are: marketing REASONING (advise — campaign/offer/segment/content drafting, the
domain expertise) + RAIL-FACING INTENT checks (report the deterministic gate decision for a send /
spend) + escalate. It carries NO ``send_*`` / ``execute_spend`` / ``make_payment`` capability; the
VT-268 ``assert_agent_tools_safe`` at build is the fail-CLOSED backstop.

v1 = ADVISE / ACT-WITHIN-POLICY (design §8). NO future-autonomy is built here — the specialist
proposes + the rails gate; it does not self-grant policy or self-loosen the gate.
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

from orchestrator.agent.lane_tenant import lane_tenant_error, resolve_lane_tenant
from orchestrator.types.trigger_reason import TriggerReason

logger = logging.getLogger("orchestrator.agent.marketing")

_PROMPT_PATH = (
    Path(__file__).parent.parent / "prompts" / "marketing_lane_system.md"
)
MARKETING_LANE_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# VT-194 prompt caching — the cached prefix amortises the system prompt + tool inventory across
# dispatches (parity with orchestrator_agent / integration_agent / onboarding_conductor).
MARKETING_LANE_SYSTEM_MESSAGE = SystemMessage(
    content=[
        {
            "type": "text",
            "text": MARKETING_LANE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
)

# mypy --strict needs the call-arg ignore for ChatAnthropic's pydantic kwargs (parity with the
# orchestrator / integration / onboarding agents).
_MODEL = ChatAnthropic(model="claude-opus-4-7", max_tokens=4096)  # type: ignore[call-arg]


# -----------------------------------------------------------------
# Tools — marketing REASONING (advise) + RAIL-FACING INTENT checks. NO send/spend tool: every
# consequential effect routes through the EXISTING deterministic rails (the specialist reports the
# gate decision; the non-agent path runs the effect after the gate). REUSE the rail functions; this
# module owns ZERO side-effect machinery of its own.
# -----------------------------------------------------------------


@tool
def list_recent_campaigns(tenant_id: str, days_back: int = 90, limit: int = 20) -> dict[str, Any]:
    """List the tenant's recent campaigns (counts only) so the specialist plans the NEXT campaign in
    context — frequency, what was sent, response rates — without re-sending the same offer.

    REUSE: delegates to the VT-42 ``get_recent_campaigns`` deterministic rollup (aggregate counts only;
    CL-390: NO per-recipient PII — no customer_id, no phone). Read-only; no side effect. Use this
    BEFORE proposing a new campaign/offer so a seasonal push doesn't collide with a recent one.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="list_recent_campaigns")
    if resolved is None:
        return lane_tenant_error("list_recent_campaigns")
    tenant_id = str(resolved)

    from orchestrator.agent.tools.get_recent_campaigns import (
        GetRecentCampaignsInput,
        get_recent_campaigns,
    )

    out = get_recent_campaigns(
        GetRecentCampaignsInput(tenant_id=tenant_id, days_back=days_back, limit=limit)
    )
    campaigns = [
        {
            "campaign_id": c.campaign_id,
            "sent_at": c.sent_at.isoformat(),
            "template_id": c.template_id,
            "recipients_count": c.recipients_count,
            "response_count": c.response_count,
            "status": c.status,
        }
        for c in out.campaigns
    ]
    logger.info(
        "marketing_lane: list_recent_campaigns tenant=%s days_back=%d count=%d",
        tenant_id, days_back, len(campaigns),
    )
    return {"campaigns": campaigns, "count": len(campaigns)}


@tool
def draft_campaign_plan(
    tenant_id: str,
    objective: str,
    segment_label: str,
    offer_summary: str,
    message_draft: str,
) -> dict[str, Any]:
    """Draft a campaign / seasonal-offer PLAN as an INTENT — the specialist's domain expertise, no
    effect. This produces the marketing PROPOSAL (segment + offer + message draft); it does NOT send
    and does NOT persist a customer-send. The intent is handed back to the manager / the deterministic
    campaign path, which validates it into a ``CampaignPlan`` (the EXISTING VT-37 schema) + routes the
    send through the rails (see ``check_send_intent``).

    Args:
      objective — the business outcome the campaign serves (e.g. "re-engage festival crowd").
      segment_label — the human label of the customer segment this targets (e.g. "lapsed_60d",
        "diwali_buyers"). The DETERMINISTIC segment bound is checked by ``check_send_intent``; this is
        the specialist's intended target, NOT an authorization.
      offer_summary — the seasonal/festival offer (e.g. "15% off, Diwali week").
      message_draft — the owner-facing draft of the message (content the specialist drafts; the
        approved Meta template + params are resolved on the deterministic send path, not here).

    Returns the structured intent (``{kind: 'campaign_plan', ...}``). No PII (CL-390): a segment LABEL
    + free-text the OWNER reviews, never a customer phone/name/id.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="draft_campaign_plan")
    if resolved is None:
        return lane_tenant_error("draft_campaign_plan")
    tenant_id = str(resolved)

    intent = {
        "kind": "campaign_plan",
        "tenant_id": tenant_id,
        "objective": objective,
        "segment_label": segment_label,
        "offer_summary": offer_summary,
        "message_draft": message_draft,
    }
    logger.info(
        "marketing_lane: draft_campaign_plan tenant=%s segment=%s (intent only, no send)",
        tenant_id, segment_label,
    )
    return intent


@tool
def draft_content(tenant_id: str, content_type: str, brief: str, draft: str) -> dict[str, Any]:
    """Draft marketing CONTENT (a caption, a post, an offer blurb, a festival greeting) — pure
    advisory output, NO side effect, NO send. The specialist's domain expertise: produce the draft;
    the owner reviews; any actual publish/send is a SEPARATE rail-gated action, never this tool.

    Args:
      content_type — what kind of content (e.g. "whatsapp_offer", "festival_greeting", "post_caption").
      brief — what the content should achieve (the manager's outcome / the specialist's framing).
      draft — the drafted content text.

    Returns ``{kind: 'content_draft', ...}``. The draft is owner-reviewed copy, not a customer message
    in flight — it carries no recipient and triggers no send.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="draft_content")
    if resolved is None:
        return lane_tenant_error("draft_content")
    tenant_id = str(resolved)

    intent = {
        "kind": "content_draft",
        "tenant_id": tenant_id,
        "content_type": content_type,
        "brief": brief,
        "draft": draft,
    }
    logger.info(
        "marketing_lane: draft_content tenant=%s type=%s (advisory, no send)",
        tenant_id, content_type,
    )
    return intent


@tool
def check_send_intent(
    tenant_id: str,
    segment_label: str,
    frequency_cap_key: str = "",
    period_count: int = 0,
) -> dict[str, Any]:
    """Check a CAMPAIGN-SEND intent against the DETERMINISTIC policy bound — the rail decides, NOT the
    specialist. The specialist NEVER sends (VT-268: it holds no send tool); this is the policy
    bound-check it consults BEFORE proposing a send, so it doesn't draft a campaign the owner's policy
    forbids.

    REUSE (no rebuild): delegates to ``business_policy.assert_within_policy`` for the ``CUSTOMER_SEND``
    class (VT-474 A2) — the owner's machine-enforceable bounds: is this SEGMENT allowed + is the
    frequency cap satisfied. ``in_policy`` here means the send MAY proceed to the customer-send rail
    (``customer_send_choke.assert_customer_send_allowed`` consent/opt-out/caps/onboarded + the VT-474
    decaying-checkpoint owner-visibility) on the EXISTING deterministic (non-agent) campaign path —
    NOT that anything has been sent. ``out_of_policy`` ⇒ the specialist does NOT propose the send;
    it pushes the proposal back to the manager (a policy grant is an OWNER act, not the brain's).

    Args:
      segment_label — the target customer segment (bound-checked against allowed_segments).
      frequency_cap_key / period_count — the owner's frequency cap to enforce + the current count in
        the period (the caller supplies the count; the rail compares). Omit (default '') for a
        type/segment-only check.

    Returns ``{in_policy, reason, action_class}`` — a reason CODE, never an instruction body (CL-390).
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="check_send_intent")
    if resolved is None:
        return lane_tenant_error("check_send_intent")
    tenant_id = str(resolved)

    from orchestrator.agents.business_policy import PolicyActionClass, assert_within_policy

    attrs: dict[str, Any] = {"segment": segment_label}
    if frequency_cap_key:
        attrs["frequency_cap_key"] = frequency_cap_key
        attrs["period_count"] = period_count
    check = assert_within_policy(UUID(tenant_id), PolicyActionClass.CUSTOMER_SEND, attrs)
    logger.info(
        "marketing_lane: check_send_intent tenant=%s segment=%s in_policy=%s reason=%s",
        tenant_id, segment_label, check.in_policy, check.reason,
    )
    return {
        "in_policy": check.in_policy,
        "reason": check.reason,
        "action_class": check.action_class,
    }


@tool
def check_ad_spend_intent(tenant_id: str, magnitude_minor: int, purpose: str) -> dict[str, Any]:
    """Check an AD-SPEND intent (a paid boost / promotion budget) against the DETERMINISTIC rails —
    the rail decides autonomous-vs-owner-approval, NOT the specialist. The specialist NEVER spends
    (VT-268: it holds no spend tool); this consults the gate so it knows whether a proposed boost is
    autonomous, needs owner approval, or is out of policy — BEFORE proposing it.

    REUSE (no rebuild): runs the SAME deterministic stack a real spend would:
      1. ``business_policy.assert_within_policy`` for ``SPEND`` — the policy's OUTER spend ceiling
         (VT-474 A2). out_of_policy ⇒ owner approval regardless of magnitude (the brain can't reason
         past the owner's ceiling).
      2. ``business_impact_choke.assert_or_gate_business_action`` for ``BusinessImpactClass.SPEND`` —
         the per-class autonomy tier (VT-467 decaying-HITL): below the tenant's threshold + a
         permitting tier ⇒ AUTONOMOUS; at/above, or a low-autonomy/frozen tenant ⇒
         REQUIRES_OWNER_APPROVAL (routed through the existing owner-approval flow).

    ``magnitude_minor`` is the spend in PAISE (integer; never float — CL all-currency-is-paise). The
    ACTUAL payment is a non-agent effect inside ``business_action_context`` AFTER an AUTONOMOUS gate or
    the owner's approval — never this tool. ``purpose`` is an owner-facing summary (the boost rationale).

    Returns ``{decision, reason, action_class, magnitude_minor, requires_owner_approval}`` — IDs +
    class + magnitude + a reason CODE only (CL-390); ``purpose`` is the specialist's own framing, not
    an owner secret.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="check_ad_spend_intent")
    if resolved is None:
        return lane_tenant_error("check_ad_spend_intent")
    tenant_id = str(resolved)

    from orchestrator.agents.business_impact_choke import (
        BusinessActionDecision,
        BusinessImpactClass,
        assert_or_gate_business_action,
    )

    gate = assert_or_gate_business_action(
        UUID(tenant_id),
        BusinessImpactClass.SPEND,
        magnitude_minor,
        action_attrs={"magnitude_minor": magnitude_minor},
    )
    logger.info(
        "marketing_lane: check_ad_spend_intent tenant=%s magnitude_minor=%d decision=%s reason=%s purpose=%s",
        tenant_id, magnitude_minor, gate.decision.value, gate.reason, purpose[:32],
    )
    return {
        "decision": gate.decision.value,
        "reason": gate.reason,
        "action_class": gate.action_class,
        "magnitude_minor": gate.magnitude_minor,
        "requires_owner_approval": gate.decision is BusinessActionDecision.REQUIRES_OWNER_APPROVAL,
    }


@tool
def marketing_escalate_to_fazal(run_id: str, reason: str, owner_stuck_at: str) -> str:
    """Escalate to the owner (WhatsApp-only, design §6) when a marketing decision is outside policy or
    a high-stakes judgment the specialist should not make in-lane. Log + return ack (last-resort)."""
    logger.warning(
        "MARKETING_ESCALATE run_id=%s reason=%s stuck_at=%s",
        run_id, reason, owner_stuck_at,
    )
    return f"[escalated] reason={reason}"


MARKETING_LANE_TOOLS: list[BaseTool] = [
    list_recent_campaigns,    # read-only context (VT-42 rollup, counts-only)
    draft_campaign_plan,      # advise: campaign/seasonal-offer INTENT (no send)
    draft_content,            # advise: content draft (no send)
    check_send_intent,        # rail-facing: policy bound-check for a CUSTOMER_SEND intent
    check_ad_spend_intent,    # rail-facing: policy + business-impact gate for a SPEND intent
    marketing_escalate_to_fazal,
]


class MarketingLaneState(AgentState, total=False):
    """State schema for the marketing_lane sub-graph (mirrors IntegrationAgentState /
    OnboardingConductorState).

    Carries the run-identity fields into the sub-graph so a future handoff tool's ``InjectedState``
    can read them (parity with the existing specialists; the current tool set keys on ``tenant_id``
    passed as a tool arg).
    """

    run_id: UUID | None
    tenant_id: UUID | None
    trigger_reason: TriggerReason | None


def build_marketing_lane_agent(
    model: ChatAnthropic = _MODEL,
    *,
    extra_tools: Sequence[BaseTool] = (),
) -> Any:
    """Build the Marketing specialist sub-graph (mirrors ``build_integration_agent`` /
    ``build_onboarding_conductor_agent``).

    VT-268 fail-CLOSED guardrail: the marketing specialist must never hold a direct customer-send /
    spend / commitment / config-write tool (raises at build if it does) — it REASONS about marketing +
    produces intents; the deterministic rails own every side-effect (the customer-send rail for a
    send, the business-impact rail for a spend). The send/spend INTENT checks here are READS of the
    deterministic gate (no effect), not the effect itself.
    """
    tools = [*MARKETING_LANE_TOOLS, *extra_tools]
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    assert_agent_tools_safe(tools, surface="marketing_lane")
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=MARKETING_LANE_SYSTEM_MESSAGE,
        name="marketing_lane",
        state_schema=MarketingLaneState,
    )


def build_marketing_lane_node(model: Any = _MODEL) -> Any:
    """Return the marketing_lane sub-graph node for the roster ``node_builder``.

    Mirrors ``roster._build_integration_node`` / ``_build_onboarding_conductor_node``: a CompiledState
    Graph sub-graph (``wrap_node=False`` — a compiled sub-graph must NOT be function-wrapped, VT-183 /
    VT-206), ``edge_to=None`` (→ END — the sub-graph emits no campaign plan to collapse; its INTENTS
    return to the manager via the two-way handoff). The coordinator (VT-465) reads ``SPECIALIST_SPEC``
    and wires this through the roster spine — this lane adds NO graph surgery.
    """
    return build_marketing_lane_agent(model=model)


# ---------------------------------------------------------------------------
# SPECIALIST_SPEC — the declarative registration the COORDINATOR translates into a roster
# ``SpecialistSpec`` (VT-465). This lane does NOT edit ``agent/roster.py`` (a shared file owned by the
# coordinator); it EXPORTS the spec and the coordinator appends it to ``ROSTER``. The dict keys mirror
# the ``SpecialistSpec`` fields the coordinator consumes: name / agent_name / route_key / node_builder
# / description / prereq (+ the spawn_tool_name + edge_to + wrap_node + default_outcome the coordinator
# fills from these). ``prereq=None`` — v1 marketing has no activation bar of its own (a SEND it
# proposes is gated downstream by the customer-send rail's onboarded/activation gate; a SPEND by the
# business-impact gate). The lane advises freely; the rails gate the consequential effects.
# ---------------------------------------------------------------------------

SPECIALIST_SPEC: dict[str, Any] = {
    "name": "marketing",
    "agent_name": "marketing_lane",
    "route_key": "spawn_marketing",
    "node_builder": build_marketing_lane_node,
    "description": (
        "Hand off to the Marketing specialist for campaigns, seasonal/festival offers, customer "
        "segments, and marketing content drafts. Use when the desired outcome is to grow demand / "
        "run a promotion / re-engage a segment via marketing (NOT dormant-customer winback — that is "
        "the Sales Recovery specialist). The Marketing specialist DRAFTS campaigns + content and "
        "proposes sends/ad-spend as INTENTS; it never sends or spends directly — sends route through "
        "the consent/caps + decaying-checkpoint customer-send rail, ad-spend through the owner-gated "
        "business-impact gate."
    ),
    "prereq": None,
}


__all__ = [
    "MARKETING_LANE_SYSTEM_MESSAGE",
    "MARKETING_LANE_SYSTEM_PROMPT",
    "MARKETING_LANE_TOOLS",
    "MarketingLaneState",
    "SPECIALIST_SPEC",
    "build_marketing_lane_agent",
    "build_marketing_lane_node",
]
