"""VT-611 Phase B1 #2 — graph tests: every specialist returns to manager_review.

The promotion-gate ask, verbatim: "graph tests (all three specialists return to manager_review)."

MODE-CONDITIONAL, not unconditional (Cowork steer): the invariant "no specialist reaches a
terminal without manager_review" holds ONLY in ``enforce`` mode. ``build_supervisor_graph``'s
``legacy``/``shadow`` shapes are BYTE-IDENTICAL to pre-VT-606 (VT-606 Package 3's own acceptance,
pinned in ``test_supervisor_loop_mode.py``) — every roster specialist still routes DIRECTLY to its
``spec.edge_to``/END, and ``manager_review`` isn't even a node. Production defaults to legacy today
(VT-611 pre-work / the program doc), so an unconditional "specialist -> manager_review always"
assertion would be simply FALSE against the running default — this file does NOT write one.

This is VT-611's OWN pin on that invariant (self-contained promotion-gate evidence), reading the
roster DYNAMICALLY (``roster.ROSTER``, not hard-coded names) so it stays correct if the roster ever
grows past the 3 specialists VT-604 fixed it at — see ``test_supervisor_loop_mode.py`` for the
byte-identical VT-606 regression lock this mirrors and extends with the dynamic-roster read.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

from orchestrator.agent.roster import ROSTER  # noqa: E402
from orchestrator.supervisor import build_supervisor_graph  # noqa: E402


class _FakeModel:
    def bind_tools(self, tools: Any, **kwargs: Any) -> "_FakeModel":
        return self


def _build(mode: str) -> Any:
    return build_supervisor_graph(model=_FakeModel(), mode=mode)  # type: ignore[arg-type]


def test_roster_is_exactly_three_specialists() -> None:
    """VT-604 fixed the roster at exactly 3 live spawnable specialists — this file's other
    assertions are only meaningful load-bearing gates if that premise still holds."""
    assert {spec.agent_name for spec in ROSTER} == {
        "sales_recovery_agent", "integration_agent", "onboarding_conductor",
    }


def test_enforce_routes_every_roster_specialist_to_manager_review() -> None:
    """The enforce-mode half of the invariant: EVERY roster specialist (read dynamically, not
    hard-coded) has an outgoing edge to manager_review and NONE of its old direct edges survive."""
    graph = _build("enforce")
    nodes = set(graph.get_graph().nodes)
    assert "manager_review" in nodes

    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    for spec in ROSTER:
        assert (spec.agent_name, "manager_review") in edges, (
            f"{spec.agent_name} does not route to manager_review in enforce mode"
        )
        old_target = spec.edge_to if spec.edge_to is not None else "__end__"
        assert (spec.agent_name, old_target) not in edges, (
            f"{spec.agent_name} still has its OLD direct edge to {old_target!r} in enforce mode "
            "— manager_review must be the ONLY route out"
        )

    # manager_review is the sole gate to a terminal: it may route to collapse (a campaign_plan
    # was produced) or straight to END — nothing else.
    review_targets = {t for s, t in edges if s == "manager_review"}
    assert review_targets == {"collapse", "__end__"}


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_legacy_and_shadow_specialists_bypass_manager_review(mode: str) -> None:
    """The invariant's OTHER half: in legacy/shadow, manager_review isn't even wired in — every
    roster specialist routes DIRECTLY to its own edge_to/END, exactly as before VT-606. This is
    NOT a bug to fix in B1; it's the mode-conditional shape the loop is DESIGNED to have until
    Fazal authorizes the enforce promotion (this row's own gate)."""
    graph = _build(mode)
    nodes = set(graph.get_graph().nodes)
    assert "manager_review" not in nodes

    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    for spec in ROSTER:
        target = spec.edge_to if spec.edge_to is not None else "__end__"
        assert (spec.agent_name, target) in edges, (
            f"{spec.agent_name} does not route directly to {target!r} in {mode} mode"
        )
        assert (spec.agent_name, "manager_review") not in edges
