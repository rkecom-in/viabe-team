"""VT-468 — the SALES specialist lane (Team-Manager rebuild, design §8).

Pins the Sales lane WITHOUT a live Anthropic call:

  1. it builds as a ``create_agent`` sub-graph (mirrors integration / onboarding) and holds NO
     send/write tool (VT-268 ``assert_agent_tools_safe``) — the lane reasons + emits INTENTS, it
     never sends; the build REFUSES a send tool;
  2. its tools produce DRAFTS / INTENTS that route through the rail — a recommendation is a
     structured intent, NOT a direct send and NOT a customer-send DB write; win-back DELEGATES to
     the existing Sales-Recovery (reused, not rebuilt);
  3. its two-way pushback seam works (the lane refuses an unwise outcome with a structured
     pushback, never a silent forced action);
  4. it exports a ``SPECIALIST_SPEC`` the coordinator can register into ROSTER in one line
     (correct shape, ``edge_to=None`` -> END, ``wrap_node=False``, no send/write in the route).
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")


# --- (1) builds as a sub-graph; holds NO send/write tool; build refuses a send tool ----------------


def test_sales_lane_holds_no_send_or_write_tool() -> None:
    from orchestrator.agent.sales_lane import SALES_LANE_TOOLS
    from orchestrator.agent.tool_guardrail import find_forbidden_tools

    # No forbidden (send / accounts-book / ledger / business-impact) capability on the surface.
    assert find_forbidden_tools(SALES_LANE_TOOLS) == []
    names = {t.name for t in SALES_LANE_TOOLS}
    assert names == {
        "recommend_sales_play",
        "identify_repeat_upsell_opportunity",
        "push_back_to_manager",
        "sales_lane_escalate_to_fazal",
    }
    # Explicit: no tool name betrays a direct-send / write capability.
    for n in names:
        low = n.lower()
        assert "send" not in low or low == "sales_lane_escalate_to_fazal"
        assert "write" not in low
        assert "draft" not in low  # the lane emits intents; drafting is the rail's (SR's) job


def test_build_sales_lane_rejects_send_tool() -> None:
    """Runtime fail-closed: handing the Sales-lane builder a send tool raises at build."""
    from langchain_core.tools import tool

    from orchestrator.agent.sales_lane import _MODEL, build_sales_lane_agent
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    @tool
    def send_whatsapp_message_evil(customer_id: str) -> str:
        """A would-be direct customer-send tool that must never reach the Sales lane."""
        return customer_id

    with pytest.raises(ToolGuardrailViolation):
        build_sales_lane_agent(_MODEL, extra_tools=[send_whatsapp_message_evil])


def test_build_sales_lane_rejects_ledger_write_tool() -> None:
    """A ledger-write tool is equally forbidden (the lane never writes customer data)."""
    from langchain_core.tools import tool

    from orchestrator.agent.sales_lane import _MODEL, build_sales_lane_agent
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    @tool
    def write_ledger_entry_evil(customer_id: str) -> str:
        """A would-be ledger-write tool that must never reach the Sales lane."""
        return customer_id

    with pytest.raises(ToolGuardrailViolation):
        build_sales_lane_agent(_MODEL, extra_tools=[write_ledger_entry_evil])


class _FakeModel:
    """Stand-in for ChatAnthropic — never invoked; only passed to node_builder + bind_tools."""

    def bind_tools(self, tools, **kwargs):  # type: ignore[no-untyped-def]
        return self


def test_sales_lane_builds_as_subgraph() -> None:
    """The node-builder returns a CompiledStateGraph sub-graph (mirrors integration/onboarding)."""
    from orchestrator.agent.sales_lane import _build_sales_lane_node

    node = _build_sales_lane_node(_FakeModel())
    # A compiled LangGraph sub-graph exposes get_graph(); a plain function would not.
    assert hasattr(node, "get_graph")


# --- (2) tools produce INTENTS through the rail — never a direct send / customer-send DB write -----


def test_recommend_play_is_an_intent_not_a_send() -> None:
    """``recommend_sales_play`` returns a structured INTENT — it does NOT send or persist a draft."""
    from orchestrator.agent.sales_lane import recommend_sales_play

    out = recommend_sales_play.func(  # type: ignore[attr-defined]
        play="repeat_purchase",
        target_framing="customers whose ~30d re-order cadence is overdue",
        reasoning="cadence ~30d, last order 52d ago",
        confidence="low",
    )
    assert out["kind"] == "sales_play_recommendation"
    assert out["play"] == "repeat_purchase"
    # An intent carries NO message_sid / sent flag / DB id — nothing was sent or persisted.
    assert "message_sid" not in out
    assert "sent" not in out
    assert "draft_id" not in out
    assert "batch_id" not in out
    # repeat_purchase is NOT a win-back, so it does not delegate to Sales-Recovery.
    assert out["delegates_to"] is None


def test_winback_play_delegates_to_sales_recovery() -> None:
    """A ``winback`` recommendation DELEGATES to the EXISTING Sales-Recovery (reused, not rebuilt)."""
    from orchestrator.agent.sales_lane import WINBACK_DELEGATES_TO, recommend_sales_play

    out = recommend_sales_play.func(  # type: ignore[attr-defined]
        play="winback",
        target_framing="lapsed high-value customers, >60d dormant",
        reasoning="p75 days-since-sale exceeded; opted-in cohort exists",
    )
    assert out["play"] == "winback"
    assert out["delegates_to"] == WINBACK_DELEGATES_TO == "sales_recovery"


def test_identify_opportunity_pushes_back_on_empty_slice() -> None:
    """No ledger slice -> not grounded (the lane must push back, never invent customer data)."""
    from orchestrator.agent.sales_lane import identify_repeat_upsell_opportunity

    empty = identify_repeat_upsell_opportunity.func(None)  # type: ignore[attr-defined]
    assert empty["grounded"] is False
    assert empty["candidate_plays"] == []

    grounded = identify_repeat_upsell_opportunity.func(  # type: ignore[attr-defined]
        {"recent_orders": 3, "cadence_days": 30}
    )
    assert grounded["grounded"] is True
    assert set(grounded["candidate_plays"]) == {"repeat_purchase", "upsell", "re_engage"}


# --- (3) two-way pushback seam ----------------------------------------------------------------------


def test_push_back_returns_structured_pushback_no_action() -> None:
    """The lane refuses an unwise outcome with a structured pushback — NOT a silent forced action."""
    from orchestrator.agent.sales_lane import push_back_to_manager

    out = push_back_to_manager.func(  # type: ignore[attr-defined]
        reason="targeted cohort has no marketing consent; a send would be blocked at the rail",
        proposed_outcome="re-engage only the opted-in subset, or collect consent first",
    )
    assert out["kind"] == "sales_lane_pushback"
    assert out["pushback"] is True
    assert out["proposed_outcome"]
    # Pushback carries NO action effect.
    assert "play" not in out
    assert "message_sid" not in out


# --- (4) SPECIALIST_SPEC — coordinator registration handle ------------------------------------------


def test_specialist_spec_shape_for_roster_registration() -> None:
    """The exported SPECIALIST_SPEC has every field the coordinator needs to append ONE ROSTER entry.

    The coordinator builds a ``SpecialistSpec`` from this dict; this test pins the shape so the
    one-line registration cannot drift from what ``roster.SpecialistSpec`` requires.
    """
    from orchestrator.agent.sales_lane import SPECIALIST_SPEC, _build_sales_lane_node

    spec = SPECIALIST_SPEC
    assert spec["name"] == "sales_lane"
    assert spec["agent_name"] == "sales_lane"
    assert spec["spawn_tool_name"] == "spawn_sales_lane"
    assert spec["route_key"] == "spawn_sales_lane"
    assert spec["node_builder"] is _build_sales_lane_node
    assert spec["edge_to"] is None  # -> END (reasoning lane, no plan to collapse)
    assert spec["wrap_node"] is False  # CompiledStateGraph — never function-wrapped
    assert spec["prereq"] == "sales_recovery"  # shares win-back's activation bar
    assert isinstance(spec["description"], str) and spec["description"]
    assert isinstance(spec["default_outcome"], str) and spec["default_outcome"]
    # The spawn tool the manager calls must not itself read as a send/write capability.
    from orchestrator.agent.tool_guardrail import FORBIDDEN_CAPABILITY_SUBSTRINGS

    low = spec["spawn_tool_name"].lower()
    assert not any(sub in low for sub in FORBIDDEN_CAPABILITY_SUBSTRINGS)


def test_specialist_spec_is_constructible_as_roster_spec() -> None:
    """The dict's keys are EXACTLY a subset of ``SpecialistSpec`` fields — so ``SpecialistSpec(**spec)``
    (minus the node-builder/update-builder callables the coordinator passes through) is valid. Proves
    the one-line registration the coordinator does will not raise on an unknown / missing field."""
    from dataclasses import fields

    from orchestrator.agent.roster import SpecialistSpec
    from orchestrator.agent.sales_lane import SPECIALIST_SPEC

    spec_field_names = {f.name for f in fields(SpecialistSpec)}
    assert set(SPECIALIST_SPEC) <= spec_field_names, (
        set(SPECIALIST_SPEC) - spec_field_names
    )
    # Construct it — every required field is present + correctly typed (no TypeError).
    built = SpecialistSpec(**SPECIALIST_SPEC)
    assert built.agent_name == "sales_lane"
    assert built.update_builder is None
    assert built.wrap_node is False
