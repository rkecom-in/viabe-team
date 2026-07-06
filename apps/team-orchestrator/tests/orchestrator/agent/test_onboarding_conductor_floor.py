"""VT-609 amendment A2 — the deterministic LLM-down floor.

``build_onboarding_conductor_node`` wraps the compiled specialist sub-graph's own ``.invoke()``:
when the specialist's OWN reasoning/tool-calling loop fails (an LLM call error/timeout/unparseable
output), it deterministically composes the next scripted question via
``conductor.next_question_for_tenant`` (LLM-free, pure) instead of letting the exception escape —
mirroring the VT-597 shape (a hard technical failure floors; this is NOT about classifying an
ambiguous owner reply). ``GraphBubbleUp`` (interrupt/subgraph-control signals) re-raises unchanged.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")


class _BoomGraph:
    """Stands in for the compiled sub-graph — its ``.invoke`` always raises, simulating a hard
    LLM-call failure (timeout / API error / unparseable tool loop)."""

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom — simulated specialist LLM failure")


def test_floor_composes_scripted_next_question_on_invoke_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orchestrator.agent.onboarding_conductor as conductor_mod
    from orchestrator.onboarding.conductor import ConductorDecision
    from orchestrator.onboarding.question_brain import Question

    monkeypatch.setattr(
        conductor_mod, "build_onboarding_conductor_agent", lambda model=None, **k: _BoomGraph()
    )

    q = Question(
        field="city", kind="confirm", prompt_en="In Pune?", prompt_hi="पुणे में?", draft_value="Pune"
    )
    import orchestrator.onboarding.conductor as onboarding_conductor_module

    monkeypatch.setattr(
        onboarding_conductor_module,
        "next_question_for_tenant",
        lambda tid: ConductorDecision(next_question=q, remaining=(q,), known=(), skipped=()),
    )

    node = conductor_mod.build_onboarding_conductor_node(model=None)  # type: ignore[arg-type]
    result = node({"tenant_id": uuid4(), "messages": []})

    assert len(result["messages"]) == 1
    assert result["messages"][0].content == "In Pune?"


def test_floor_reports_all_set_when_no_question_remains(monkeypatch: pytest.MonkeyPatch) -> None:
    import orchestrator.agent.onboarding_conductor as conductor_mod
    from orchestrator.onboarding.conductor import ConductorDecision

    monkeypatch.setattr(
        conductor_mod, "build_onboarding_conductor_agent", lambda model=None, **k: _BoomGraph()
    )
    import orchestrator.onboarding.conductor as onboarding_conductor_module

    monkeypatch.setattr(
        onboarding_conductor_module,
        "next_question_for_tenant",
        lambda tid: ConductorDecision(next_question=None, remaining=(), known=(), skipped=()),
    )

    node = conductor_mod.build_onboarding_conductor_node(model=None)  # type: ignore[arg-type]
    result = node({"tenant_id": uuid4(), "messages": []})

    assert result["messages"][0].content == conductor_mod._FLOOR_ALL_SET_EN


def test_floor_falls_back_to_generic_line_when_its_own_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense in depth: even if the floor's OWN next-question read fails, it never silences —
    it degrades to the generic honest-trouble line."""
    import orchestrator.agent.onboarding_conductor as conductor_mod

    monkeypatch.setattr(
        conductor_mod, "build_onboarding_conductor_agent", lambda model=None, **k: _BoomGraph()
    )
    import orchestrator.onboarding.conductor as onboarding_conductor_module

    def _boom(tid: Any) -> None:
        raise RuntimeError("the floor's own read also failed")

    monkeypatch.setattr(onboarding_conductor_module, "next_question_for_tenant", _boom)

    node = conductor_mod.build_onboarding_conductor_node(model=None)  # type: ignore[arg-type]
    result = node({"tenant_id": uuid4(), "messages": []})

    assert result["messages"][0].content == conductor_mod._FLOOR_FALLBACK_EN


def test_floor_never_silences_with_no_tenant_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """No tenant_id in state at all (a malformed dispatch) still produces a reply — never silence."""
    import orchestrator.agent.onboarding_conductor as conductor_mod

    monkeypatch.setattr(
        conductor_mod, "build_onboarding_conductor_agent", lambda model=None, **k: _BoomGraph()
    )

    node = conductor_mod.build_onboarding_conductor_node(model=None)  # type: ignore[arg-type]
    result = node({"messages": []})

    assert result["messages"][0].content == conductor_mod._FLOOR_FALLBACK_EN


def test_floor_reraises_graph_bubble_up_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interrupt / subgraph-control signals are NOT floored — the conductor calls no interrupt()
    today, but this is the same defense-in-depth carve-out supervisor._wrap_lane_node_exceptions
    uses, so a future interrupt still propagates correctly."""
    from langgraph.errors import GraphInterrupt

    import orchestrator.agent.onboarding_conductor as conductor_mod

    class _InterruptingGraph:
        def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
            raise GraphInterrupt()

    monkeypatch.setattr(
        conductor_mod, "build_onboarding_conductor_agent", lambda model=None, **k: _InterruptingGraph()
    )
    node = conductor_mod.build_onboarding_conductor_node(model=None)  # type: ignore[arg-type]
    with pytest.raises(GraphInterrupt):
        node({"tenant_id": uuid4(), "messages": []})


def test_floor_does_not_engage_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """The common path: the sub-graph succeeds -> its own result passes through untouched."""
    import orchestrator.agent.onboarding_conductor as conductor_mod

    class _HealthyGraph:
        def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
            return {"messages": ["real specialist reply"]}

    monkeypatch.setattr(
        conductor_mod, "build_onboarding_conductor_agent", lambda model=None, **k: _HealthyGraph()
    )
    node = conductor_mod.build_onboarding_conductor_node(model=None)  # type: ignore[arg-type]
    result = node({"tenant_id": uuid4(), "messages": []})
    assert result == {"messages": ["real specialist reply"]}
