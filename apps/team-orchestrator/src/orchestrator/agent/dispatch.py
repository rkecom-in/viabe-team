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
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

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
from orchestrator.observability.tm_audit import emit_tm_audit
from orchestrator.output_composer import compose_owner_output
from orchestrator.state import SubscriberState
from orchestrator.supervisor import (
    SpecialistNoOutputError,
    build_supervisor_graph,
)
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
_BRAIN_MODEL_SONNET = "claude-sonnet-5"  # routine/simple turns — Sonnet 5 (Fazal 2026-07-04, quality)
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


def _manager_memory_retrieval_enabled() -> bool:
    """VT-556 config gate — the manager reads VTR directives ONLY when this is explicitly on
    (default OFF). The observe-first posture: the retrieval seam lands dark, is flipped per-env
    (dev for the teach→pickup e2e) once validated. Prod stays off until authorized."""
    return os.environ.get("MANAGER_MEMORY_RETRIEVAL", "").strip().lower() in {"1", "true", "yes"}


def _build_manager_directive_block(tenant_id: UUID) -> str | None:
    """VT-556 — render the tenant's retrieval-eligible VTR directives as the ``## VTR directives``
    system block. Returns ``None`` when the config gate is off, retrieval fails, or there are no
    eligible directives (the manager then reasons without them). Content is already PII-redacted at
    write; only ``authority=vtr`` / global-seed rows that a VTR marked eligible surface here."""
    if not _manager_memory_retrieval_enabled():
        return None
    try:
        from orchestrator.agents.agent_memory import get_active_memory

        rows = get_active_memory(tenant_id, agent="manager")
    except Exception:  # noqa: BLE001 — directive retrieval is best-effort, like L1/business
        logger.warning(
            "dispatch: VTR-directive retrieval failed (tenant=%s); proceeding without", tenant_id
        )
        return None
    if not rows:
        return None
    lines = [
        "## VTR directives",
        "Human VTR operators set these strategic/behavioural directives for this tenant. Treat them "
        "as authoritative guidance and apply them in your decisions this run:",
    ]
    for r in rows:
        tag = "VTR" if r.get("authority") == "vtr" else str(r.get("authority") or "memory")
        lines.append(f"- [{tag}] {r['content']}")
    return "\n".join(lines)


def _build_manager_lessons_block(tenant_id: UUID) -> str | None:
    """VT-566 — the flywheel's read-back leg. Render this owner's captured lessons
    (``agent_corrections`` — the owner's own edit/reject/approve verdicts, authoritative) + weak
    outcome signals (``owner_feedback``, tier-branched) as the ``## Lessons from this owner`` (+
    optional ``## Outcome signals (weak)``) system block, so the Team-Manager reasons WITH the
    owner's accumulated verdicts on its NEXT run — closing the capture→retrieve loop.

    Gate: REUSES ``MANAGER_MEMORY_RETRIEVAL`` (the VTR-directive flag) — both blocks are 'manager
    memory read-back', so one env switch activates the family (fewer flags; the per-source
    granularity lives in the per-row ``retrieval_eligible`` gate, which ``record_correction`` sets at
    capture). Default OFF; dev flips it once validated. Returns ``None`` when the gate is off,
    retrieval fails, or there is nothing captured yet. Best-effort + PII-safe: content is redacted at
    capture; a read miss never breaks dispatch, and only presence booleans are ever logged."""
    if not _manager_memory_retrieval_enabled():
        return None
    try:
        from orchestrator.agents.correction_store import get_recent_lessons
        from orchestrator.agents.lesson_readback import (
            get_recent_outcome_signals,
            render_lessons_block,
        )

        lessons = get_recent_lessons(tenant_id)
        outcomes = get_recent_outcome_signals(tenant_id)
    except Exception:  # noqa: BLE001 — read-back is best-effort, like the directive block
        logger.warning(
            "dispatch: lesson read-back failed (tenant=%s); proceeding without", tenant_id
        )
        return None
    return render_lessons_block(lessons, outcomes)


