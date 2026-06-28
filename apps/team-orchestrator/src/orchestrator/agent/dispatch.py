"""VT-193 brain dispatch — closes the runner → supervisor seam.

`pre_filter` classifies inbound webhook events into one of three buckets
(direct_handler / brain / reject). The "brain" bucket previously wrote a
``record_brain_pending`` placeholder and bailed to ``status='escalated'``
because the supervisor graph wasn't wired into ``runner.py``. The
substrate (VT-125/126/27/180/182/183) was built but never invoked from
the production webhook path. This module closes that seam.

Flow:

1. Write the dispatch ENTRY envelope: ``agent_invocation`` step_kind
   (VT-179's canonical kind — its docstring says "runner.record_brain_
   pending writes this kind"; semantic now shifts from placeholder to
   real dispatch).
2. Enter ``observability_context(run_id, tenant_id)`` so VT-125's
   ``OrchestratorReasoningCallback`` reads ContextVar and emits
   ``agent_reasoning_step`` rows on each ``on_llm_end`` boundary.
3. Build the supervisor graph + the langchain callback. PASS the
   callback explicitly via ``graph.invoke(..., config={"callbacks":[cb]})``
   so langgraph propagates it to the inner orchestrator-agent
   subgraph's LLM calls (without this, the callback never attaches and
   reasoning rows never get written — exactly the symptom the 2026-05-27
   E2E surfaced).
4. Capture terminal state. Branch on:
   - ``terminated_without_spawn=True`` → terminal node reached
     (orchestrator responded directly); final_status='completed'.
   - ``campaign_plan`` field present → collapse node reached (specialist
     produced a plan); final_status='completed'.
   - ``escalate_to_fazal`` tool was called → final_status='escalated'.
5. Programmatic ``compose_owner_output(specialist_result, state,
   intent_or_trigger)`` to produce the unified-output envelope (VT-30).
   Emit the ``compose_output`` step_kind row regardless of terminal
   path so Ops Console replay always sees the composed payload.
6. On ``HardLimitExceeded`` (raised by ``OrchestratorReasoningCallback``
   mid-invocation per VT-125 hard-limit enforcement): catch, write
   ``aborted_hard_limit`` envelope step, return ``DispatchResult`` with
   ``final_status='aborted_hard_limit'``. Per VT-193 Pillar 8 error
   taxonomy: this is a CLEAN terminal state — DBOS does NOT retry the
   workflow.

Q1/Q2/Q3/Q4 locked per Cowork plan-review 2026-05-27:
- Q1 Option A: direct supervisor invoke; callback via config.callbacks
- Q2 Option A: programmatic compose at dispatch exit (verified
  orchestrator_terminal_node + collapse_node do NOT compose)
- Q3: ``record_brain_pending`` deleted (placeholder dead code; test
  ``test_record_brain_pending_idempotent`` rips with it)
- Q4: existing ``escalate_to_fazal`` tool name kept (no rename)

Per CL-19: typed envelopes; brain_dispatch reuses agent_invocation
(VT-179 canonical kind).
Per CL-24: orchestrator-as-agent locked; this seam invokes that brain.
Per CL-122: write_step is best-effort; observability failures don't
break the caller's flow.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from orchestrator.agent.orchestrator_agent_driver import (
    HardLimitExceeded,
    OrchestratorAgentDriver,
    OrchestratorUsage,
)
from orchestrator.observability.decorators import observability_context
from orchestrator.observability.langchain_callback import (
    OrchestratorReasoningCallback,
)
from orchestrator.observability.pipeline_observability import write_step
from orchestrator.output_composer import compose_owner_output
from orchestrator.state import SubscriberState
from orchestrator.supervisor import build_supervisor_graph
from orchestrator.types import WebhookEvent

logger = logging.getLogger(__name__)


# VT-47: 'paused' is a NEW distinct terminal — the run halted on an owner-
# approval interrupt() and is waiting for the owner's decision (resume path /
# timeout sweep drives it onward to 'completed'). It is NOT an error and NOT
# 'completed'. Threaded through pipeline_runs.status (migration 052 CHECK).
FinalStatus = Literal["completed", "escalated", "aborted_hard_limit", "paused"]
TerminalPath = Literal["terminal", "collapse", "escalated", "paused"]


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of a brain dispatch."""

    final_status: FinalStatus
    terminal_path: TerminalPath | None
    reason: str | None = None


