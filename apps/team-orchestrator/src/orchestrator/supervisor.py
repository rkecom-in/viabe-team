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
import logging
from typing import Any
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError
from team_shared.mcp import ToolContext

from orchestrator._tenant_guard import TenantIsolationError
from orchestrator.auth.prod_safety import _is_prod
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
from orchestrator.manager.loop_mode import LoopMode, get_loop_mode
from orchestrator.routing import (
    orchestrator_terminal_node,
    route_after_approval,
    route_after_collapse,
    route_after_orchestrator,
)
from orchestrator.state.agent_graph_state import AgentGraphState

logger = logging.getLogger(__name__)


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


class LaneNodeError(RuntimeError):
    """VT-602 — a ROSTER lane node raised an exception that would otherwise escape
    ``graph.invoke()`` (the structural gap the VT-598 live pack surfaced: at the time,
    none of the six business lanes — marketing/sales/finance/accounting/tech/cost_opt —
    nor integration/onboarding_conductor carried any error middleware; a live crash
    inside a lane's ``create_agent`` build, e.g. the marketing-lane
    "non-consecutive system messages" ValueError, escaped into ``dispatch_brain``'s
    generic ``except Exception: raise`` -> DBOS retries forever -> the run never
    terminates -> owner silence). VT-604 Package 1: the six business lanes are no
    longer ROSTER nodes (they are Manager-held advisory tools — no graph node to
    crash), so today this net wraps the three remaining ROSTER specialists
    (sales_recovery / integration / onboarding_conductor); kept generic so any FUTURE
    roster addition inherits it for free.

    Raised by ``_wrap_lane_node_exceptions`` (below), which wraps EVERY ROSTER node
    at ``build_supervisor_graph`` registration — the whole class of lane-node
    exceptions is dead regardless of the specific exception type, and a future lane
    appended to ROSTER inherits the net for free. ``dispatch_brain`` catches this
    (mirroring the VT-492 ``SpecialistNoOutputError`` convert-don't-orphan pattern)
    and maps it to a CLEAN ``escalated`` terminal.

    PII-safe fields only: the lane name + the ORIGINAL exception's TYPE name (never
    ``str(exc)``, which may carry the owner body / a specialist's draft — CL-390).
    """

    def __init__(self, *, lane: str, exc_type: str) -> None:
        self.lane = lane
        self.exc_type = exc_type
        super().__init__(
            f"lane node {lane!r} raised {exc_type} (see the preceding warning "
            f"log line for the original exception)"
        )