def _build_manager_conversation_block(
    tenant_id: UUID, *, exclude_message_sid: str | None = None
) -> str | None:
    """VT-579 — the ALWAYS-ON conversation memory block. Renders the running DISTILLED summary (older
    turns folded, compact) ABOVE the last ≤20 turns within 24h (chronological, owner/assistant labeled)
    as the ``## Conversation (last 24h)`` system block, so the Team-Manager ALWAYS has the recent
    back-and-forth in context (Fazal, CL-2026-07-03: "always be part of the team-manager's LLM context").

    NO env gate — unlike the VTR-directive / lessons blocks (learned memory, retrieval-gated), this is
    CONVERSATION: it is always present. Best-effort: a read miss returns ``None`` and dispatch proceeds.
    ``exclude_message_sid`` drops the CURRENT inbound turn — it already rides as the HumanMessage, so
    surfacing it in the window too would double it."""
    try:
        from orchestrator.conversation_log import active_window, read_manager_summary

        summary = read_manager_summary(tenant_id)
        turns = active_window(tenant_id, exclude_message_sid=exclude_message_sid)
    except Exception:  # noqa: BLE001 — conversation memory is best-effort, like the L1/business blocks
        logger.warning(
            "dispatch: conversation-window assembly failed (tenant=%s); proceeding without", tenant_id
        )
        return None
    if not summary and not turns:
        return None
    lines = [
        "## Conversation (last 24h)",
        "The recent back-and-forth with this owner — your live memory of the chat. Oldest first; keep "
        "continuity and do NOT re-ask what is already answered here.",
    ]
    if summary:
        lines.append(f"Earlier (summarised): {summary}")
    for t in turns:
        who = "owner" if t.get("role") == "owner" else "assistant"
        text = str(t.get("text") or "").strip()
        if text:
            lines.append(f"{who}: {text}")
    return "\n".join(lines)