# Same model surface as build_orchestrator_agent's module-level default.
# Small max_tokens cap (defense against runaway generation; the hard-limit
# callback covers the cost dimension).
_DEFAULT_MAX_TOKENS = 4096

# VT-480 — brain model tiering. Fazal CHOSE this over raising the ₹5 cost cap
# (ORCHESTRATOR_COST_HARD_LIMIT_PAISE=500): a multi-turn Opus run on routine
# chatter exceeded the cap → HardLimitExceeded → reply truncated to
# team_unable_to_complete_request. Route ROUTINE/simple turns to Sonnet (cheap,
# completes within ₹5); reserve Opus for COMPLEX reasoning (business actions,
# specialist spawns, cross-lane decisions, anything ambiguous).
#
# SINGLE SOURCE OF TRUTH for the brain model IDs — every brain-model selection
# reads these two constants (do NOT inline the strings elsewhere in this file).
_BRAIN_MODEL_SONNET = "claude-sonnet-4-6"  # routine/simple turns — cheap, fast
_BRAIN_MODEL_OPUS = "claude-opus-4-8"  # complex reasoning — the capable default

# Classifications that are CLEARLY simple → route to Sonnet. CORRECTNESS-FIRST:
# anything NOT in this allow-set (incl. an absent/failed classify) falls back to
# Opus — under-powering a business decision is worse than the cost. Each entry
# is a low-stakes, typically single-step turn that does not drive a specialist
# spawn or a customer-facing send:
#   - approval / rejection      : a one-step ack of a pending owner decision
#   - question                  : a simple FAQ / factual "what's my plan" read
#   - status_query              : read-only state lookup (also edge-fast-pathed)
# Everything else stays on Opus by design:
#   - feedback                  : may carry a business signal → reason hard
#   - first_data_step_onboarding: can drive an onboarding-conductor spawn
#   - adhoc_campaign_request    : a SEND / business action (owner_initiated)
#   - exclusion_request         : low-confidence fall-through = ambiguous mutate
#   - other                     : ambiguous by definition
# Values mirror agent.tools.classify_owner_message.Classification.
_ROUTINE_INTENTS: frozenset[str] = frozenset(
    {"approval", "rejection", "question", "status_query"}
)


def select_brain_model(intent: dict[str, Any]) -> tuple[str, str]:
    """VT-480 — pick the brain model from the ALREADY-COMPUTED intent signal.

    ``intent`` is the same dict the VT-461 edge router populated via its
    ``intent_sink`` (``classification`` / ``confidence`` / ``suggested_action``):
    a successful classify carries those fields; an empty dict means classify was
    skipped or failed. This REUSES that classification — it does NOT make a
    second classify / LLM call.

    Returns ``(model_id, tier)`` where ``tier`` is ``"sonnet"`` | ``"opus"`` (a
    PII-safe label for observability — never the owner body). CORRECTNESS-FIRST:
    a routine classification in ``_ROUTINE_INTENTS`` → Sonnet; ANY other value,
    including a missing/empty signal, fails safe to Opus (the capable model).
    """
    classification = intent.get("classification")
    if isinstance(classification, str) and classification in _ROUTINE_INTENTS:
        return (_BRAIN_MODEL_SONNET, "sonnet")
    # Complex, ambiguous, or signal-absent → the capable model (fail-safe).
    return (_BRAIN_MODEL_OPUS, "opus")


def _build_manager_intent_block(intent: dict[str, Any]) -> str | None:
    """VT-461 — render the captured Haiku classification as the brain's ``## Manager
    intent signal`` prior.

    ``intent`` is the dict the edge router populated (see ``route_edge_case``'s
    ``intent_sink``): a successful classify carries ``classification`` / ``confidence`` /
    ``suggested_action``; an empty dict means classify was skipped or failed. Returns the
    system-block text, or ``None`` when there is no signal to inject (the brain then reasons
    from the owner's message alone). Carries ONLY the typed envelope fields — never the raw
    owner body (that already rides in the HumanMessage; the consent gate governs its
    transmit, CL-425/VT-270)."""
    classification = intent.get("classification")
    if not classification:
        return None
    confidence = float(intent.get("confidence", 0.0) or 0.0)
    suggested = str(intent.get("suggested_action", "") or "").strip()
    lines = [
        "## Manager intent signal",
        "A fast pre-read of the owner's message (a PRIOR, not a verdict — reason from it):",
        f"- classification: {classification}",
        f"- confidence: {confidence:.2f}",
    ]
    if suggested:
        lines.append(f"- suggested next step: {suggested}")
    return "\n".join(lines)


