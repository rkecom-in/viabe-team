"""Conditional routing for the supervisor graph (VT-3.4 PR 2/3 / CL-188).

``route_after_orchestrator`` decides whether the orchestrator-agent fired the
``spawn_sales_recovery`` handoff; ``orchestrator_terminal_node`` is the
no-spawn terminal sink.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

from orchestrator.observability.tm_audit import emit_tm_audit
from orchestrator.state.agent_graph_state import AgentGraphState


def route_after_orchestrator(state: AgentGraphState) -> str:
    """Return the conditional-edge key after the orchestrator node runs.

    Registry-driven (VT-465): for each spawn tool the manager's LLM may fire,
    the roster declares the conditional-edge ``route_key`` it maps to. This
    function looks up the FIRST roster spawn tool present in the last
    AIMessage's tool_calls and returns its ``route_key``; no spawn tool ->
    'terminal'. Adding a lane needs NO edit here — the new spec's
    ``spawn_tool_name -> route_key`` enters the map automatically.

    'spawn'           — spawn_sales_recovery fired; path map -> 'sales_recovery_agent'.
    'spawn_integration' — spawn_integration fired; path map -> 'integration_agent'.
    'terminal'        — no spawn tool_call; path map -> 'orchestrator_terminal'.

    Precedence (§4.5 / CL-209): if the last AIMessage carries BOTH a spawn
    tool_call and an escalate_to_fazal tool_call, the spawn wins — spawning the
    specialist is the routable action; escalation is handled inside the agent
    loop, not by this conditional edge. Tool-call order within the AIMessage
    decides which roster member wins if (rarely) two spawn tools are emitted.

    CL-183 VERIFICATION TARGET (verified in test_supervisor.py):
    Whether this function fires on the spawn path depends on langgraph's
    Command.PARENT-vs-conditional-edge precedence, which Context7 does not
    document for this composition. test_supervisor_graph_spawn_path_and_
    conditional_edge_dont_double_fire exercises it empirically. Returning the
    spawn ``route_key`` for the spawn path is safe either way — it agrees with
    the Command's goto target, or it is dead code on that path. Do not remove
    that test as "redundant".
    """
    # Local import avoids a module-load cycle (roster -> supervisor -> routing).
    from orchestrator.agent.roster import spawn_tool_route_keys

    messages = state.get("messages", [])
    latest_ai = next(
        (m for m in reversed(messages) if isinstance(m, AIMessage)),
        None,
    )
    if latest_ai is None:
        # Orchestrator never produced an AIMessage — treat as terminal.
        return "terminal"
    route_for_tool = spawn_tool_route_keys()
    tool_calls = list(getattr(latest_ai, "tool_calls", None) or [])
    for tc in tool_calls:
        route_key = route_for_tool.get(tc.get("name", ""))
        if route_key is not None:
            # VT-514 DECIDES — route_decided spine row (fail-soft, conn=None).
            emit_tm_audit(
                event_layer="decides",
                event_kind="route_decided",
                actor="team_manager",
                tenant_id=state.get("tenant_id"),
                run_id=state.get("run_id"),
                summary=f"orchestrator spawned specialist via {tc.get('name')}",
                decision={
                    "route_key": route_key,
                    "spawn_tool": tc.get("name"),
                    "tool_call_args": tc.get("args"),
                },
            )
            return route_key
    # VT-514 DECIDES — terminal route (no spawn tool fired).
    emit_tm_audit(
        event_layer="decides",
        event_kind="route_decided",
        actor="team_manager",
        tenant_id=state.get("tenant_id"),
        run_id=state.get("run_id"),
        summary="orchestrator terminated without spawning a specialist",
        decision={"route_key": "terminal", "spawn_tool": None},
    )
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


def route_after_approval(state: AgentGraphState) -> str:
    """VT-251 — route after the request_owner_approval node resolves.

    'campaign_execute' — owner_decision is 'approved'; fan out the campaign.
    'end'              — any other decision (rejected / needs_changes /
                         timeout / send_failed) or no decision set. The
                         campaign is NOT sent (Pillar 7: non-approved decisions
                         must NEVER proceed to send).

    Keying on state['owner_decision'] keeps the execute-branch strictly tied
    to the authoritative Pillar-7 gate outcome.
    """
    decision = state.get("owner_decision")
    if decision == "approved":
        return "campaign_execute"
    return "end"
