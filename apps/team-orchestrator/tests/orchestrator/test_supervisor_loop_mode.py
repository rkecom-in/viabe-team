"""VT-606 (Loop Package 3) — ``build_supervisor_graph``'s ``TEAM_MANAGER_LOOP_MODE`` gate.

Package 3 acceptance, verbatim: legacy-mode graph is byte-identical to pre-VT-606; only enforce
changes the shape (specialist -> manager_review, not straight to collapse/END). Amendment A1:
shadow's LIVE graph must ALSO stay legacy-shaped (its own observational pass is separate, additive
code — never the graph the owner's turn is dispatched through).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

from orchestrator.supervisor import build_supervisor_graph  # noqa: E402


class _FakeModel:
    def bind_tools(self, tools: Any, **kwargs: Any) -> "_FakeModel":
        return self


# The EXACT pre-VT-606 node/edge shape (test_roster_registry.py /
# test_existing_graph_shape_unchanged_by_roster_refactor pins the same facts) — repeated here as
# the explicit VT-606 regression lock.
_PRE_VT606_NODES = frozenset({
    "__start__", "__end__", "orchestrator_agent", "sales_recovery_agent", "integration_agent",
    "onboarding_conductor", "collapse", "orchestrator_terminal", "request_owner_approval",
    "campaign_execute",
})


def _build(mode: str) -> Any:
    return build_supervisor_graph(model=_FakeModel(), mode=mode)  # type: ignore[arg-type]


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_legacy_and_shadow_graph_is_byte_identical_to_pre_vt606(mode: str) -> None:
    graph = _build(mode)
    nodes = set(graph.get_graph().nodes)
    assert nodes == _PRE_VT606_NODES, sorted(nodes)
    assert "manager_review" not in nodes

    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    assert ("sales_recovery_agent", "collapse") in edges
    assert ("integration_agent", "__end__") in edges
    assert ("onboarding_conductor", "__end__") in edges


def test_enforce_adds_manager_review_and_reroutes_every_specialist() -> None:
    graph = _build("enforce")
    nodes = set(graph.get_graph().nodes)
    assert "manager_review" in nodes
    # Every pre-existing node is STILL present — enforce ADDS a node/reroutes edges, it doesn't
    # remove anything else.
    assert _PRE_VT606_NODES <= nodes

    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    for specialist in ("sales_recovery_agent", "integration_agent", "onboarding_conductor"):
        assert (specialist, "manager_review") in edges
        # NONE of the OLD direct edges survive in enforce mode.
        assert (specialist, "collapse") not in edges
        assert (specialist, "__end__") not in edges

    # manager_review's own routing reaches both its possible targets.
    review_targets = {t for s, t in edges if s == "manager_review"}
    assert review_targets == {"collapse", "__end__"}


def test_default_mode_reads_the_env_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "enforce")
    graph = build_supervisor_graph(model=_FakeModel())  # type: ignore[arg-type]
    assert "manager_review" in set(graph.get_graph().nodes)


def test_explicit_mode_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "enforce")
    graph = _build("legacy")  # explicit param wins over the env
    assert "manager_review" not in set(graph.get_graph().nodes)