def _resolve_model(model_id: str = _BRAIN_MODEL_OPUS) -> ChatAnthropic:
    # VT-480: ``model_id`` is the tier-selected brain model (see
    # select_brain_model). Defaults to Opus (the capable model) so any caller
    # that doesn't pass a selection still fails safe. mypy --strict needs the
    # call-arg ignore because ChatAnthropic's pydantic kwargs aren't expanded
    # without the pydantic mypy plugin (parity with orchestrator_agent.py:_MODEL).
    return ChatAnthropic(  # type: ignore[call-arg]
        model=model_id, max_tokens=_DEFAULT_MAX_TOKENS
    )


def dispatch_brain(
    *,
    event: WebhookEvent,
    state: SubscriberState,
    run_id: UUID,
    tenant_id: UUID,
) -> DispatchResult:
    """Invoke the supervisor graph for a brain-routed webhook event.

    Returns ``DispatchResult`` carrying the terminal status for
    ``close_webhook_run`` to apply to ``pipeline_runs.status``.

    Caller (runner.webhook_pipeline_run) MUST have already opened the
    run + recorded the webhook_received envelope before calling this.

    Env gate: requires ``ANTHROPIC_API_KEY`` to be set. CI test runs
    without the key (real-Anthropic tests gated by ``ANTHROPIC_API_KEY``
    presence + ``RUN_INTEGRATION_TESTS=1``); when absent, dispatch
    writes the entry envelope but returns ``escalated`` so the path
    still terminates cleanly (mirrors the pre-VT-193 placeholder
    behaviour test fixtures still assert against).
    """
    # 1. Dispatch ENTRY envelope — agent_invocation step_kind reused per
    # Cowork brief correction (the VT-179 canonical kind).
    _write_dispatch_entry(run_id=run_id, tenant_id=tenant_id, event=event)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    # Tests monkeypatch this env to sentinel values like "test-sentinel"
    # (see test_twilio_ingress.py) to exercise non-brain seams without
    # making real SDK calls. Real Anthropic keys are sk-ant-…; gate on
    # that prefix so tests with sentinel values fall through to the
    # placeholder escalated-status path.
    if not api_key.startswith("sk-ant-"):
        logger.warning(
            "dispatch_brain: ANTHROPIC_API_KEY missing or sentinel-shaped; "
            "skipping supervisor invocation + returning escalated "
            "(test-mode + pre-prod-key fallback)",
            extra={"run_id": str(run_id), "tenant_id": str(tenant_id)},
        )
        return DispatchResult(
            final_status="escalated",
            terminal_path=None,
            reason="anthropic_key_absent",
        )

    # VT-84: stage-2 edge-case router — intercept the deterministic edge-case intents
    # (exclusion / status_query) and fast-path them to handlers, skipping the full agent.
    # Returns a DispatchResult to terminate, or None to fall through to the agent below.
    from orchestrator.edge_cases_router import route_edge_case

    # VT-461: capture the SAME Haiku classification the edge router already runs, so the
    # Team-Manager brain can reason handle-directly-vs-delegate from it (no 2nd classify
    # call). REUSE classify_owner_message via the edge router's intent_sink — do NOT build
    # a parallel classifier. When the turn falls through to the agent, _manager_intent
    # carries the typed envelope; an empty dict means classify was skipped/failed (the
    # brain reasons from the message alone).
    _manager_intent: dict[str, Any] = {}
    _edge = route_edge_case(
        tenant_id=tenant_id, event=event, intent_sink=_manager_intent
    )
    if isinstance(_edge, DispatchResult):
        return _edge
    # VT-335: the adhoc-campaign request returns the "owner_initiated" str marker, which
    # overrides the trigger_reason; the agent then builds a plan that the approval gate
    # confirms before any send. Validate the EXACT marker — a stray router str must never
    # silently become a trigger_reason (defensive). Anything else keeps the default.
    _trigger_reason = "owner_initiated" if _edge == "owner_initiated" else "weekly_cadence"

    usage = OrchestratorUsage()
    # callback only uses driver for ``check_mid_invocation`` raises; the
    # _NullDriver below provides that surface without the full driver's
    # post-invocation enforcement (Q1 Option A locked: mid-invocation
    # callback is the load-bearing limit gate).
    callback = OrchestratorReasoningCallback(
        driver=cast("OrchestratorAgentDriver", _NullDriver()),
        usage=usage,
        run_id=run_id,
        tenant_id=tenant_id,
    )

    # VT-195 Phase 2: pre-inject the tenant's L1 identity as a SEPARATE system
    # block AFTER the VT-194 cached prefix (D2). langchain_anthropic merges this
    # SystemMessage into the Anthropic `system` param as a distinct block FOLLOWING
    # the cached system_prompt — the cached prefix block stays first + byte-
    # identical, so the VT-194 cache still HITs (verified structurally;
    # vt195_l1_phase2 canary asserts the live cache_read + that the model uses the
    # block). L1 is enrichment: a read failure must never break dispatch.
    _messages: list[Any] = [HumanMessage(content=event.body or "")]
    try:
        from orchestrator.knowledge import assemble_context_bundle

        l1_block = assemble_context_bundle(tenant_id)
    except Exception:  # noqa: BLE001 — L1 enrichment is best-effort
        logger.warning(
            "dispatch: L1 context assembly failed (tenant=%s); proceeding without",
            tenant_id,
        )
        l1_block = None
    if l1_block:
        _messages.insert(0, SystemMessage(content=l1_block))

    # VT-466: the Team-Manager's business-context READ seam — surface the IDENTITY
    # anchor (verified business name + verification status + phase) + the manager-
    # held BUSINESS OBJECTIVE as a SEPARATE ``## Business context`` system block.
    # The manager prompt already declares "you hold the business objective + the
    # cross-functional context"; this wires the store that backs it. Composes over
    # the EXISTING L1 business_profile entity (read_business_context) — NOT a new
    # store. Best-effort, like the L1 block above: a read miss never breaks
    # dispatch. Inserted AFTER the cached system prefix (a per-turn SystemMessage),
    # so the VT-194 cache still hits. The L1 block (above) carries the owner-stated
    # profile; this block adds the identity + objective the L1 block does not.
    try:
        from orchestrator.knowledge import (
            read_business_context,
            render_business_context_block,
        )

        business_block = render_business_context_block(
            read_business_context(tenant_id)
        )
    except Exception:  # noqa: BLE001 — business-context enrichment is best-effort
        logger.warning(
            "dispatch: business-context assembly failed (tenant=%s); proceeding without",
            tenant_id,
        )
        business_block = None
    if business_block:
        _messages.insert(0, SystemMessage(content=business_block))

    # VT-461: inject the Manager-intent signal as a separate system block so the
    # Team-Manager brain reads it as a prior (the prompt's "## Manager intent signal"
    # contract) when deciding handle-directly-vs-delegate. Reuses the classification the
    # edge router already computed — no extra Haiku call. Inserted AFTER the cached system
    # prefix (it's a per-turn SystemMessage in `messages`, not the cached system_prompt), so
    # the VT-194 cache still hits. Absent/failed classify → no block; the brain still works.
    intent_block = _build_manager_intent_block(_manager_intent)
    if intent_block:
        _messages.insert(0, SystemMessage(content=intent_block))

    initial_state: dict[str, Any] = {
        "messages": _messages,
        "tenant_id": tenant_id,
        "run_id": run_id,
        "trigger_reason": _trigger_reason,  # VT-335: 'owner_initiated' for adhoc, else default
    }

    final_status: FinalStatus = "completed"
    terminal_path: TerminalPath | None = None
    reason: str | None = None
    specialist_result: Any = None
    intent_or_trigger = "owner_substantive_message"

    try:
        with observability_context(run_id=run_id, tenant_id=tenant_id):
            # VT-47: compile the supervisor graph WITH the module-level
            # checkpointer + a thread_id == run_id config so the owner-approval
            # gate's interrupt() can persist + later resume on the same run.
            # Before VT-47 this built checkpoint-free, so a pause could not
            # survive (decision D1). The checkpointer is the same PostgresSaver
            # the substrate set up + RLS'd (graph._setup_checkpoint_rls keys
            # checkpoint rows on thread_id -> pipeline_runs.tenant_id).
            from orchestrator.graph import get_checkpointer

            # VT-480: tier the BRAIN model from the already-computed intent
            # (_manager_intent — populated by route_edge_case's intent_sink
            # above; NO second classify call). Routine/simple → Sonnet (cheap,
            # completes within the ₹5 cap); complex/ambiguous/absent → Opus.
            brain_model_id, brain_tier = select_brain_model(_manager_intent)
            # PII-safe observability: the TIER + the (typed) intent label only —
            # never the owner body. Lets Ops see the Sonnet/Opus split.
            logger.info(
                "dispatch_brain: brain model tier selected",
                extra={
                    "run_id": str(run_id),
                    "tenant_id": str(tenant_id),
                    "brain_model_tier": brain_tier,
                    "brain_model_id": brain_model_id,
                    "intent_classification": _manager_intent.get("classification"),
                },
            )
            graph = build_supervisor_graph(
                model=_resolve_model(brain_model_id),
                checkpointer=get_checkpointer(),
            )
            terminal_state: dict[str, Any] = graph.invoke(
                initial_state,
                config={
                    "callbacks": [callback],
                    "configurable": {"thread_id": str(run_id)},
                },
            )
        # VT-47: a pause surfaces as the ``__interrupt__`` key in the returned
        # state (langgraph swallows GraphInterrupt internally and surfaces it
        # here — verified empirically against langgraph==1.2.0; it does NOT
        # raise to this caller). Map it to the NEW 'paused' terminal: the DBOS
        # workflow exits cleanly (non-error), the run sits at status='paused'
        # until resume/timeout. NO compose-output is forced (the agent has not
        # produced an owner-facing send — the owner is being ASKED, not told).
        if terminal_state.get("__interrupt__"):
            logger.info(
                "dispatch_brain: run PAUSED on owner-approval interrupt "
                "run=%s tenant=%s",
                str(run_id), str(tenant_id),
            )
            return DispatchResult(
                final_status="paused",
                terminal_path="paused",
                reason="owner_approval_pending",
            )
        # Inspect terminal state to determine final_status + terminal_path.
        terminal_path, final_status, reason, specialist_result = _classify_terminal(
            terminal_state
        )
        # VT-241: a fail-closed cohort rejection routes the owner message to
        # the Tier-A catch-all template (Cowork ruling a — no count-bearing
        # template until VT-108 approval). Owner gets "couldn't complete
        # your request"; the rejected-id detail stays in the operator audit
        # log. The reason discriminator keeps these runs distinguishable
        # from real campaign sends in observability/day-39 rollups.
        if reason is not None and reason.startswith(
            "campaign_not_sent_invalid_cohort"
        ):
            intent_or_trigger = "campaign_not_sent_invalid_cohort"
    except HardLimitExceeded as hle:
        _write_aborted_hard_limit(
            run_id=run_id,
            tenant_id=tenant_id,
            event=event,
            exc=hle,
        )
        return DispatchResult(
            final_status="aborted_hard_limit",
            terminal_path=None,
            reason=f"hard_limit:{hle.axis}",
        )
    except Exception:
        # Unhandled — re-raise to DBOS for retry. write_step happens via
        # DBOS's own error path; we don't pre-empt the workflow.
        logger.exception(
            "dispatch_brain unhandled exception; DBOS will retry",
            extra={"run_id": str(run_id), "tenant_id": str(tenant_id)},
        )
        raise

    # 2. compose_output envelope (Q2 Option A) — always emit, regardless
    # of terminal path. Empty/None ComposedOutput is acceptable when the
    # agent's intent didn't map to a template; the envelope still records
    # WHICH path produced WHAT.
    _write_compose_output(
        run_id=run_id,
        tenant_id=tenant_id,
        state=state,
        specialist_result=specialist_result,
        intent_or_trigger=intent_or_trigger,
        terminal_path=terminal_path or "terminal",
    )

    return DispatchResult(
        final_status=final_status,
        terminal_path=terminal_path,
        reason=reason,
    )


