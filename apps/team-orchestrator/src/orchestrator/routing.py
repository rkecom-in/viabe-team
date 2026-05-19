"""Conditional routing for the supervisor graph (VT-3.4 PR 2/3 / CL-188).

``route_after_orchestrator`` decides whether the orchestrator-agent fired the
``spawn_sales_recovery`` handoff; ``orchestrator_terminal_node`` is the
no-spawn terminal sink.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

from orchestrator.state.agent_graph_state import AgentGraphState


def route_after_orchestrator(state: AgentGraphState) -> str:
    """Return the conditional-edge key after the orchestrator node runs.

    'spawn'    — the last AIMessage carries a spawn_sales_recovery tool_call;
                 path map routes to 'sales_recovery_agent'.
    'terminal' — no spawn tool_call; path map routes to 'orchestrator_terminal'.

    CL-183 VERIFICATION TARGET (verified in test_supervisor.py):
    Whether this function fires on the spawn path depends on langgraph's
    Command.PARENT-vs-conditional-edge precedence, which Context7 does not
    document for this composition. test_supervisor_graph_spawn_path_and_
    conditional_edge_dont_double_fire exercises it empirically. Returning
    'spawn' for the spawn path is safe either way — it agrees with the
    Command's goto target, or it is dead code on that path. Do not remove
    that test as "redundant".
    """
    messages = state.get("messages", [])
    latest_ai = next(
        (m for m in reversed(messages) if isinstance(m, AIMessage)),
        None,
    )
    if latest_ai is None:
        # Orchestrator never produced an AIMessage — treat as terminal.
        return "terminal"
    tool_calls = list(getattr(latest_ai, "tool_calls", None) or [])
    for tc in tool_calls:
        if tc.get("name") == "spawn_sales_recovery":
            return "spawn"
    return "terminal"


def orchestrator_terminal_node(state: AgentGraphState) -> dict[str, Any]:
    """Terminal node — the orchestrator finished without spawning a specialist.

    Sets terminated_without_spawn=True so downstream consumers (output composer,
    observability, billing) distinguish this from a normal spawn-and-return.
    """
    return {"terminated_without_spawn": True}