def _build_onboarding_state_block(tenant_id: UUID) -> str | None:
    """VT-588 — surface the LIVE onboarding step so the Team-Manager knows it is mid-setup and can
    field an OFF-SCRIPT owner message without losing the thread. The integration resume gate now falls
    a question / topic-switch / chat (anything that isn't the awaited store-address or 'done') THROUGH
    to this brain instead of a canned reprompt (shopify_onboarding VT-588); this block is what lets the
    brain answer it AND guide the owner back to the connect step. Read-only, best-effort (a read miss →
    no block). Inserted AFTER the cached prefix (a per-turn SystemMessage), so the VT-194 cache holds."""
    try:
        from orchestrator.onboarding.shopify_onboarding import (
            PHASE_AUTH,
            PHASE_DISCOVERY,
            has_live_resume,
            read_integration_state,
        )

        if not has_live_resume(tenant_id):
            return None
        state = read_integration_state(tenant_id) or {}
        phase = state.get("phase")
    except Exception:  # noqa: BLE001 — best-effort, like the L1/business/conversation blocks
        logger.warning(
            "dispatch: onboarding-state assembly failed (tenant=%s); proceeding without", tenant_id
        )
        return None

    if phase == PHASE_DISCOVERY:
        step = ("The owner is connecting their Shopify store and you are waiting for them to send their "
                "store address (it looks like yourstore.myshopify.com — then you send a one-tap connect link).")
    elif phase == PHASE_AUTH:
        step = ("The owner is connecting their Shopify store — you already sent a one-tap connect link and "
                "are waiting for them to approve it in the browser and reply 'done'.")
    else:
        step = "The owner is in the middle of connecting an integration."

    return (
        "## Onboarding in progress — you are mid-setup\n"
        f"{step}\n"
        "Their latest message reached you because it wasn't that exact next step. So:\n"
        "- ANSWER their actual message first — directly, honestly, and helpfully (it may be a real "
        "question, a change of mind, or a different topic).\n"
        "- Then, in the SAME reply, gently guide them back to the step above.\n"
        "- If they ALREADY gave a detail earlier in the conversation (e.g. their store address is in the "
        "window above), do NOT ask for it again — acknowledge you have it and continue.\n"
        "- NEVER claim the store is connected / the step is done until it actually is."
    )


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

    # VT-514 GETS — inbound_received audit spine row (fail-soft, conn=None).
    # tenant_id/run_id are the dispatch_brain params; no raw body/phone (ids +
    # length only — emit_tm_audit redacts defensively).
    emit_tm_audit(
        event_layer="gets",
        event_kind="inbound_received",
        actor="team_manager",
        tenant_id=tenant_id,
        run_id=run_id,
        summary="brain dispatch entry — owner message routed to Team-Manager",
        input={
            "message_type": getattr(event, "message_type", None),
            "twilio_message_sid": getattr(event, "twilio_message_sid", None),
            "body_len": len(event.body or ""),
            "dupe_status": getattr(event, "dupe_status", None),
        },
    )

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

    # VT-556: the VTR teach-loop retrieval seam — inject retrieval-eligible VTR strategy/
    # behavioural directives as a separate ``## VTR directives`` system block so the Team-Manager
    # PICKS THEM UP on its next run (closes the human-as-teacher → learn loop the C3 memory backs).
    # Config-gated by MANAGER_MEMORY_RETRIEVAL (default OFF) — double safety with the per-row
    # retrieval_eligible flip: BOTH must be true for a directive to steer a decision. Best-effort
    # (a read miss never breaks dispatch) + inserted AFTER the cached prefix (a per-turn
    # SystemMessage), so the VT-194 cache still hits.
    directive_block = _build_manager_directive_block(tenant_id)
    if directive_block:
        _messages.insert(0, SystemMessage(content=directive_block))

    # VT-566: the flywheel's read-back leg — inject this owner's captured lessons (their own
    # edit/reject/approve verdicts, authoritative) + weak outcome signals (tier-branched) as a
    # separate ``## Lessons from this owner`` system block, so a captured correction/approval steers
    # the manager's NEXT run. Same double gate as the VTR-directive block above (MANAGER_MEMORY_
    # RETRIEVAL env flag + per-row retrieval_eligible). Best-effort + inserted AFTER the cached prefix
    # (a per-turn SystemMessage), so the VT-194 cache still hits.
    lessons_block = _build_manager_lessons_block(tenant_id)
    if lessons_block:
        _messages.insert(0, SystemMessage(content=lessons_block))

    # VT-461: inject the Manager-intent signal as a separate system block so the
    # Team-Manager brain reads it as a prior (the prompt's "## Manager intent signal"
    # contract) when deciding handle-directly-vs-delegate. Reuses the classification the
    # edge router already computed — no extra Haiku call. Inserted AFTER the cached system
    # prefix (it's a per-turn SystemMessage in `messages`, not the cached system_prompt), so
    # the VT-194 cache still hits. Absent/failed classify → no block; the brain still works.
    intent_block = _build_manager_intent_block(_manager_intent)
    if intent_block:
        _messages.insert(0, SystemMessage(content=intent_block))

    # VT-579: the ALWAYS-ON conversation memory — the running distilled summary + the last ≤20 turns
    # within 24h (both directions), so the Team-Manager ALWAYS carries the recent chat in its context
    # (Fazal: "always be part of the team-manager's LLM context"). NOT env-gated (this is conversation,
    # not learned/VTR memory — those two blocks above ARE gated). Inserted AFTER the cached prefix (a
    # per-turn SystemMessage), so the VT-194 cache still hits. The current inbound is excluded (it already
    # rides as the HumanMessage) — the runner logged it to conversation_log just before this dispatch.
    conversation_block = _build_manager_conversation_block(
        tenant_id, exclude_message_sid=getattr(event, "twilio_message_sid", None)
    )
    if conversation_block:
        _messages.insert(0, SystemMessage(content=conversation_block))

    # VT-588: the ONBOARDING-STATE block — when an onboarding integration hand-off is live and the
    # owner's message fell off-script (the resume gate passed it through, VT-588), tell the brain which
    # step it is mid-way through so it answers the off-script message AND guides the owner back, instead
    # of dropping the thread. Best-effort + per-turn SystemMessage (cache holds). None when not onboarding.
    onboarding_state_block = _build_onboarding_state_block(tenant_id)
    if onboarding_state_block:
        _messages.insert(0, SystemMessage(content=onboarding_state_block))

    # VT-514 GETS — retrieval audit spine row: which context sources hit
    # (presence flags only; the redacted block CONTENT rides the KNOWS row).
    emit_tm_audit(
        event_layer="gets",
        event_kind="retrieval",
        actor="team_manager",
        tenant_id=tenant_id,
        run_id=run_id,
        summary="assembled L1 / business / manager-intent context blocks",
        result={
            "l1_present": bool(l1_block),
            "business_present": bool(business_block),
            "directive_present": bool(directive_block),
            "lessons_present": bool(lessons_block),
            "intent_present": bool(intent_block),
            # VT-579: whether the always-on conversation window (+summary) was present this turn.
            "conversation_present": bool(conversation_block),
            # VT-588: whether the onboarding-state block was present (a live integration hand-off).
            "onboarding_state_present": bool(onboarding_state_block),
            "intent_classification": _manager_intent.get("classification"),
        },
    )

    # VT-514 snapshot plumbing + KNOWS context_assembled spine row. snapshot_id
    # = sha256 of the assembled system blocks; THIS row is the snapshot STORE the
    # id points at (carries the REDACTED blocks). Best-effort: a hash/emit
    # failure must never break dispatch.
    _snapshot_id: str | None = None
    try:
        import hashlib

        _blocks_for_hash = "\n--\n".join(
            b
            for b in (
                l1_block,
                business_block,
                directive_block,
                lessons_block,
                intent_block,
                conversation_block,  # VT-579: the window is part of the assembled context snapshot
            )
            if b
        )
        _snapshot_id = hashlib.sha256(_blocks_for_hash.encode("utf-8")).hexdigest()
    except Exception:  # noqa: BLE001 — snapshot is best-effort
        _snapshot_id = None
    emit_tm_audit(
        event_layer="knows",
        event_kind="context_assembled",
        actor="team_manager",
        tenant_id=tenant_id,
        run_id=run_id,
        snapshot_id=_snapshot_id,
        summary="assembled Team-Manager context snapshot",
        input={
            "l1_block": l1_block,
            "business_block": business_block,
            "lessons_block": lessons_block,
            "intent_block": intent_block,
        },
    )
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
        with observability_context(run_id=run_id, tenant_id=tenant_id, snapshot_id=_snapshot_id):
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
            # VT-565 — park this run's manager_task at 'waiting_owner' so the stalled-task reaper
            # (which scans only planned/running/verifying) never mis-reads an awaiting-approval
            # task as stalled and walks it to dead_letter. Fail-soft.
            from orchestrator.manager.task_producer import on_run_paused

            on_run_paused(tenant_id, run_id)
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
        # VT-565 — an aborted run's task settles to 'failed' (an honest terminal the reaper leaves
        # alone; re-running would just re-hit the ceiling). Fail-soft, no-op if no task was minted.
        from orchestrator.manager.task_producer import on_run_failed

        on_run_failed(tenant_id, run_id, reason=f"hard_limit:{hle.axis}")
        return DispatchResult(
            final_status="aborted_hard_limit",
            terminal_path=None,
            reason=f"hard_limit:{hle.axis}",
        )
    except SpecialistNoOutputError as snoe:
        # VT-492 — a specialist dispatch terminated with NO usable output
        # (status in {refused, invalid, terminated}; e.g. the SR retry emitted
        # non-dict terminal text → agent_terminal_no_dict). The specialist
        # already routed its FailureRecord (the invalid output stays
        # observable). Convert the dead-end to a CLEAN 'escalated' terminal —
        # the SAME convert-don't-orphan shape as the HardLimitExceeded branch
        # above (and VT-484's tool-error middleware): a bare re-raise here
        # escapes to webhook_pipeline_run BEFORE close_webhook_run, orphaning
        # the run at status='running' until the VT-481 reaper. 'escalated' is a
        # valid pipeline_runs.status terminal (mig-052 CHECK) AND a VT-88
        # _UNRESOLVED status, so close_webhook_run records a terminal status
        # and maybe_escalate_support acks the owner — never silence. PII-safe:
        # the reason carries the specialist + terminal status only (no body).
        logger.warning(
            "dispatch_brain: specialist produced no usable output; resolving "
            "to a clean 'escalated' terminal (VT-492 — preventing an orphaned "
            "status='running' hang)",
            extra={
                "run_id": str(run_id),
                "tenant_id": str(tenant_id),
                "specialist": snoe.specialist,
                "agent_status": snoe.status,
            },
        )
        # VT-565 — the specialist produced nothing usable; settle the task to 'failed'. Fail-soft.
        from orchestrator.manager.task_producer import on_run_failed

        on_run_failed(tenant_id, run_id, reason=f"specialist_no_output:{snoe.specialist}")
        return DispatchResult(
            final_status="escalated",
            terminal_path="escalated",
            reason=f"specialist_no_output:{snoe.specialist}:{snoe.status}",
        )
    except Exception:
        # Unhandled — re-raise to DBOS for retry. write_step happens via
        # DBOS's own error path; we don't pre-empt the workflow.
        logger.exception(
            "dispatch_brain unhandled exception; DBOS will retry",
            extra={"run_id": str(run_id), "tenant_id": str(tenant_id)},
        )
        raise

    # VT-589 — a no-spawn "handle-directly" turn: the manager brain wrote its
    # answer as the final AIMessage, but nothing downstream transmits it, so the
    # run completes silent and runner.py fires the generic D1 fallback instead of
    # the real answer. Send it here. Terminal path ONLY — the specialist / collapse
    # / escalated paths transmit their own owner-facing message, so gating on
    # "terminal" avoids a double-send. send_freeform_ack records the assistant turn,
    # which auto-suppresses the D1 completed-no-reply fallback (VT-583). Best-effort:
    # _maybe_send_manager_reply never raises.
    if terminal_path == "terminal" and final_status == "completed":
        _maybe_send_manager_reply(tenant_id, event, terminal_state)

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

    # VT-565 — close the run's manager_task at its terminal (a no-op when the manager answered
    # directly and minted no task). A completed run → 'completed' + a 'done' step; an escalation →
    # 'failed'. 'paused' / 'aborted_hard_limit' already returned above. Fail-soft.
    if final_status == "completed":
        from orchestrator.manager.task_producer import on_run_completed

        on_run_completed(tenant_id, run_id)
    elif final_status == "escalated":
        from orchestrator.manager.task_producer import on_run_failed

        on_run_failed(tenant_id, run_id, reason=reason)

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