@dataclass(frozen=True)
class _CohortRejectedResult:
    """Carries the fail-closed rejection COUNT to the composer (VT-248).

    The composer reads ``specialist_result.output['rejected_count']`` — the
    same channel every terminal path uses — to populate the
    team_campaign_not_sent {{2}} count. Count ONLY reaches this object: no ids,
    no cross-tenant distinction (VT-241 privacy invariant; the full rejected-id
    list stays in the operator audit log written by collapse_node).
    """

    rejected_count: int

    @property
    def output(self) -> dict[str, int]:
        return {"rejected_count": self.rejected_count}


def _classify_terminal(
    terminal_state: dict[str, Any],
) -> tuple[TerminalPath, FinalStatus, str | None, Any]:
    """Determine terminal_path + final_status from supervisor final state.

    - ``terminated_without_spawn`` flag (set by ``orchestrator_terminal_node``)
      → terminal path; final_status='completed'.
    - ``campaign_plan`` field present → collapse path; final_status='completed'.
    - ``escalate_to_fazal`` ToolMessage in messages → escalated; final_status='escalated'.
    """
    messages = terminal_state.get("messages", []) or []
    for msg in reversed(messages):
        name = getattr(msg, "name", None)
        if name == "escalate_to_fazal":
            reason_text = getattr(msg, "content", None) or "agent_escalation"
            return ("escalated", "escalated", str(reason_text), None)

    # VT-241: a fail-closed cohort rejection (collapse rolled the campaign
    # back) — checked BEFORE campaign_plan, since the plan object is still
    # in state even though nothing persisted. The run itself completed
    # cleanly (fail-closed is a valid terminal — no new pipeline_runs.status
    # value, so no CHECK-constraint change). The owner-facing message
    # (count only — never which ids / cross-tenant) is composed downstream;
    # the full rejected-id list is already in the audit log (collapse_node).
    cohort_rejected = terminal_state.get("campaign_rejected")
    if cohort_rejected is not None:
        n = int(cohort_rejected.get("rejected_count", 0))
        # VT-248: thread the count to the composer so team_campaign_not_sent
        # gets its {{2}} param. Count only — no ids (VT-241 privacy invariant).
        return (
            "collapse",
            "completed",
            f"campaign_not_sent_invalid_cohort:{n}",
            _CohortRejectedResult(rejected_count=n),
        )

    campaign_plan = terminal_state.get("campaign_plan")
    if campaign_plan is not None:
        return ("collapse", "completed", None, campaign_plan)

    if terminal_state.get("terminated_without_spawn"):
        return ("terminal", "completed", None, None)

    # Fall-through: graph returned without a recognised terminal marker.
    # Treat as completed but log so future investigation can spot it.
    logger.warning(
        "dispatch_brain: unrecognised terminal state; defaulting to completed",
        extra={
            "state_keys": list(terminal_state.keys()),
            "message_count": len(messages),
        },
    )
    return ("terminal", "completed", None, None)


