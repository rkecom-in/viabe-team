"""VT-3.4 PR 2/3 — supervisor conditional-routing tests (§4.5).

Pure-Python: exercises ``route_after_orchestrator`` (the conditional-edge key
function) and ``orchestrator_terminal_node`` (the no-spawn sink). No LLM, no DB.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core")

from langchain_core.messages import AIMessage, ToolCall

from orchestrator.routing import (
    orchestrator_terminal_node,
    route_after_collapse,
    route_after_orchestrator,
)
from orchestrator.state.agent_graph_state import AgentGraphState


def _route(tool_calls: list[ToolCall]) -> str:
    """Run route_after_orchestrator over a state whose last AIMessage carries
    ``tool_calls``."""
    state = AgentGraphState(
        messages=[AIMessage(content="", tool_calls=tool_calls)]
    )
    return route_after_orchestrator(state)


def test_route_spawn_tool_call_returns_spawn() -> None:
    """§4.5 case 1 — a spawn_sales_recovery tool_call routes to 'spawn'."""
    assert _route([{"name": "spawn_sales_recovery", "args": {}, "id": "1"}]) == "spawn"


def test_route_no_tool_calls_returns_terminal() -> None:
    """§4.5 case 2 — an AIMessage with no tool_calls routes to 'terminal'."""
    assert _route([]) == "terminal"


def test_route_escalate_only_returns_terminal() -> None:
    """§4.5 case 3 — escalate_to_fazal is not a spawn; routes to 'terminal'."""
    assert _route([{"name": "escalate_to_fazal", "args": {}, "id": "1"}]) == "terminal"


# --- VT-47: route_after_collapse (the owner-approval gate routing) -----------


def test_route_after_collapse_with_pending_request_goes_to_gate() -> None:
    """A persisted proposed campaign attaches pending_approval_request ->
    route to the request_owner_approval gate node."""
    state = AgentGraphState(
        pending_approval_request={"approval_type": "campaign_send"}
    )
    assert route_after_collapse(state) == "approval_gate"


def test_route_after_collapse_without_request_goes_to_end() -> None:
    """No approval request (refusal / defer / fail-closed rejection) -> END."""
    assert route_after_collapse(AgentGraphState()) == "end"
    assert route_after_collapse(AgentGraphState(campaign_rejected={})) == "end"


def test_route_spawn_and_escalate_returns_spawn() -> None:
    """§4.5 case 4 — spawn_sales_recovery + escalate_to_fazal together: 'spawn'
    wins (precedence documented in routing.py)."""
    assert _route(
        [
            {"name": "escalate_to_fazal", "args": {}, "id": "1"},
            {"name": "spawn_sales_recovery", "args": {}, "id": "2"},
        ]
    ) == "spawn"


def test_orchestrator_terminal_node_sets_terminated_flag() -> None:
    """§4.5 — the terminal node marks terminated_without_spawn=True."""
    assert orchestrator_terminal_node(AgentGraphState()) == {
        "terminated_without_spawn": True
    }
