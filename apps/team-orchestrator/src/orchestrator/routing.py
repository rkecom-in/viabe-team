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

    Precedence (§4.5 / CL-209): if the last AIMessage carries BOTH a
    spawn_sales_recovery and an escalate_to_fazal tool_call, 'spawn' wins —
    spawning the specialist is the routable action; escalation is handled
    inside the agent loop, not by this conditional edge.

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
        if tc.get("name") == "spawn_integration":
            # VT-206 — orchestrator-agent decided to hand off to the
            # Integration Agent. Same conditional-edge precedence
            # discussion as spawn_sales_recovery (Command.PARENT vs
            # explicit edge); both targets agree on goto so safe either
            # way.
            return "spawn_integration"
    return "terminal"


def orchestrator_terminal_node(state: AgentGraphState) -> dict[str, Any]:
    """Terminal node — the orchestrator finished without spawning a specialist.

    Sets terminated_without_spawn=True so downstream consumers (output composer,
    observability, billing) distinguish this from a normal spawn-and-return.
    """
    return {"terminated_without_spawn": True}


def route_after_collapse(state: AgentGraphState) -> str:
    """VT-47 — route after the collapse node persists the specialist's verdict.

    'approval_gate' — the collapse path persisted a PROPOSED campaign and
        attached ``pending_approval_request`` (a campaign send is a Pillar-7
        sensitive action that requires the owner's authoritative approval).
        Path map routes to the ``request_owner_approval`` node, which pauses
        the run via ``interrupt()`` until the owner decides.
    'end' — no approval needed (the agent declined to act: out_of_scope /
        insufficient_data, or the cohort was fail-closed rejected). The run
        completes without an owner prompt.

    Keying on ``pending_approval_request`` presence (set by collapse_node on
    the proposed-success path) keeps this decision in one place — collapse
    owns "did we propose something that needs sign-off", routing owns "go to
    the gate".
    """
    if state.get("pending_approval_request") is not None:
        return "approval_gate"
    return "end"