def _write_dispatch_entry(
    *, run_id: UUID, tenant_id: UUID, event: WebhookEvent
) -> None:
    """Emit the dispatch ENTRY envelope (step_kind='agent_invocation').

    Per Cowork brief correction: reuses the existing VT-179
    ``agent_invocation`` envelope. The old placeholder writer
    (``record_brain_pending``) is deleted by this PR.

    VT-464 D4: the AgentInvocationInput schema REQUIRES ``agent_role`` +
    ``reason`` (extra="forbid") and AgentInvocationEnvelope.output_envelope is
    ``None``. The previous writer put ``reason`` in output_envelope and packed
    undeclared keys (inbound_body_len / trigger / dispatched_at) into
    input_envelope, so every brain dispatch-entry envelope soft-failed
    validation (payload_validation_failed=True) — degrading Ops replay. The
    dispatch ``reason`` text now lives in the validated input_envelope (it
    still carries the "owner message" substring downstream readers assert on).
    """
    try:
        write_step(
            step_kind="agent_invocation",
            run_id=run_id,
            tenant_id=tenant_id,
            step_name="brain_dispatch_entry",
            input_envelope={
                "agent_role": "orchestrator",
                "reason": "substantive owner message — needs orchestrator-agent reasoning",
            },
            output_envelope=None,
            status="running",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dispatch_brain: agent_invocation entry write_step swallowed",
            extra={"exc": repr(exc), "run_id": str(run_id)},
        )