def _last_manager_reply_text(terminal_state: dict[str, Any]) -> str | None:
    """VT-589 — the trailing AIMessage's OWN text, or None.

    Scans ``terminal_state["messages"]`` in REVERSE and evaluates the FIRST
    ``AIMessage`` it reaches (i.e. the manager's LAST turn). ToolMessage /
    HumanMessage are skipped. Handles both content shapes: a plain ``str``, and
    a list of content blocks (dicts with ``type == "text"`` → their ``"text"``
    joined). The result is stripped; empty → ``None``.

    Deliberately does NOT dig past that trailing AIMessage: if it holds only
    ``tool_calls`` with empty text, we return ``None`` rather than surfacing an
    earlier (stale) reasoning turn. Only the manager's own final answer transmits.
    """
    for msg in reversed(terminal_state.get("messages") or []):
        if not isinstance(msg, AIMessage):
            continue
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            return content.strip() or None
        if isinstance(content, list):
            text = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            return text or None
        # Trailing AIMessage with a non-str/non-list (or None) content → no
        # transmittable text; do NOT walk back into earlier reasoning.
        return None
    return None


def _reply_is_incomplete(terminal_state: dict[str, Any]) -> bool:
    """VT-591 — does the manager's trailing reply NEED a compose-completion pass?

    True when the trailing message is NOT a complete owner reply — the signals we
    can detect deterministically at the transmit seam:

    - text is None/empty (nothing to send; ``_last_manager_reply_text`` already
      returns None for a tool-calls-only trailing AIMessage), OR
    - ``text.rstrip()`` ends with a colon — the "explainer intro" truncation the
      ReAct loop leaves behind ("here's how it works end to end:") when the agent
      emits an intro and stops with no tool call, OR
    - the trailing AIMessage still carried a non-empty ``tool_calls`` (it DEFERRED
      the real content to a call that never resolved into a final answer here).

    Pure + cheap: this runs on EVERY no-spawn transmit turn, and a False result
    means the caller SKIPS the extra LLM call entirely (the cost guard — a good
    reply pays nothing). Unit-tested in test_manager_reply_complete.py.
    """
    text = _last_manager_reply_text(terminal_state)
    if not text:
        return True
    if text.rstrip().endswith(":"):
        return True
    # The trailing AIMessage (same one _last_manager_reply_text evaluated) still
    # holding tool_calls → it deferred; complete it. Reverse-scan to that first
    # AIMessage, mirroring _last_manager_reply_text's scan.
    for msg in reversed(terminal_state.get("messages") or []):
        if isinstance(msg, AIMessage):
            return bool(getattr(msg, "tool_calls", None))
    return False


