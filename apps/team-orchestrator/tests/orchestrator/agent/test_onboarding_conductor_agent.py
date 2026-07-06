"""VT-462 / VT-609 — the onboarding-conductor SPECIALIST agent (roster member + real tool surface).

Pins the conductor specialist WITHOUT a live Anthropic call:

  1. it is registered in ROSTER as a SpecialistSpec (a deterministic-floor-wrapped PLAIN function,
     VT-609 — no longer the raw CompiledStateGraph, -> END) and the supervisor graph gains its node
     + route — proving the manager can hand off to it;
  2. its tool surface is grounding (registry-bounded next question) + read/write onboarding-state
     tools + the DETERMINISTIC completion/activation checks + policy-confirmation — and holds NO
     FORBIDDEN (customer-send / accounts-book-write / ledger-write) tool (VT-268 guard) even though
     it now legitimately holds onboarding-state/policy WRITE tools (VT-609 — that is the point of
     the conversion, not a guardrail regression);
  3. the tools delegate to ``onboarding.conductor`` / ``onboarding.journey`` (no parallel logic) and
     the deterministic checks own "complete"/"activated" — the agent never self-marks either.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")


# --- (1) registered in ROSTER + wired into the supervisor graph -------------------------------------


def test_conductor_is_registered_in_roster() -> None:
    from orchestrator.agent.roster import ROSTER, get_spec

    spec = get_spec("onboarding_conductor")
    assert spec.spawn_tool_name == "spawn_onboarding_conductor"
    assert spec.route_key == "spawn_onboarding_conductor"
    # VT-609: the node is now the deterministic-floor-wrapped PLAIN function (not the raw
    # CompiledStateGraph) — it CAN be (and now is) function-wrapped with the VT-183 hook.
    assert spec.wrap_node is True
    assert spec.edge_to is None  # -> END
    assert {s.name for s in ROSTER} >= {"onboarding_conductor"}


class _FakeModel:
    """Stand-in for ChatAnthropic — never invoked; only passed to node_builder + bind_tools."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_FakeModel":
        return self


def test_supervisor_graph_gains_conductor_node_and_route() -> None:
    from orchestrator import routing
    from orchestrator.supervisor import build_supervisor_graph

    graph = build_supervisor_graph(model=_FakeModel())  # type: ignore[arg-type]
    nodes = set(graph.get_graph().nodes)
    assert "onboarding_conductor" in nodes, sorted(nodes)

    # route_after_orchestrator maps the spawn tool -> the conductor route key.
    from langchain_core.messages import AIMessage

    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"name": "spawn_onboarding_conductor", "args": {}, "id": "1"}],
            )
        ]
    }
    assert routing.route_after_orchestrator(state) == "spawn_onboarding_conductor"

    # the conditional-edge path map reaches the conductor node from the orchestrator.
    edges = graph.get_graph().edges
    targets = {e.target for e in edges if e.source == "orchestrator_agent"}
    assert "onboarding_conductor" in targets, targets


# --- (2) tool surface: grounding + deterministic check, NO send/write ------------------------------


def test_conductor_holds_no_forbidden_capability_tool() -> None:
    """VT-609 — the conductor now legitimately holds WRITE tools (record_answer / record_skip /
    apply_correction / confirm_business_policy): that is the point of the conversion, not a
    guardrail regression. The invariant that still binds is the NARROWER one VT-268 actually
    enforces — no customer-send / accounts-book-write / ledger-write tool — which none of the
    onboarding-state/policy write tools below are."""
    from orchestrator.agent.onboarding_conductor import ONBOARDING_CONDUCTOR_TOOLS
    from orchestrator.agent.tool_guardrail import find_forbidden_tools

    assert find_forbidden_tools(ONBOARDING_CONDUCTOR_TOOLS) == []
    names = {t.name for t in ONBOARDING_CONDUCTOR_TOOLS}
    assert names == {
        "read_onboarding_state",
        "extract_owner_answer",
        "record_answer",
        "record_skip",
        "apply_correction",
        "next_required_question",
        "profile_completion_check",
        "activation_check",
        "confirm_business_policy",
        "conductor_escalate_to_fazal",
    }


def test_build_conductor_rejects_send_tool() -> None:
    """Runtime fail-closed: handing the conductor builder a send tool raises at build."""
    from langchain_core.tools import tool

    from orchestrator.agent.onboarding_conductor import (
        _MODEL,
        build_onboarding_conductor_agent,
    )
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    @tool
    def send_whatsapp_message_evil(customer_id: str) -> str:
        """A would-be direct customer-send tool that must never reach the conductor."""
        return customer_id

    with pytest.raises(ToolGuardrailViolation):
        build_onboarding_conductor_agent(_MODEL, extra_tools=[send_whatsapp_message_evil])


# --- (3) tools delegate to onboarding.conductor; completion is deterministic -----------------------


def test_next_question_tool_delegates_and_returns_done_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """``next_required_question`` delegates to the conductor; a None decision -> {"done": True}.

    The tool lazily imports ``next_question_for_tenant`` from ``orchestrator.onboarding.conductor``,
    so we patch it on the SOURCE module (the lazy import resolves the patched attribute each call)."""
    from uuid import uuid4

    import orchestrator.onboarding.conductor as conductor_mod
    from orchestrator.agent.onboarding_conductor import next_required_question
    from orchestrator.onboarding.conductor import ConductorDecision
    from orchestrator.onboarding.question_brain import Question

    # A decision carrying a real next question.
    q = Question(field="city", kind="confirm", prompt_en="In Pune?", prompt_hi="पुणे में?", draft_value="Pune")
    monkeypatch.setattr(
        conductor_mod,
        "next_question_for_tenant",
        lambda tid: ConductorDecision(next_question=q, remaining=(q,), known=(), skipped=()),
    )
    out = next_required_question.func(str(uuid4()))  # type: ignore[attr-defined]
    assert out["field"] == "city"
    assert out["kind"] == "confirm"
    assert out["draft_value"] == "Pune"

    # A None decision -> done (the registry-bounded set is satisfied).
    monkeypatch.setattr(
        conductor_mod,
        "next_question_for_tenant",
        lambda tid: ConductorDecision(next_question=None, remaining=(), known=(), skipped=()),
    )
    out2 = next_required_question.func(str(uuid4()))  # type: ignore[attr-defined]
    assert out2 == {"done": True}