def _wrap_lane_node_exceptions(node_callable: Any, *, lane: str) -> Any:
    """VT-602 — wrap a ROSTER lane's node/sub-graph so ANY exception escaping it
    converts to a ``LaneNodeError`` instead of propagating raw into
    ``graph.invoke()`` (see ``LaneNodeError`` docstring for the defect this closes).

    Handles BOTH node shapes ``build_supervisor_graph`` iterates over ROSTER:
    a plain function (``spec.wrap_node=True`` — e.g. ``_sales_recovery_node``,
    called directly) and a compiled sub-graph (``spec.wrap_node=False`` — e.g.
    marketing/integration/onboarding_conductor, called via ``.invoke``;
    ``CompiledStateGraph`` is not itself callable — verified empirically).

    Deliberately a BARE closure — NOT ``functools.wraps``. ``with_state_transition_
    hook`` (VT-183) cannot wrap a compiled sub-graph: ``functools.wraps`` copies
    ``__wrapped__`` onto the wrapper, and ``inspect.signature`` follows that chain
    into the ``CompiledStateGraph`` instance's own ``__call__`` descriptor, tripping
    "descriptor '__call__' for 'type' objects doesn't apply to a CompiledStateGraph"
    at ``add_node``/build time (the reason ``wrap_node=False`` skips ``with_state_
    transition_hook`` entirely). A bare closure carries no ``__wrapped__`` — LangGraph
    inspects only the wrapper's own ``(state, *args, **kwargs)`` signature and never
    touches the wrapped sub-graph's type, sidestepping the trap (verified empirically:
    a real compiled sub-graph wrapped this way builds, runs, and still propagates an
    internal ``interrupt()`` through unchanged).

    Two carve-outs re-raise UNCHANGED (checked before the catch-all, most specific
    first):
      - ``GraphBubbleUp`` (``GraphInterrupt``'s base + subgraph-control signals) —
        mirrors the VT-484 tool-error middleware's own carve-out. NONE of the six
        lanes / integration / onboarding_conductor call ``interrupt()`` today (only
        the standalone ``request_owner_approval`` gate node does, and it is added
        OUTSIDE the ROSTER loop precisely so it is never wrapped) — this is
        defense-in-depth, not a live path.
      - ``SpecialistNoOutputError`` — the EXISTING VT-492 typed signal
        ``_sales_recovery_node`` raises. It is already a clean, structured signal
        ``dispatch_brain`` converts via its OWN more specific ``except`` clause
        (which reads ``.specialist`` / ``.status`` to build a precise reason); this
        wrapper must not re-box it into a generic ``LaneNodeError`` and lose that
        precision or change the VT-492 reason format.
    """
    invoke = getattr(node_callable, "invoke", node_callable)

    def _lane_node_wrapper(state: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return invoke(state, *args, **kwargs)
        except GraphBubbleUp:
            raise
        except SpecialistNoOutputError:
            raise
        except Exception as exc:  # noqa: BLE001 — the whole point: convert ANY lane exception
            # Structural detail (HTTP status) is non-PII and logged everywhere; the FULL
            # exception message is dev-only. An Anthropic 400 message can echo request
            # content (owner text → PII), so it must never reach prod logs. Dev carries
            # only synthetic data (CL-422), so the full message is safe there.
            status = getattr(exc, "status_code", None)
            detail = f"status={status}" if status is not None else ""
            if not _is_prod():
                detail = f"{detail} {str(exc)[:600]}".strip()
            logger.warning(
                "supervisor: lane node %r raised %s; converting to LaneNodeError "
                "(VT-602 — preventing an unhandled lane exception from hanging the run): %s",
                lane,
                type(exc).__name__,
                detail,
            )
            raise LaneNodeError(lane=lane, exc_type=type(exc).__name__) from exc

    return _lane_node_wrapper


# VT-101 Stage 3(b) — the framework registration for the SR PROPOSER path, cached per-process.
_SR_REGISTERED: Any = None


def _sr_registered_module() -> Any:
    """Register the SR module into the framework's process-global default registry ONCE and cache it.

    The default registry is SHARED with ``coordinator.get_registry``'s flag-ON EXECUTOR branch;
    whichever path runs first registers, the other re-enters. A module is keyed by ``manifest.name``,
    so ``register_agent`` raises ``ModuleRegistrationError`` on a duplicate — on a second entry (this
    node re-invoked in the same process, OR the coordinator having registered first) fall back to
    ``get_registered`` rather than crash. Both paths then share the SAME ``RegisteredModule``.
    """
    global _SR_REGISTERED
    if _SR_REGISTERED is None:
        from orchestrator.agent_framework import (
            ModuleRegistrationError,
            get_registered,
            register_agent,
        )
        from orchestrator.agent_framework.modules.sales_recovery_module import (
            MODULE_NAME,
            SalesRecoveryModule,
        )

        try:
            _SR_REGISTERED = register_agent(SalesRecoveryModule())
        except ModuleRegistrationError:
            _SR_REGISTERED = get_registered(MODULE_NAME)
    return _SR_REGISTERED


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

    # VT-101 Stage 3(b): behind ``TEAM_SR_VIA_FRAMEWORK`` (default OFF), route the proposer through
    # the agent_framework module contract instead of calling ``run_sales_recovery_agent`` directly.
    # Both branches yield the SAME two downstream locals (``result_output`` / ``result_status``); the
    # flag-OFF branch IS the original call, byte-identical to pre-VT-101 behavior. The module's
    # ``propose`` is None-preserving, so ``result_output is None`` iff the agent produced no output —
    # the ``SpecialistNoOutputError`` detection below is unchanged under the flag.
    from orchestrator.agent_framework.modules.sales_recovery_module import (
        sr_via_framework,
    )

    if sr_via_framework():
        from orchestrator.agent_framework import ModuleContext

        registered = _sr_registered_module()
        module_ctx = ModuleContext.for_proposer(
            tenant_model_value=str(tenant_uuid),
            module_name="sales_recovery",
            run_id=str(run_uuid),
            data={"sales_recovery_context": context, "evaluator": evaluator},
        )
        mres = registered.run(module_ctx)
        result_output = mres.proposal  # None-preserving (the VT-101 module fix guarantees this)
        result_status = mres.status
    else:
        agent_result = run_sales_recovery_agent(context, evaluator=evaluator)
        result_output = agent_result.output
        result_status = agent_result.status

    if result_output is None:
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
            status=str(result_status),
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
        plan = parse_campaign_plan(result_output)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise SpecialistNoOutputError(
            specialist="sales_recovery",
            status=str(result_status),
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


def _render_raw_specialist_output(state: AgentGraphState) -> str:
    """VT-606 — a PII-conscious text rendering of what the just-dispatched specialist actually
    produced, for ``manager_review``'s structured-extraction LLM call. Reuses the SAME fields
    ``dispatch._classify_terminal`` already reads (campaign_plan / messages) — no new state, no new
    redaction layer: the specialists' own existing PII discipline (CL-390 — e.g. CampaignPlan
    carries segment LABELS never customer identities; advisory/lane tool outputs are counts-only)
    is what this renders, unchanged. Bounded to the last few messages (cost; enough context)."""
    parts: list[str] = []
    campaign_plan = state.get("campaign_plan")
    if campaign_plan is not None:
        parts.append(f"campaign_plan: {campaign_plan!r}")
    messages = state.get("messages") or []
    for msg in messages[-6:]:
        role = type(msg).__name__
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = " ".join(str(c) for c in content)
        line = f"[{role}] {content}"
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            line += f" tool_calls={tool_calls}"
        parts.append(line)
    return "\n".join(parts) if parts else "(no output)"


def _manager_review_node(state: AgentGraphState) -> dict[str, Any]:
    """VT-606 (Loop Package 3) — the manager_review graph node. ENFORCE-MODE ONLY (never added to
    the legacy/shadow graph — see ``build_supervisor_graph``'s mode branch). Reads the plan-store
    framing ``manager_task_workflow`` populated in the initial state, runs
    ``manager.review.manager_review`` (the ONE sonnet-5 structured-extraction call + the
    deterministic decision seam — persists the plan_store effect + tm_audit + incident itself), and
    writes ``manager_review_outcome`` so ``_route_after_manager_review`` can route, plus (round-3
    adversarial-review fix, MAJOR #4) ``manager_review_revised_outcome`` — the reframed
    desired_outcome ``decide_next_action`` computed on a revise_step decision, which the OLD code
    silently discarded (the step was reset to 'pending' and re-dispatched with its STALE original
    framing, never the actual revision). ``None`` for every other outcome.
    """
    from orchestrator.manager.review import manager_review

    tenant_id = state.get("tenant_id")
    task_id = state.get("manager_task_id")
    step_id = state.get("manager_step_id")
    if tenant_id is None or task_id is None or step_id is None:
        # Cannot happen on a graph manager_task_workflow itself built and invoked (it always
        # populates these) — fail closed rather than crash the graph on a malformed caller.
        logger.warning(
            "_manager_review_node: missing manager_task_id/manager_step_id/tenant_id in state "
            "(enforce-mode graph invoked without the loop's own framing) — no-op, routes to END"
        )
        return {"manager_review_outcome": "escalate", "manager_review_revised_outcome": None}

    # §7D — the reasoning_ref join target is the ACTIVE ObservabilityContext's run_id, NOT
    # state['run_id']: the two diverge for an enforce-loop dispatch (manager.workflow.
    # _dispatch_specialist_step enters observability_context(run_id=UUID(task_id), ...) while
    # state['run_id'] carries the per-attempt loop_run_id) — see manager_review's own docstring
    # for the full explanation. Fail-soft to state['run_id'] if the context is somehow unset
    # (should not happen — every graph.invoke() reaching this node runs inside that context — but
    # observability must never crash the graph over a missing ContextVar).
    from orchestrator.observability.decorators import _observability_context

    ctx = _observability_context.get()
    review_run_id = ctx.run_id if ctx is not None else state.get("run_id")

    result = manager_review(
        tenant_id,
        task_id,
        step_id,
        situation=state.get("manager_step_situation") or "",
        desired_outcome=state.get("manager_step_desired_outcome") or "",
        acceptance_criteria=state.get("manager_step_acceptance_criteria") or [],
        raw_output=_render_raw_specialist_output(state),
        has_next_step=bool(state.get("manager_has_next_step")),
        # VT-607 (Loop Package 6): Sales Recovery's own structured CampaignPlan, when present,
        # routes manager_review through the deterministic typed adapter (no sonnet-5 call) —
        # see manager_review's own docstring for why.
        campaign_plan=state.get("campaign_plan"),
        run_id=review_run_id,
    )
    return {
        "manager_review_outcome": result.outcome,
        "manager_review_revised_outcome": result.decision.revised_outcome,
    }


def _route_after_manager_review(state: AgentGraphState) -> str:
    """VT-606 — manager_review's own outgoing edge. A produced ``campaign_plan`` still needs
    ``collapse`` + the approval rail (Package 3: "Preserve the approval interrupt and campaign
    effect path") regardless of ``manager_review_outcome`` — collapse's own fail-closed guard
    (CL-294) independently re-validates the plan variant. Every other case ends this graph
    invocation; ``manager_task_workflow``'s outer loop decides what happens next."""
    if state.get("campaign_plan") is not None:
        return "collapse"
    return "end"


def build_supervisor_graph(
    model: BaseChatModel,
    checkpointer: PostgresSaver | None = None,
    *,
    mode: LoopMode | None = None,
    suppress_answerable_spawns: bool = False,
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

    ``mode`` (VT-606, Loop Package 3) — defaults to ``manager.loop_mode.get_loop_mode()`` when
    omitted. ``legacy`` AND ``shadow`` build the EXACT graph shape described above (byte-identical
    node/edge set — amendment A1: shadow's real dispatch must stay legacy-shaped so the
    CampaignPlan -> collapse -> VT-594 owner-surfacing path is untouched until enforce). ONLY
    ``enforce`` changes the shape: every roster specialist routes to a NEW ``manager_review`` node
    instead of straight to its ``edge_to``/END target; only ``manager_review`` may then route to
    ``collapse`` (a campaign_plan was produced) or END. See ``_manager_review_node`` /
    ``_route_after_manager_review`` below.
    """
    resolved_mode: LoopMode = mode if mode is not None else get_loop_mode()
    # VT-465 — the roster registry drives the manager's spawn-tool set + the
    # specialist nodes + their conditional-edge route map. Adding a future lane
    # = ONE SpecialistSpec entry in agent/roster.py — no edit here. The three
    # roster specialists (sales_recovery, integration, onboarding_conductor) are
    # roster entries that reproduce their pre-VT-465 wiring byte-for-byte.
    #
    # VT-604 Package 1: ROSTER is now EXACTLY those three — the six business-domain
    # lanes (sales/marketing/finance/accounting/tech/cost_opt) are no longer
    # dynamically registered here; they are Manager-held ADVISORY tools instead
    # (``ADVISORY_TOOLS``, below) — no spawn tool, no graph node, no conditional-edge
    # route for any of the six.
    from orchestrator.agent.advisory_registry import ADVISORY_TOOLS
    from orchestrator.agent.roster import ANSWERABLE_SUPPRESSED_ROUTE_KEYS, ROSTER

    # T9 — on an answerable turn (triage direct_reply / task_status), drop the non-onboarding
    # specialist spawns so the manager ANSWERS in-turn from its read-tools instead of spawning an
    # async specialist that D1-stalls. onboarding_conductor stays (increment-2). The excluded
    # specialists' graph nodes/edges remain but are unreachable this turn.
    _spawn_exclusions = ANSWERABLE_SUPPRESSED_ROUTE_KEYS if suppress_answerable_spawns else frozenset()
    orchestrator = build_orchestrator_agent(
        model=model,
        extra_tools=[*roster_spawn_tools(exclude_route_keys=_spawn_exclusions), *ADVISORY_TOOLS],
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
        # VT-602 — the structural exception net: wrap AFTER the (optional)
        # state-transition hook so a failed sales_recovery run still records its
        # 'failed' pipeline_steps row (the hook's own except-log-reraise) before this
        # converts the escaped exception to a clean LaneNodeError. Applies to EVERY
        # roster node uniformly (function or compiled sub-graph) — a future lane
        # appended to ROSTER inherits the net with no further wiring.
        node = _wrap_lane_node_exceptions(node, lane=spec.name)
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

    def _route_after_orchestrator_producing(state: AgentGraphState) -> str:
        """VT-565 — wrap the routing decision so an objective-bearing SPAWN mints the run's
        durable manager_task at the delegation seam (the B2 live producer). This is the seam the
        landmine test proved fires exactly once per run ('spawn' on the spawn path, 'terminal'
        on the no-spawn path). Pure state-tracking + fully fail-soft: ``on_route_decided`` never
        raises and never changes the route returned, so routing is byte-for-byte unchanged.
        ``route_after_orchestrator`` is referenced as a module global so the existing tests that
        monkeypatch it still drive this wrapper."""
        route = route_after_orchestrator(state)
        from orchestrator.manager.task_producer import on_route_decided

        on_route_decided(state, route)

        # VT-619 — budget hard-pause at the delegation seam. If the manager routed to a specialist
        # whose BILLED agent has hit its HARD cap, DON'T spawn (don't do the paid work): override
        # to the orchestrator terminal so the manager answers the owner conversationally instead.
        # Meters on RAW counts; budget_status fails OPEN, and any error here keeps the ORIGINAL
        # route (fail-open) — a metering blip never breaks routing.
        try:
            from orchestrator.agent.usage_meter import billed_agent_for_node, budget_status

            billed = billed_agent_for_node(orchestrator_route_map.get(route))
            tenant_id = state.get("tenant_id")
            if (
                billed is not None
                and tenant_id
                and budget_status(tenant_id, billed).get("over_hard")
            ):
                # TODO(VT-619): compose an owner-facing top-up OFFER in orchestrator_terminal_node
                # keyed off ``budget_paused_agent`` (do NOT fabricate customer copy here). Making
                # this flag durable requires registering it as a state channel + a sanctioned state
                # update; set best-effort for now. The LOAD-BEARING behavior is the route override
                # (no paid specialist spawn once hard-capped).
                state["budget_paused_agent"] = billed
                return "terminal"
        except Exception:  # noqa: BLE001 — fail-open: a metering blip never changes routing
            logger.warning("VT-619 budget-pause route check swallowed", exc_info=True)
        return route

    graph.add_conditional_edges(
        "orchestrator_agent",
        _route_after_orchestrator_producing,
        orchestrator_route_map,
    )
    if resolved_mode == "enforce":
        # VT-606 (Loop Package 3) — "Remove specialist-to-END edges. Route every specialist to
        # manager_review. Only manager_review may advance or terminate a task." EVERY roster
        # specialist's edge_to is ignored here (not read at all) — they all route to the ONE new
        # node instead. legacy/shadow NEVER reach this branch (see resolved_mode check above),
        # so their graph shape is provably unaffected by this node existing in the module.
        graph.add_node(
            "manager_review",
            with_state_transition_hook(_manager_review_node, node_name="manager_review"),
        )
        for spec in ROSTER:
            graph.add_edge(spec.agent_name, "manager_review")
        # manager_review's OWN routing: a produced campaign_plan still needs collapse + the
        # approval rail (Package 3: "Preserve the approval interrupt and campaign effect path") —
        # every other outcome (continue/complete/revise_step/ask_owner/escalate) ends THIS graph
        # invocation cleanly; manager_task_workflow's own outer loop decides what happens next
        # (claim the next step / stop), never a second dispatch inside the SAME graph.invoke.
        graph.add_conditional_edges(
            "manager_review",
            _route_after_manager_review,
            {"collapse": "collapse", "end": END},
        )
    else:
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
