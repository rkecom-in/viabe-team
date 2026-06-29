"""Parent multi-agent StateGraph wiring (VT-3.4 PR 1/3 + 2/3, dispatch-switch
VT-SalesRecovery-Agent Exec Order 6.7).

Per CL-175: built manually instead of using ``langgraph_supervisor.create_supervisor``.
``orchestrator_agent`` IS the supervisor (CL-22). Specialists are routed-to via
custom handoff tools returning ``Command(goto=..., graph=Command.PARENT)``.

PR 2/3 (CL-188): adds an explicit conditional edge after the orchestrator —
``route_after_orchestrator`` sends the spawn case to ``sales_recovery_agent``
and the no-spawn case to the ``orchestrator_terminal`` sink. Also accepts an
optional ``checkpointer``.

CL-183 VERIFICATION TARGET (verified in test_supervisor.py):
``Command.PARENT`` from the spawn tool vs the ``add_conditional_edges`` after
the orchestrator node — the precedence of these two is NOT documented in
Context7 for this composition. The landmine test exercises both paths and
asserts the observed behaviour. Do not remove it as "redundant".

Dispatch switch (this commit): the ``sales_recovery_agent`` node now calls
``run_sales_recovery_agent`` (VT-32) instead of the langchain ``create_agent``
stub. The self-evaluate gate (VT-36 + VT-50 + the VT-SR-Agent wiring) is
construction-injected via a per-run ``SelfEvaluateAdapter`` and becomes
PRODUCTION-LOAD-BEARING with this PR. The stub module remains on disk for
out-of-graph callers (tests, future replay tooling) but is no longer on the
dispatch path.

Module-level node (NOT a closure) so tests can ``monkeypatch.setattr(supervisor_mod,
"_sales_recovery_node", ...)`` the same way collapse_node is patched in the
landmine routing tests.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError
from team_shared.mcp import ToolContext

from orchestrator._tenant_guard import TenantIsolationError
from orchestrator.agent.limits.wallclock_timer import WALL_CLOCK_HARD_LIMIT_S
from orchestrator.agent.orchestrator_agent import build_orchestrator_agent
from orchestrator.agent.sales_recovery import run_sales_recovery_agent
from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan
from orchestrator.agent.tools.request_owner_approval import (
    request_owner_approval_node,
)
from orchestrator.agent.tools.self_evaluate import SelfEvaluateAdapter
from orchestrator.agent.roster import roster_spawn_tools
from orchestrator.collapse import collapse_node
from orchestrator.db import tenant_connection
from orchestrator.routing import (
    orchestrator_terminal_node,
    route_after_approval,
    route_after_collapse,
    route_after_orchestrator,
)
from orchestrator.state.agent_graph_state import AgentGraphState


# Per-run budgets sourced from VT-35's hard-limit constants. Matched to the
# values agent/sales_recovery_node.py uses for the standalone-node path so
# the supervisor dispatch and the standalone wrapper give the gate the same
# context shape.
_RUN_COST_BUDGET_PAISE = 5_000  # ₹50 per VT-35
_RUN_WALLCLOCK_BUDGET_MS = int(WALL_CLOCK_HARD_LIMIT_S * 1000)


class SpecialistNoOutputError(RuntimeError):
    """A specialist dispatch terminated with NO usable output (VT-492).

    Raised by a specialist node when ``run_<specialist>_agent`` returns a
    terminal ``AgentResult`` whose ``output`` is None — the live-agent
    failure modes ``status in {refused, invalid, terminated}`` (e.g. a
    post-REVISE retry emits non-dict terminal text, classified
    ``agent_terminal_no_dict``). The agent ALREADY routed its own
    ``FailureRecord`` (the failure stays observable); this exception is the
    CONTROL signal that lets ``dispatch_brain`` convert the dead-end into a
    CLEAN ``escalated`` terminal — instead of letting a bare ``RuntimeError``
    escape ``graph.invoke`` → ``dispatch_brain``'s catch-all re-raise →
    ``webhook_pipeline_run`` skip ``close_webhook_run`` → the run ORPHAN at
    ``status='running'`` until the VT-481 reaper (hours later).

    Mirrors the ``HardLimitExceeded`` clean-terminal pattern (a structured
    exception the dispatch boundary maps to a known final_status) and the
    VT-484 convert-don't-orphan principle. PII-safe fields only — specialist
    name + the terminal ``status`` + run / tenant ids (NO owner body, NO
    draft).
    """

    def __init__(
        self,
        *,
        specialist: str,
        status: str,
        run_id: UUID,
        tenant_id: UUID,
    ) -> None:
        self.specialist = specialist
        self.status = status
        self.run_id = run_id
        self.tenant_id = tenant_id
        super().__init__(
            f"{specialist}_node: agent returned status={status!r} with no "
            f"output (FailureRecord already routed if applicable; "
            f"run={run_id} tenant={tenant_id})"
        )


def _sales_recovery_node(state: AgentGraphState) -> dict[str, Any]:
    """The supervisor's specialist-dispatch node.

    Calls ``run_sales_recovery_agent`` (VT-32) — the REAL agent loop on the
    Anthropic Messages SDK with the self-evaluate gate active (VT-36, made
    structural by VT-SR-Agent gate wiring; backed by VT-50's Opus evaluator).

    Exec-6.85: consumes the Context Composer bundle from
    ``state['sales_recovery_context']`` directly. The bundle is attached by
    ``spawn_sales_recovery``'s ``_build_sales_recovery_update`` (handoffs.py)
    and now carries the full task context — tenant identity, run identity,
    user_request, trigger_reason, plus the per-section data the Composer
    assembled. Fail loud if the bundle is missing: a None bundle at this
    seam means the handoff is broken (TenantIsolationError-style).

    Parse exception handling (CL-238 + VT-494): catches only
    ``(json.JSONDecodeError, ValidationError)`` — narrow by design. A
    ``ValidationError`` here means the live agent emitted a non-None but
    malformed ``CampaignPlan`` (the CL-288 coerced variant dict that then
    FAILED ``parse_campaign_plan`` at the agent's gate seam — e.g. the VT-493
    backdated ``campaign_window`` or an off-enum ``source_kind``). The agent
    already routed its ``FailureRecord`` (``agent_schema_rejection``), so the
    bug stays observable; re-parsing here raises the SAME error. VT-494: do
    NOT let the bare ``ValidationError`` escape — it would unwind
    ``graph.invoke`` → ``dispatch_brain``'s catch-all re-raise →
    ``webhook_pipeline_run`` skips ``close_webhook_run`` → the run ORPHANS at
    ``status='running'`` until the VT-481 reaper. Instead convert it to the
    SAME structured ``SpecialistNoOutputError`` the output=None path uses
    (VT-492), so ``dispatch_brain`` maps it to a CLEAN ``escalated`` terminal +
    the VT-88 SupportBot acks the owner. This is the VT-484 convert-don't-orphan
    principle, not a silent swallow.
    """
    context = state.get("sales_recovery_context")
    if context is None:
        raise TenantIsolationError(
            "sales_recovery_node: state['sales_recovery_context'] is None —"
            " spawn_sales_recovery must attach the Context Composer bundle"
            " (handoffs._build_sales_recovery_update). A missing bundle"
            " means the specialist would run against no task context."
        )

    tenant_uuid = context.tenant_id
    run_uuid = context.run_id

    # Per-invocation ToolContext + adapter — the gate runs against a real
    # SelfEvaluateAdapter (Opus-backed by VT-50). Production-load-bearing
    # path activates here.
    tool_ctx = ToolContext(
        tenant_id=tenant_uuid,
        run_id=run_uuid,
        agent_id="sales_recovery",
        parent_tool_call_id=None,
        cost_budget_remaining_paise=_RUN_COST_BUDGET_PAISE,
        wallclock_remaining_ms=_RUN_WALLCLOCK_BUDGET_MS,
        db_handle=tenant_connection,
    )
    evaluator = SelfEvaluateAdapter(ctx=tool_ctx)

    # VT-73 PRE-FLIGHT context isolation: independently re-query every per-tenant
    # id in the bundle against context.tenant_id BEFORE the specialist sees it
    # (defense-in-depth over the builders' RLS reads). Raises
    # ContextIsolationViolation (critical + Detector-1 alert) on any cross-tenant id.
    from orchestrator.context_validator import validate_context_isolation

    validate_context_isolation(context)

    agent_result = run_sales_recovery_agent(context, evaluator=evaluator)

    if agent_result.output is None:
        # Live-agent terminal failure modes (status in {refused, invalid,
        # terminated}) produce no output. The agent's own emit calls
        # routed a FailureRecord; the supervisor surfaces the failure
        # rather than synthesising a fallback plan (CL-238 — the brief's
        # "real error, not silent fallback").
        #
        # VT-492: raise the STRUCTURED SpecialistNoOutputError (NOT a bare
        # RuntimeError). dispatch_brain catches it and maps it to a CLEAN
        # 'escalated' terminal so the run reaches a terminal status + the
        # VT-88 SupportBot acks the owner — a bare raise would orphan the run
        # at status='running' (the original VT-492 defect: the raise escaped
        # before close_webhook_run, leaving the run stuck until the VT-481
        # reaper). The invalid output stays observable via the agent's
        # already-routed FailureRecord — this does not mask the real bug.
        raise SpecialistNoOutputError(
            specialist="sales_recovery",
            status=str(agent_result.status),
            run_id=run_uuid,
            tenant_id=tenant_uuid,
        )

    # Tight exception handling — narrow catch on parse failure. A
    # ValidationError surfacing here means the live agent emitted a non-None
    # but malformed CampaignPlan (e.g. VT-493's backdated campaign_window /
    # off-enum source_kind that failed parse at the agent's gate seam). That
    # is a real bug — the agent already routed its FailureRecord, so it stays
    # observable — but it must NOT escape as a bare raise (VT-494): a bare
    # ValidationError unwinds graph.invoke → dispatch_brain's catch-all →
    # the run orphans at status='running' until the VT-481 reaper. Convert it
    # to the structured SpecialistNoOutputError (the same control signal the
    # output=None path uses, VT-492) so dispatch_brain resolves a CLEAN
    # 'escalated' terminal and the owner gets the VT-88 no-silence ack.
    try:
        plan = parse_campaign_plan(agent_result.output)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise SpecialistNoOutputError(
            specialist="sales_recovery",
            status=str(agent_result.status),
            run_id=run_uuid,
            tenant_id=tenant_uuid,
        ) from exc

    overrides: dict[str, Any] = {}
    if plan.tenant_id != tenant_uuid:
        overrides["tenant_id"] = tenant_uuid
    if plan.run_id != run_uuid:
        overrides["run_id"] = run_uuid
    if overrides:
        plan = plan.model_copy(update=overrides)

    return {"campaign_plan": plan}


def _campaign_execute_node(state: AgentGraphState) -> dict[str, Any]:
    """VT-251 — fan out the approved campaign to all recipients.

    Called only when owner_decision == 'approved' (routed by route_after_approval).
    Reads campaign_id from state['pending_approval_request']['campaign_id'],
    opens a tenant-scoped connection, and calls execute_approved_campaign.

    Returns execution summary (counts only, CL-390 no PII) as
    state['campaign_execution_summary']. On error, surfaces the exception
    message as state['campaign_execution_error'] and does NOT re-raise (the
    graph run completes; the error is observable via pipeline_steps / logs).

    D2 (Cowork ruling 2026-05-31): attribution is NOT computed here — it is
    deferred to the VT-176 async close trigger.
    """
    from orchestrator.campaign.execute import execute_approved_campaign

    tenant_id = state.get("tenant_id")
    if tenant_id is None:
        raise RuntimeError(
            "_campaign_execute_node: tenant_id missing from state — "
            "the graph entry point must set it"
        )

    approval_req = state.get("pending_approval_request") or {}
    campaign_id = approval_req.get("campaign_id")
    if campaign_id is None:
        raise RuntimeError(
            "_campaign_execute_node: pending_approval_request['campaign_id'] "
            "is missing — collapse must have attached it before routing to "
            "the approval gate"
        )

    tenant_id_str = str(tenant_id)
    campaign_id_str = str(campaign_id)

    # VT-374 — run-control pause at the send boundary (supersedes the VT-300 run_controls
    # consume; N1 RETIRE arm, mig 131 drops the table). An active workflow_controls hold
    # for (tenant, 'campaign_send') HOLDS the fan-out before any customer send — same
    # held-status return shape as VT-300 so downstream readers are unchanged. Tenant-scoped
    # (no run_id needed): ops 'pause' rows land per (tenant, kind), released via /release.
    # check_pause is the F9 two-tier read (fail-CLOSED on an acknowledged pause, fail-OPEN
    # + degraded alert otherwise) — it never raises into a live graph run.
    from orchestrator.run_control import check_pause

    if check_pause(tenant_id_str, "campaign_send"):
        import logging
        logging.getLogger(__name__).info(
            "_campaign_execute_node: HELD by run-control pause tenant=%s campaign=%s",
            tenant_id_str, campaign_id_str,
        )
        # VT-374 B1 — the hold lands on this run's timeline as a
        # run_control_intervention step row (action='held'; this node returns held
        # rather than waiting, so there is no paused_ms duration to record).
        # record_intervention never raises — a timeline miss must not alter the hold.
        run_id = state.get("run_id")
        if run_id is not None:
            from orchestrator.observability.pipeline_observability import (
                record_intervention,
            )

            record_intervention(
                tenant_id_str,
                str(run_id),
                workflow_kind="campaign_send",
                step_name="execute_fanout",
                action="held",
            )
        return {
            "campaign_execution_summary": {
                "status": "held_by_run_control",
                "control_type": "pause",
            }
        }

    try:
        with tenant_connection(tenant_id_str) as conn:
            summary = execute_approved_campaign(
                tenant_id_str,
                campaign_id_str,
                conn=conn,
            )
        # VT-328: the enforcement lives INSIDE execute_approved_campaign (the single chokepoint).
        # The node only reflects a block into clean graph state — no duplicate phase read here.
        if summary.get("dispatch_blocked"):
            return {"campaign_execution_blocked": {"reason": "tenant_phase_terminal"}}
        return {"campaign_execution_summary": summary}
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).info(
            "_campaign_execute_node: error tenant=%s campaign=%s err=%s",
            tenant_id_str, campaign_id_str, type(exc).__name__,
        )
        return {"campaign_execution_error": type(exc).__name__}


def build_supervisor_graph(
    model: ChatAnthropic,
    checkpointer: PostgresSaver | None = None,
) -> Any:
    """Compose and compile the parent multi-agent graph.

    Nodes:
      - orchestrator_agent: the supervisor, built with spawn_sales_recovery
        added to its tools.
      - sales_recovery_agent: the module-level ``_sales_recovery_node`` —
        calls the REAL ``run_sales_recovery_agent`` with the self-evaluate
        gate active (VT-SR-Agent dispatch switch).
      - orchestrator_terminal: the no-spawn sink (CL-188).

    Routing:
      - START -> orchestrator_agent
      - orchestrator_agent -> conditional: 'spawn' -> sales_recovery_agent,
        'terminal' -> orchestrator_terminal (route_after_orchestrator).
        The spawn tool ALSO emits Command(goto='sales_recovery_agent',
        graph=Command.PARENT) — landmine test covers the precedence.
      - sales_recovery_agent -> collapse (PR 3/3): persists the CampaignPlan
        and updates subscriber_states activity. No phase change.
      - collapse -> END
      - orchestrator_terminal -> END

    ``checkpointer`` (PR 2/3): when given, the graph compiles with Postgres
    checkpointing; PR 1/3 callers pass nothing and compile checkpoint-free.
    """
    # VT-465 — the roster registry drives the manager's spawn-tool set + the
    # specialist nodes + their conditional-edge route map. Adding a future lane
    # = ONE SpecialistSpec entry in agent/roster.py — no edit here. The two
    # existing specialists (sales_recovery, integration) are roster entries that
    # reproduce their pre-VT-465 wiring byte-for-byte.
    from orchestrator.agent.roster import ROSTER, _register_lanes

    # VT-465 central integration — ensure all six business specialist lanes are
    # registered before iterating ROSTER. ``_register_lanes`` is idempotent +
    # re-entrancy-safe; it backfills any lane deferred by the roster<->lane import
    # cycle (so the graph always gains the full nine-lane node/route set).
    _register_lanes()

    orchestrator = build_orchestrator_agent(
        model=model, extra_tools=roster_spawn_tools()
    )

    # VT-183 retrofit: 3 function-based supervisor StateGraph nodes wrapped
    # with `with_state_transition_hook` so each execution writes one
    # `state_transition` pipeline_steps row via VT-180 write_step.
    #
    # ``orchestrator`` is a CompiledStateGraph (returned by
    # `build_orchestrator_agent`) — LangGraph's `add_node` coerces compiled
    # subgraphs through a different signature-inspection path that does not
    # tolerate function wrappers; wrapping the compiled subgraph trips
    # `descriptor '__call__' for 'type' objects doesn't apply to a
    # CompiledStateGraph` (caught in CI run 26474435891). The orchestrator
    # subgraph emits its own internal state transitions; the supervisor's
    # 3 function nodes around it capture the parent-graph transitions.
    # If pipeline_steps coverage of inside-orchestrator transitions becomes
    # required, follow-up VT-N row wires a hook inside `build_orchestrator_agent`.
    #
    # Caller MUST enter `observability_context(...)` before invoking
    # the compiled graph or the hooks skip with a warning (best-effort
    # per CL-122). Q1/Q2/Q3 Option A locked per Cowork plan-review.
    from orchestrator.observability.langgraph_hooks import (
        with_state_transition_hook,
    )

    graph = StateGraph(AgentGraphState)
    # observability:opt-out reason=CompiledStateGraph-subgraph-rejects-function-wrappers-per-VT-183
    graph.add_node("orchestrator_agent", orchestrator)

    # VT-465 — roster-driven specialist nodes. Each spec contributes its node
    # under spec.agent_name (built via spec.node_builder, fed the shared model).
    # spec.wrap_node=True => a plain function wrapped with the VT-183
    # state-transition hook (sales_recovery); False => a CompiledStateGraph
    # sub-graph added raw (integration — LangGraph rejects function wrappers
    # around compiled sub-graphs, VT-183/VT-206).
    # observability:opt-out reason=CompiledStateGraph-subgraph-rejects-function-wrappers-per-VT-183
    for spec in ROSTER:
        node = spec.node_builder(model)
        if spec.wrap_node:
            node = with_state_transition_hook(node, node_name=spec.agent_name)
        graph.add_node(spec.agent_name, node)

    graph.add_node(
        "collapse",
        with_state_transition_hook(collapse_node, node_name="collapse"),
    )
    graph.add_node(
        "orchestrator_terminal",
        with_state_transition_hook(orchestrator_terminal_node, node_name="orchestrator_terminal"),
    )
    # VT-47 — the Pillar-7 owner-approval gate node. NOT wrapped with
    # with_state_transition_hook: this node calls langgraph.types.interrupt(),
    # which raises GraphInterrupt mid-execution for the pregel loop to catch +
    # checkpoint. A state-transition hook around it would observe a partial
    # (interrupting) execution and could swallow / mis-time the GraphInterrupt.
    # The node's own CL-390 logging is the observability substrate here.
    # observability:opt-out reason=interrupt-raising-control-node-must-not-be-hook-wrapped-VT-47
    graph.add_node("request_owner_approval", request_owner_approval_node)
    graph.add_edge(START, "orchestrator_agent")
    # VT-465 — the conditional-edge path map is derived from the roster: each
    # spec's route_key -> its agent_name node, plus the 'terminal' sink for the
    # no-spawn case. route_after_orchestrator returns whichever applies. A new
    # lane's branch appears here automatically from its SpecialistSpec.
    orchestrator_route_map: dict[str, str] = {
        spec.route_key: spec.agent_name for spec in ROSTER
    }
    orchestrator_route_map["terminal"] = "orchestrator_terminal"
    graph.add_conditional_edges(
        "orchestrator_agent",
        route_after_orchestrator,
        orchestrator_route_map,
    )
    # VT-465 — each lane's outgoing edge is declared by spec.edge_to:
    #   - sales_recovery -> 'collapse' (its CampaignPlan needs persisting +
    #     the approval rail).
    #   - integration -> END (spec.edge_to=None): the integration_agent
    #     sub-graph emits its own internal state transitions and produces no
    #     campaign plan, so control returns straight to the supervisor's END.
    for spec in ROSTER:
        graph.add_edge(spec.agent_name, spec.edge_to if spec.edge_to is not None else END)
    # VT-47 — after collapse persists a PROPOSED campaign it attaches
    # pending_approval_request; route_after_collapse sends that to the
    # approval gate (which pauses via interrupt()). Every other collapse
    # terminal (refusal / defer / fail-closed rejection) goes straight to END.
    graph.add_conditional_edges(
        "collapse",
        route_after_collapse,
        {
            "approval_gate": "request_owner_approval",
            "end": END,
        },
    )
    # VT-251 — campaign execution seam: when the owner approves, fan out
    # the campaign before ending the run. Non-approved decisions go directly
    # to END (Pillar 7: rejected / needs_changes / timeout / send_failed
    # must NEVER proceed to send).
    # observability:opt-out reason=deterministic-post-gate-node-no-interrupt-VT-251
    graph.add_node(
        "campaign_execute",
        with_state_transition_hook(_campaign_execute_node, node_name="campaign_execute"),
    )
    graph.add_conditional_edges(
        "request_owner_approval",
        route_after_approval,
        {
            "campaign_execute": "campaign_execute",
            "end": END,
        },
    )
    graph.add_edge("campaign_execute", END)
    graph.add_edge("orchestrator_terminal", END)

    if checkpointer is not None:
        return graph.compile(checkpointer=checkpointer)
    return graph.compile()
