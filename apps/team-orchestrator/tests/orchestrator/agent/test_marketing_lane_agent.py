"""VT-469 — the MARKETING SPECIALIST lane (roster member + tool surface + rail-facing intents).

Pins the marketing specialist WITHOUT a live Anthropic call:

  1. it EXPORTS a ``SPECIALIST_SPEC`` the coordinator translates into a roster ``SpecialistSpec``
     (CompiledStateGraph sub-graph, -> END) — the lane adds NO graph surgery (the VT-465 spine);
  2. its tool surface is marketing REASONING (advise — campaign/offer/segment/content drafting) +
     RAIL-FACING INTENT checks — and holds NO send/spend tool (VT-268 guard); build raises if one is
     added;
  3. the rail-facing tools DELEGATE to the EXISTING deterministic rails (no parallel logic): a
     send intent → ``business_policy.assert_within_policy`` (CUSTOMER_SEND), an ad-spend intent →
     ``business_impact_choke.assert_or_gate_business_action`` (SPEND) — the rail decides, not the brain;
  4. (conditional) once the coordinator has registered the lane in ROSTER, the supervisor graph gains
     its node + route — proving the manager can hand off to it.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")


# --- (1) the exported SPECIALIST_SPEC the coordinator registers ------------------------------------


def test_specialist_spec_shape() -> None:
    from orchestrator.agent.marketing_lane import SPECIALIST_SPEC, build_marketing_lane_node

    # The keys the coordinator (VT-465) consumes to build a roster SpecialistSpec.
    assert SPECIALIST_SPEC["name"] == "marketing"
    assert SPECIALIST_SPEC["agent_name"] == "marketing_lane"
    assert SPECIALIST_SPEC["route_key"] == "spawn_marketing"
    assert SPECIALIST_SPEC["node_builder"] is build_marketing_lane_node
    assert SPECIALIST_SPEC["prereq"] is None  # v1: no activation bar of its own (rails gate effects)
    assert isinstance(SPECIALIST_SPEC["description"], str) and SPECIALIST_SPEC["description"]


def test_node_builder_returns_compiled_subgraph() -> None:
    """The node_builder yields a compiled sub-graph (parity with integration / onboarding-conductor
    nodes — wrap_node=False territory). It must not raise (the VT-268 build guard passes)."""
    from orchestrator.agent.marketing_lane import build_marketing_lane_node

    node = build_marketing_lane_node(_FakeModel())
    # A compiled langgraph graph exposes get_graph(); a plain callable would not.
    assert hasattr(node, "get_graph")


class _FakeModel:
    """Stand-in for ChatAnthropic — never invoked; only passed to node_builder + bind_tools."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_FakeModel":
        return self


# --- (2) tool surface: advise + rail-facing intents, NO send/spend ---------------------------------


def test_marketing_holds_no_send_or_spend_tool() -> None:
    from orchestrator.agent.marketing_lane import MARKETING_LANE_TOOLS
    from orchestrator.agent.tool_guardrail import find_forbidden_tools

    # VT-268: no forbidden capability (send / spend / commit / config / ledger / sheet write).
    assert find_forbidden_tools(MARKETING_LANE_TOOLS) == []
    names = {t.name for t in MARKETING_LANE_TOOLS}
    assert names == {
        "list_recent_campaigns",
        "draft_campaign_plan",
        "draft_content",
        "check_send_intent",
        "check_ad_spend_intent",
        "marketing_escalate_to_fazal",
    }


def test_build_marketing_rejects_send_tool() -> None:
    """Runtime fail-closed: handing the marketing builder a send tool raises at build."""
    from langchain_core.tools import tool

    from orchestrator.agent.marketing_lane import _MODEL, build_marketing_lane_agent
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    @tool
    def send_whatsapp_template_evil(customer_id: str) -> str:
        """A would-be direct customer-send tool that must never reach the marketing specialist."""
        return customer_id

    with pytest.raises(ToolGuardrailViolation):
        build_marketing_lane_agent(_MODEL, extra_tools=[send_whatsapp_template_evil])


def test_build_marketing_rejects_spend_tool() -> None:
    """Runtime fail-closed: a direct SPEND tool also raises (VT-467 business-impact capability)."""
    from langchain_core.tools import tool

    from orchestrator.agent.marketing_lane import _MODEL, build_marketing_lane_agent
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    @tool
    def execute_spend_on_boost(magnitude_minor: int) -> str:
        """A would-be direct ad-spend tool that must never reach the marketing specialist."""
        return str(magnitude_minor)

    with pytest.raises(ToolGuardrailViolation):
        build_marketing_lane_agent(_MODEL, extra_tools=[execute_spend_on_boost])


# --- (3) advise tools produce INTENTS (no side effect) ---------------------------------------------


def test_draft_campaign_plan_is_intent_only() -> None:
    from uuid import uuid4

    from orchestrator.agent.marketing_lane import draft_campaign_plan

    out = draft_campaign_plan.func(  # type: ignore[attr-defined]
        tenant_id=str(uuid4()),
        objective="re-engage festival crowd",
        segment_label="diwali_buyers",
        offer_summary="15% off Diwali week",
        message_draft="Happy Diwali! 15% off this week.",
    )
    assert out["kind"] == "campaign_plan"
    assert out["segment_label"] == "diwali_buyers"
    # An intent carries NO recipient / phone / customer id (CL-390) and no send result.
    assert "message_sid" not in out
    assert "customer_id" not in out


def test_draft_content_is_advisory_only() -> None:
    from uuid import uuid4

    from orchestrator.agent.marketing_lane import draft_content

    out = draft_content.func(  # type: ignore[attr-defined]
        tenant_id=str(uuid4()),
        content_type="festival_greeting",
        brief="warm Diwali greeting + soft offer",
        draft="Wishing you a bright Diwali from us!",
    )
    assert out["kind"] == "content_draft"
    assert "message_sid" not in out  # advisory copy, not a send