# VT-591 — the compose-completion system prompt. Bound with NO tools, so the model
# MUST emit a final message (it cannot defer into another dangling intro / tool
# call). Kept inline (single source; not a template file) + concise.
_COMPOSE_COMPLETION_SYSTEM = (
    "You are the Viabe Team-Manager writing the FINAL WhatsApp reply to the OWNER of a "
    "small Indian business. Output ONLY the complete message you would send them — no "
    "narration, no meta-commentary, no third person, never describe what you are doing. "
    'Write in second person ("you", "your store") and in the owner\'s language (match '
    "the conversation). Your reply MUST be complete: never end on a dangling colon or a "
    "half-sentence, and never stop at an intro that only promises an explanation — "
    "actually give the full answer. If you lack a specific fact, say so honestly and "
    "give the next step — NEVER invent a number, date, status, or detail. If an "
    "onboarding-state block is present, answer the owner's message AND gently guide them "
    "back to the pending step."
)


def _compose_completed_reply(
    tenant_id: UUID, event: WebhookEvent, terminal_state: dict[str, Any]
) -> str | None:
    """VT-591 — ONE focused, no-tools LLM call that turns the manager's INCOMPLETE
    trailing reply into the COMPLETE owner-facing WhatsApp message.

    Called ONLY when ``_reply_is_incomplete`` fired (the cost guard: a complete
    reply never reaches here). NO tools are bound, so the model MUST return final
    text — it cannot defer into another dangling intro. Same model TIER as the
    brain conversational hot path: this is a plain conversational rewrite, not a
    reasoning task, so it uses ``_BRAIN_MODEL_SONNET`` (the SAME constant
    ``select_brain_model`` returns for routine turns) via the SAME ``_resolve_model``
    builder — deliberately NOT Opus.

    Compose context assembles (a) the recent conversation window (reused from the
    dispatch-side ``_build_manager_conversation_block``, current inbound excluded —
    it rides as its own labeled line), (b) the onboarding-state block if any (so the
    completion answers AND guides back mid-onboarding), (c) the owner's latest
    inbound text, (d) the manager's DRAFT so far to finish.

    Fail-soft: returns the stripped completion, or ``None`` on empty / any exception
    (the caller then falls back to the raw trailing text, then to the D1 net). MUST
    NEVER raise into ``_maybe_send_manager_reply``.
    """
    try:
        draft = _last_manager_reply_text(terminal_state) or ""
        owner_text = event.body or ""
        # Reuse the dispatch-side window so the completion keeps continuity (and does
        # not re-ask). Exclude the current inbound — it rides as its own labeled line
        # below (mirrors the dispatch-side exclude_message_sid, avoids doubling).
        conversation_block = _build_manager_conversation_block(
            tenant_id, exclude_message_sid=getattr(event, "twilio_message_sid", None)
        )
        onboarding_block = _build_onboarding_state_block(tenant_id)

        human_parts: list[str] = []
        if conversation_block:
            human_parts.append(conversation_block)
        if onboarding_block:
            human_parts.append(onboarding_block)
        human_parts.append(f"## The owner just messaged you\n{owner_text}")
        human_parts.append(
            "## Your draft so far — finish it into the complete message\n"
            f"{draft}"
        )
        human_content = "\n\n".join(human_parts)

        response = _resolve_model(_BRAIN_MODEL_SONNET).invoke(
            [
                SystemMessage(content=_COMPOSE_COMPLETION_SYSTEM),
                HumanMessage(content=human_content),
            ]
        )
        # Handle both content shapes (str + list-of-blocks), like _last_manager_reply_text.
        raw = getattr(response, "content", None)
        if isinstance(raw, str):
            text = raw.strip()
        elif isinstance(raw, list):
            text = "".join(
                block.get("text", "")
                for block in raw
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
        else:
            text = ""
        return text or None
    except Exception:  # noqa: BLE001 — compose-completion is best-effort; never raise
        logger.warning(
            "VT-591: compose-completion failed (fail-soft) tenant=%s", tenant_id
        )
        return None


def _maybe_send_manager_reply(
    tenant_id: UUID, event: WebhookEvent, terminal_state: dict[str, Any]
) -> None:
    """VT-589 — transmit the manager's conversational answer on a no-spawn,
    handle-directly turn. Best-effort: MUST NEVER raise into ``dispatch_brain``.

    The manager brain writes its reply as the final ``AIMessage.content``, but on a
    ``terminated_without_spawn`` turn nothing downstream sends it. The run would then
    complete SILENT and ``runner.py``'s D1 fallback (VT-583) would fire a generic
    "on it" line INSTEAD of the real answer. This seam sends the actual text.

    Called ONLY for ``terminal_path == "terminal"`` (see call site): the specialist /
    collapse / escalated paths transmit their own owner-facing message elsewhere, so
    gating on the terminal path is what prevents a double-send. We reuse
    ``send_freeform_ack`` because it RECORDS the assistant turn into ``conversation_log``
    — exactly what ``runner._brain_emitted_owner_reply`` reads — so recording here
    AUTO-suppresses the D1 fallback (no double-send). The manager holds NO send tool by
    construction (the tool_guardrail invariant), so this owner send lives here in the
    deterministic dispatch seam rather than as a brain tool-call.

    VT-591: the trailing reply sometimes truncates — the agent emits an "explainer
    intro" ending in a dangling colon (or defers to a tool_call) as its complete turn
    and stops. When ``_reply_is_incomplete`` fires, run ONE focused no-tools
    compose-completion LLM call and send THAT instead; a COMPLETE reply skips the call
    entirely (cost guard). Fail-soft chain: completed → raw trailing text → D1 net.
    Exactly ONE send.
    """
    recipient = getattr(event, "sender_phone", None)
    if not recipient:
        return

    raw = _last_manager_reply_text(terminal_state)
    # VT-591: complete an INCOMPLETE trailing reply; leave a complete one untouched
    # (no extra LLM call). The compose call is itself fail-soft (returns None), so on
    # its miss we fall back to the raw trailing text, then to runner.py's D1 net.
    if _reply_is_incomplete(terminal_state):
        completed = _compose_completed_reply(tenant_id, event, terminal_state)
        body, path = (completed, "completed") if completed else (raw, "raw-fallback")
    else:
        body, path = raw, "raw"

    if not body:
        # Nothing transmittable — leave runner.py's D1 fallback (VT-583) as the net.
        return
    try:
        from orchestrator.owner_surface.freeform_acks import send_freeform_ack

        send_freeform_ack(tenant_id, recipient, body)
        logger.info(
            "VT-589/591: transmitted manager direct reply (tenant=%s path=%s)",
            tenant_id,
            path,
        )
    except Exception:  # noqa: BLE001 — best-effort; the D1 fallback remains the net
        logger.warning(
            "VT-589: manager direct-reply send failed (fail-soft) tenant=%s", tenant_id
        )


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