def _write_compose_output(
    *,
    run_id: UUID,
    tenant_id: UUID,
    state: SubscriberState,
    specialist_result: Any,
    intent_or_trigger: str,
    terminal_path: TerminalPath,
) -> None:
    """Compose + emit the ``compose_output`` envelope (VT-30 substrate)."""
    composed: Any = None
    try:
        composed = compose_owner_output(
            specialist_result=specialist_result,
            state=state,
            intent_or_trigger=intent_or_trigger,
        )
    except Exception as exc:  # noqa: BLE001
        # Composer is deterministic; failure is informative but shouldn't
        # block envelope emission. Record None for output fields.
        logger.warning(
            "dispatch_brain: compose_owner_output raised; emitting empty envelope",
            extra={"exc": repr(exc), "run_id": str(run_id)},
        )

    output_payload: dict[str, Any] = {}
    if composed is not None:
        output_payload = {
            "template_name": getattr(composed, "template_name", None),
            "content_sid": getattr(composed, "content_sid", None),
            "body_preview": (getattr(composed, "body", None) or "")[:200],
            "variables": getattr(composed, "variables", None),
            "envelope_hash": getattr(composed, "envelope_hash", None),
        }

    try:
        write_step(
            step_kind="compose_output",
            run_id=run_id,
            tenant_id=tenant_id,
            step_name="brain_dispatch_compose",
            input_envelope={
                "intent_or_trigger": intent_or_trigger,
                "terminal_path": terminal_path,
            },
            output_envelope=output_payload,
            status="completed",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dispatch_brain: compose_output write_step swallowed",
            extra={"exc": repr(exc), "run_id": str(run_id)},
        )