# --- (4) rail-facing intents DELEGATE to the deterministic rails (the rail decides) ----------------


def test_check_send_intent_delegates_to_policy_rail(monkeypatch: pytest.MonkeyPatch) -> None:
    """``check_send_intent`` consults ``business_policy.assert_within_policy`` for CUSTOMER_SEND —
    it REPORTS the deterministic decision; it does NOT send. Patch the rail on its SOURCE module (the
    tool lazily imports it, so the patched attr resolves per call)."""
    from uuid import uuid4

    import orchestrator.agents.business_policy as policy_mod
    from orchestrator.agent.marketing_lane import check_send_intent
    from orchestrator.agents.business_policy import PolicyActionClass, PolicyCheck, PolicyDecision

    seen: dict[str, Any] = {}

    def _fake_assert(tenant_id: Any, action_class: Any, action_attrs: Any = None, *, conn: Any = None) -> PolicyCheck:
        seen["action_class"] = action_class
        seen["attrs"] = action_attrs
        return PolicyCheck(
            decision=PolicyDecision.OUT_OF_POLICY, reason="segment_not_allowed",
            action_class=PolicyActionClass.CUSTOMER_SEND.value,
        )

    monkeypatch.setattr(policy_mod, "assert_within_policy", _fake_assert)
    out = check_send_intent.func(  # type: ignore[attr-defined]
        tenant_id=str(uuid4()), segment_label="vip", frequency_cap_key="marketing_weekly", period_count=2,
    )
    # Routed through the CUSTOMER_SEND policy class with the segment + freq attrs.
    assert seen["action_class"] is PolicyActionClass.CUSTOMER_SEND
    assert seen["attrs"]["segment"] == "vip"
    assert seen["attrs"]["frequency_cap_key"] == "marketing_weekly"
    assert seen["attrs"]["period_count"] == 2
    # The rail's decision is reported faithfully (the brain does not override it).
    assert out["in_policy"] is False
    assert out["reason"] == "segment_not_allowed"


def test_check_ad_spend_intent_delegates_to_business_impact_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """``check_ad_spend_intent`` routes the SPEND magnitude through
    ``business_impact_choke.assert_or_gate_business_action`` — the rail decides autonomous-vs-approval;
    it does NOT spend. Patch the gate on its SOURCE module (lazy import)."""
    from uuid import uuid4

    import orchestrator.agents.business_impact_choke as choke_mod
    from orchestrator.agent.marketing_lane import check_ad_spend_intent
    from orchestrator.agents.business_impact_choke import (
        BusinessActionDecision,
        BusinessActionGate,
        BusinessImpactClass,
    )

    seen: dict[str, Any] = {}

    def _fake_gate(tenant_id: Any, action_class: Any, magnitude_minor: int, *, action_attrs: Any = None, conn: Any = None) -> BusinessActionGate:
        seen["action_class"] = action_class
        seen["magnitude_minor"] = magnitude_minor
        seen["attrs"] = action_attrs
        return BusinessActionGate(
            decision=BusinessActionDecision.REQUIRES_OWNER_APPROVAL, reason="always_approve_tier",
            action_class=BusinessImpactClass.SPEND.value, magnitude_minor=magnitude_minor, tier="always_approve",
        )

    monkeypatch.setattr(choke_mod, "assert_or_gate_business_action", _fake_gate)
    out = check_ad_spend_intent.func(  # type: ignore[attr-defined]
        tenant_id=str(uuid4()), magnitude_minor=50000, purpose="boost the Diwali post",
    )
    # Routed through the SPEND business-impact class with the policy attrs carried.
    assert seen["action_class"] is BusinessImpactClass.SPEND
    assert seen["magnitude_minor"] == 50000
    assert seen["attrs"] == {"magnitude_minor": 50000}
    # The rail's decision is reported faithfully — an always-approve tenant requires owner approval.
    assert out["decision"] == "requires_owner_approval"
    assert out["requires_owner_approval"] is True
    assert out["magnitude_minor"] == 50000


# --- (5) conditional: once the COORDINATOR registers the lane, the supervisor graph wires it --------


def test_supervisor_graph_gains_marketing_node_when_registered() -> None:
    """If the coordinator has registered the marketing SPECIALIST_SPEC in ROSTER, the supervisor graph
    gains the marketing node + route — proving the manager can hand off. SKIPPED until the coordinator
    registers it (this lane does NOT edit the shared roster.py — VT-469 builds the lane; the coordinator
    wires it)."""
    from orchestrator.agent.roster import ROSTER

    if "marketing" not in {s.name for s in ROSTER}:
        pytest.skip("marketing lane not yet registered in ROSTER by the coordinator")

    from orchestrator import routing
    from orchestrator.agent.roster import get_spec
    from orchestrator.supervisor import build_supervisor_graph

    spec = get_spec("marketing_lane")
    assert spec.route_key == "spawn_marketing"
    assert spec.wrap_node is False  # CompiledStateGraph — never function-wrapped
    assert spec.edge_to is None  # -> END

    graph = build_supervisor_graph(model=_FakeModel())  # type: ignore[arg-type]
    nodes = set(graph.get_graph().nodes)
    assert "marketing_lane" in nodes, sorted(nodes)

    from langchain_core.messages import AIMessage

    state = {
        "messages": [
            AIMessage(content="", tool_calls=[{"name": "spawn_marketing", "args": {}, "id": "1"}])
        ]
    }
    assert routing.route_after_orchestrator(state) == "spawn_marketing"