def _write_aborted_hard_limit(
    *,
    run_id: UUID,
    tenant_id: UUID,
    event: WebhookEvent,
    exc: HardLimitExceeded,
) -> None:
    """Emit the ``aborted_hard_limit`` envelope on HardLimitExceeded."""
    try:
        write_step(
            step_kind="aborted_hard_limit",
            run_id=run_id,
            tenant_id=tenant_id,
            step_name="brain_dispatch_aborted",
            input_envelope={
                "reason": f"hard_limit_exceeded:{exc.axis}",
                "inbound_body_len": len(event.body or ""),
            },
            output_envelope={
                "axis": exc.axis,
                "observed": float(exc.observed),
                "limit": float(exc.limit),
            },
            status="failed",
        )
    except Exception as inner:  # noqa: BLE001
        logger.warning(
            "dispatch_brain: aborted_hard_limit write_step swallowed",
            extra={"exc": repr(inner), "run_id": str(run_id)},
        )


class _NullDriver:
    """Minimal driver stand-in for the callback's ``check_mid_invocation``
    contract. The callback raises ``HardLimitExceeded`` itself when usage
    crosses any limit; this stub only needs to provide the limits + a
    no-op ``check_mid_invocation``-compatible surface for VT-125.

    Hard limits read from VT-125 constants — same enforcement envelope
    the driver would use.
    """

    # VT-125 limits — module constants, not env-tunable.
    tool_call_limit: int = int(os.environ.get(
        "ORCHESTRATOR_TOOL_CALL_HARD_LIMIT", "5"
    ))
    token_limit: int = int(os.environ.get(
        "ORCHESTRATOR_TOKEN_HARD_LIMIT", "10000"
    ))
    wall_clock_limit_s: float = float(os.environ.get(
        "ORCHESTRATOR_WALL_CLOCK_HARD_LIMIT_S", "120.0"
    ))
    cost_limit_paise: int = int(os.environ.get(
        "ORCHESTRATOR_COST_HARD_LIMIT_PAISE", "500"
    ))

    def check_mid_invocation(
        self,
        usage: OrchestratorUsage,
        *,
        run_id: UUID,
        tenant_id: UUID,
    ) -> None:
        if usage.tool_calls > self.tool_call_limit:
            raise HardLimitExceeded(
                axis="tool_calls",
                observed=usage.tool_calls,
                limit=self.tool_call_limit,
                run_id=run_id,
                tenant_id=tenant_id,
            )
        if usage.cumulative_tokens > self.token_limit:
            raise HardLimitExceeded(
                axis="tokens",
                observed=usage.cumulative_tokens,
                limit=self.token_limit,
                run_id=run_id,
                tenant_id=tenant_id,
            )
        if usage.wall_clock_s > self.wall_clock_limit_s:
            raise HardLimitExceeded(
                axis="wall_clock_s",
                observed=usage.wall_clock_s,
                limit=self.wall_clock_limit_s,
                run_id=run_id,
                tenant_id=tenant_id,
            )
        if usage.cost_paise > self.cost_limit_paise:
            raise HardLimitExceeded(
                axis="cost_paise",
                observed=usage.cost_paise,
                limit=self.cost_limit_paise,
                run_id=run_id,
                tenant_id=tenant_id,
            )


__all__ = [
    "DispatchResult",
    "FinalStatus",
    "TerminalPath",
    "dispatch_brain",
    "select_brain_model",
]
