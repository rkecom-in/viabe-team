"""Sales Recovery specialist — Agent SDK skeleton (VT-32).

This module is the real specialist that the orchestrator's specialist
dispatch will eventually call (currently still routed through the stub —
see ``sales_recovery_stub.py``; switching dispatch call sites is a later
subtask).

Tier 2 plumbing only (CL-242)
-----------------------------
This module MUST NOT touch the database, send WhatsApp messages, or
mutate LangGraph state directly. It receives a typed context, runs an
agent loop on the Anthropic Messages API, and returns a typed
``AgentResult``. The orchestrator owns persistence + side effects.

VT-35 hook seams
----------------
The two well-named functions below are the seams VT-35's four hard-limit
enforcers attach to. Do NOT collapse them into a single opaque call:

  - ``_run_one_turn`` — the *per-turn boundary*. Each call is one
    Messages.create round-trip. The depth tracker and token meter
    instrument here.
  - ``_dispatch_tool`` — the *tool-dispatch seam*. Each call is one tool
    invocation (success OR failure). The tool counter instruments here.

The wall-clock timer attaches at ``run_sales_recovery_agent`` entry/exit
(it watches the whole run); the cancel coordinator orchestrates a clean
break across all four enforcers.

The placeholder prompt
----------------------
This PR ships with a placeholder system prompt that asks the model to
emit ``{"status": "placeholder"}`` and stop. The real prompt is a later
subtask. The placeholder text is intentionally short and free of
instruction-tuning: it is for plumbing validation, not behaviour
validation.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import yaml
from anthropic import Anthropic, APITimeoutError

from orchestrator.agent.cost import compute_cost_paise
from orchestrator.agent.limits import (
    PER_TURN_HTTP_TIMEOUT_S,
    CancellationContext,
    DepthTracker,
    TokenMeter,
    ToolCounter,
    WallclockTimer,
)
from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan
from orchestrator.agent.self_evaluate import (
    GateAction,
    SelfEvaluateGate,
    SelfEvaluator,
)
from orchestrator.agent.types import AgentResult
from orchestrator.error_router import route_failure
from orchestrator.failures import FailureRecord, FailureType, HardLimitAxis

# Sales Recovery system prompt v1.0 (VT-33 / VT-4.2). Loaded from the
# markdown file under prompts/; the file is the source of truth and is
# CI-gated at 4000 tokens (gate-sr-agent-prompt-token-cap). Prompt edits
# go through versioned files (sales_recovery_v1.md -> _v2.md ...); major
# revisions are Type 2 governance.
_SR_AGENT_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "sales_recovery_v1.md"
)
_SR_AGENT_SYSTEM_PROMPT = _SR_AGENT_PROMPT_PATH.read_text(encoding="utf-8")

# Markdown code-fence stripper. Matches a recognised fence shape and
# captures the inner content. NARROW by design: it does not extract a
# JSON object from arbitrary surrounding prose — that would mask
# genuinely malformed output. Recognised: ``` or ```json (case-
# insensitive) on its own line, optional whitespace, closing ``` on its
# own line.
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?[ \t]*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)

# Per-response output cap passed to ``messages.create``. Distinct from the
# run-level hard-limit ceiling below — ``max_tokens`` here is "max length
# of ONE response", which is what the Messages API expects (passing the
# 80K run-level budget here also trips the SDK's non-streaming 10-minute
# timeout guard). The placeholder canary response is ~10 tokens; 1024 is
# generous headroom. Real-prompt tuning lands with the real prompt.
_MAX_OUTPUT_TOKENS_PER_TURN = 1024

# Run-level hard-limit ceiling. VT-35's token meter enforces a CUMULATIVE
# 80K cap across every turn in one run. This constant lives here only as
# a documented reference for AgentResult semantics (CL-242); it is NOT
# wired into any SDK call. VT-35 will read this when wiring the token
# meter. Renaming this constant requires updating VT-35's enforcer.
_RUN_LEVEL_TOKEN_HARD_LIMIT = 80_000

# Extended thinking is intentionally NOT wired for VT-32's placeholder
# canary path — a placeholder that emits ``{"status": "placeholder"}``
# does zero reasoning, so a thinking budget on the call is meaningless
# AND tripped a 400 from the API when budget_tokens > max_tokens.
# The real agent's thinking policy (whether to enable, with what
# budget) is a VT-4.2-era per-turn reasoning decision, intertwined
# with VT-35's depth tracker. VT-32 must not pre-empt it — when that
# work lands, re-introduce ``thinking={"type": "enabled",
# "budget_tokens": N}`` where N < _MAX_OUTPUT_TOKENS_PER_TURN (the API
# enforces that relationship). Do NOT smuggle a thinking budget in
# here today.

# Loop safety upper bound (NOT a budget). VT-35's depth (≤8), tool-call
# (≤25), wallclock (≤300s) and token (≤80K) enforcers are the real
# budgets — this cap exists only as the final guard against a runaway
# loop if every enforcer somehow failed to fire. Sized comfortably above
# the tool-call cap so tests that exercise the 25/26 boundary can run
# without bumping into it.
_MAX_TURNS_PER_RUN = 50


_MODELS_YAML = (
    Path(__file__).resolve().parents[3] / "config" / "models.yaml"
)


@dataclass
class SalesRecoveryContext:
    """Placeholder context type for VT-32.

    The real ``SalesRecoveryContext`` bundle (full Context Composer
    output) is a later subtask. For VT-32 the agent is run with a
    placeholder prompt and no tools, so it does not consume context
    fields — but the function signature MUST be stable so dispatch
    callers can wire to it now and the bundle can fill in later.

    ``tenant_id`` and ``run_id`` are required (Pillar 3 — every run is
    tenant-scoped; the orchestrator never invokes a specialist without
    them); other fields land later.
    """

    tenant_id: str
    run_id: str


def _resolve_model(agent_name: str = "sales_recovery") -> str:
    """Return the model id for ``agent_name`` per ``VIABE_ENV``.

    ``VIABE_ENV in {'production'}`` → ``production`` slot; everything else
    (test/dev/canary or unset) → ``test`` slot. The unset default is
    test/Haiku — never silently fall through to Opus in a development
    environment.
    """
    env = os.environ.get("VIABE_ENV", "test").lower()
    slot = "production" if env == "production" else "test"
    with open(_MODELS_YAML) as f:
        config = yaml.safe_load(f)
    return cast(str, config[agent_name][slot])


def _dispatch_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    tools: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a tool call. VT-35 tool-counter seam.

    For VT-32 ``tools`` is always ``{}`` (no real tools yet). Calling
    this with an empty registry returns a structured ``tool_error``
    result so the agent loop can append it as a ``tool_result`` and
    finish cleanly — instead of raising and unwinding the loop.

    VT-35's tool counter wraps this function: every call increments the
    counter regardless of whether the dispatch succeeded.
    """
    if tool_name not in tools:
        return {
            "tool_name": tool_name,
            "is_error": True,
            "content": f"unknown tool: {tool_name}",
        }
    handler = tools[tool_name]
    try:
        return cast(dict[str, Any], handler(tool_input))
    except Exception as exc:  # noqa: BLE001 — surface as tool_error result
        return {"tool_name": tool_name, "is_error": True, "content": str(exc)}


def _run_one_turn(
    client: Anthropic,
    *,
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    timeout: float = PER_TURN_HTTP_TIMEOUT_S,
) -> Any:
    """One Messages.create round-trip. VT-35 per-turn / token-meter seam.

    Isolated so VT-35's enforcers can instrument exactly one turn at a
    time and so tests can mock at this boundary (zero real API calls in
    CI by patching this function).

    ``timeout`` (VT-35): per-turn HTTP ceiling passed to httpx. Caps the
    wall-clock cost of any single round-trip even if the model hangs;
    the run-level wall-clock budget is enforced separately by
    WallclockTimer at the turn boundary.
    """
    # mypy: anthropic.Messages.create's overloads are TypedDict-heavy
    # (MessageParam, ThinkingConfigEnabledParam) — typing the plain-dict
    # messages list to match would add noise without value for a Phase 1
    # placeholder loop. The shape is asserted at runtime by the SDK.
    return client.messages.create(
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS_PER_TURN,
        system=system_prompt,
        messages=messages,  # type: ignore[arg-type]
        tools=[],
        timeout=timeout,
    )


def _extract_text(content_blocks: list[Any]) -> str:
    """Concatenate every TextBlock's text from a response's content."""
    out: list[str] = []
    for block in content_blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            out.append(text)
    return "".join(out)


def _parse_placeholder_output(text: str) -> dict[str, Any] | None:
    """Best-effort parse of the placeholder JSON. Returns None on failure.

    Tolerates ONE level of markdown code-fence wrapping (``` or ```json)
    — models intermittently wrap JSON in a fence even when the prompt
    forbids it. The strip is narrow: a recognised fence shape only,
    NOT a loose "first { to last }" extraction. Genuinely malformed or
    truncated output must still return None so the caller classifies
    ``status='invalid'`` rather than silently inventing a parse.
    """
    import json

    text = text.strip()
    if not text:
        return None
    fence_match = _CODE_FENCE_RE.match(text)
    if fence_match is not None:
        text = fence_match.group("body").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def run_sales_recovery_agent(
    context: SalesRecoveryContext,
    *,
    evaluator: SelfEvaluator | None = None,
) -> AgentResult:
    """Run the sales_recovery specialist; return a structured ``AgentResult``.

    Hand-written agent loop on the Anthropic Messages API (CL-242).
    Tier-2 plumbing: no DB, no side effects — the orchestrator owns
    those. The orchestrator measures (VT-35 hard limits attach here),
    the agent does not see its own usage.

    VT-35: four hard-limit enforcers — TokenMeter, ToolCounter,
    DepthTracker, WallclockTimer — instantiate per invocation (budgets
    do not carry across dispatches) and report into the shared
    CancellationContext. First signal wins. On cancel: status becomes
    'terminated', terminated_by is the winning axis, terminated_reason
    is the enforcer's message, and a FailureRecord(AGENT_HARD_LIMIT_BREACH)
    is emitted to the error router. cost_paise STILL accrues — the API
    spend already happened.

    VT-36 self-evaluate gate (``evaluator`` parameter): when provided,
    a draft CampaignPlan that the model produces at terminal goes
    through the gate before being returned. Two-revise-then-fail policy
    per ``config/self_evaluate.yaml``. ``evaluator=None`` skips the
    gate — that is the current production default because VT-50 (the
    real Opus-backed evaluator) is backlog. When VT-50 lands, every
    caller starts passing it.
    """
    start = time.monotonic()
    client = Anthropic()
    model = _resolve_model("sales_recovery")
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "begin"},
    ]
    tools: dict[str, Any] = {}  # VT-32: no real tools yet.
    raw_messages: list[dict[str, Any]] = list(messages)

    # VT-35 enforcers — fresh per invocation.
    ctx = CancellationContext()
    token_meter = TokenMeter(ctx)
    tool_counter = ToolCounter(ctx)
    depth_tracker = DepthTracker(ctx)
    wallclock_timer = WallclockTimer(ctx)

    # VT-36 self-evaluate gate — fresh per invocation when an evaluator
    # is provided. Shares the same ToolCounter so the gate's calls land
    # on the 25-call cap (Pillar 1 / VT-35 precedence).
    gate: SelfEvaluateGate | None = (
        SelfEvaluateGate(evaluator=evaluator, ctx=ctx, tool_counter=tool_counter)
        if evaluator is not None
        else None
    )

    input_tokens_used = 0
    output_tokens_used = 0
    tool_calls_made = 0
    status: str = "completed"
    output: dict[str, Any] | None = None

    for _ in range(_MAX_TURNS_PER_RUN):
        # Pre-turn checks: wallclock (the only enforcer that can fire
        # without a per-turn event source — accumulated time).
        wallclock_timer.check()
        if ctx.is_cancelled:
            break

        try:
            response = _run_one_turn(
                client,
                model=model,
                system_prompt=_SR_AGENT_SYSTEM_PROMPT,
                messages=messages,
            )
        except APITimeoutError:
            # Per-turn HTTP ceiling tripped — one round-trip exceeded
            # PER_TURN_HTTP_TIMEOUT_S. The underlying condition is "this
            # run is taking too long"; convert to a wall-clock hard
            # limit so the cancel path runs uniformly (terminated_by =
            # wall_clock, FailureRecord routed). Distinguished from the
            # turn-boundary check by the reason string.
            wallclock_timer.ctx.signal(
                HardLimitAxis.WALL_CLOCK,
                f"per-turn HTTP timeout exceeded {PER_TURN_HTTP_TIMEOUT_S}s",
            )
            break

        usage = getattr(response, "usage", None)
        if usage is not None:
            in_t = int(getattr(usage, "input_tokens", 0) or 0)
            out_t = int(getattr(usage, "output_tokens", 0) or 0)
            input_tokens_used += in_t
            output_tokens_used += out_t
            token_meter.record_turn(input_tokens=in_t, output_tokens=out_t)

        # Depth: if the previous beat was a tool dispatch, THIS turn is
        # the post-tool reasoning step — increment depth.
        depth_tracker.record_reasoning_turn()

        # Post-turn cancellation check (token/depth may have signalled).
        if ctx.is_cancelled:
            break

        content_blocks = list(getattr(response, "content", []) or [])
        raw_messages.append(
            {"role": "assistant", "content": [_block_to_dict(b) for b in content_blocks]}
        )

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "tool_use":
            tool_results: list[dict[str, Any]] = []
            for block in content_blocks:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_calls_made += 1
                tool_counter.record_dispatch()
                depth_tracker.record_tool_dispatch()
                if ctx.is_cancelled:
                    break
                result = _dispatch_tool(
                    block.name, dict(block.input or {}), tools
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result.get("content", ""),
                        "is_error": bool(result.get("is_error", False)),
                    }
                )
            if ctx.is_cancelled:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": [_block_to_dict(b) for b in content_blocks],
                }
            )
            messages.append({"role": "user", "content": tool_results})
            raw_messages.append({"role": "user", "content": tool_results})
            continue

        # No tool_use → terminal. Extract output.
        text = _extract_text(content_blocks)
        output = _parse_placeholder_output(text)
        if output is not None and output.get("status") == "placeholder":
            status = "placeholder"
            break
        if stop_reason == "refusal":
            status = "refused"
            break
        if output is None:
            status = "invalid"
            break

        # VT-36 self-evaluate gate. Without a configured evaluator the
        # gate is skipped (VT-50 deferral); the draft ships with
        # self_evaluate_status='not_yet_evaluated' (schema default).
        if gate is None:
            status = "completed"
            break

        try:
            draft_plan = parse_campaign_plan(output)
        except Exception:
            # Model emitted something that wasn't a valid CampaignPlan —
            # the gate has no draft to evaluate.
            status = "invalid"
            break

        gate_outcome = gate.run(draft_plan)

        # Every gate.run() — emit a self_evaluate event so production
        # REVISE-frequency and per-attempt verdicts are observable in
        # pipeline_steps. Best-effort; routing failure does NOT re-raise.
        _emit_self_evaluate_attempt(
            context=context,
            attempt_number=gate_outcome.attempt_number,
            outcome=gate_outcome.outcome,
            rejection_feedback=gate_outcome.rejection_feedback,
            feedback_messages=gate_outcome.feedback_messages,
        )

        if gate_outcome.action is GateAction.SHIP:
            stamped = draft_plan.model_copy(
                update={"self_evaluate_status": gate_outcome.self_evaluate_status}
            )
            output = stamped.model_dump(mode="json")
            status = "completed"
            break
        if gate_outcome.action is GateAction.ABORTED:
            # Hard-limit cancel during the gate; the post-loop branch
            # handles termination uniformly.
            break
        if gate_outcome.action is GateAction.SEAM_ERROR:
            _emit_invalid_output(
                context=context,
                reason=gate_outcome.error_message or "self_evaluate seam error",
                tokens_used=input_tokens_used + output_tokens_used,
                tool_calls_made=tool_calls_made + gate.evaluator_calls,
                wallclock_ms=int((time.monotonic() - start) * 1000),
            )
            status = "invalid"
            break
        if gate_outcome.action is GateAction.REJECTED:
            # Exhausted the one-retry budget; the draft is known-bad.
            # Do NOT ship. Route SELF_EVAL_REJECTED for escalation; the
            # router's default_strategy is ESCALATE_TO_FAZAL.
            _emit_self_eval_rejected(
                context=context,
                rejection_feedback=gate_outcome.rejection_feedback,
                attempt_number=gate_outcome.attempt_number,
                tokens_used=input_tokens_used + output_tokens_used,
                tool_calls_made=tool_calls_made + gate.evaluator_calls,
                wallclock_ms=int((time.monotonic() - start) * 1000),
            )
            stamped = draft_plan.model_copy(
                update={"self_evaluate_status": gate_outcome.self_evaluate_status}
            )
            output = stamped.model_dump(mode="json")
            status = "rejected"
            break

        # RETRY — append feedback as a user message and let the loop
        # ask the model for a new draft. The next turn re-enters the
        # terminal branch and runs the gate again.
        for fb_msg in gate_outcome.feedback_messages:
            messages.append(fb_msg)
            raw_messages.append(fb_msg)
        continue

    wallclock_ms = int((time.monotonic() - start) * 1000)
    # cost_paise accrues even on terminated runs (hard rule, VT-35 brief):
    # the API spend already happened; refunds are not a thing.
    cost_paise = compute_cost_paise(
        model=model,
        input_tokens=input_tokens_used,
        output_tokens=output_tokens_used,
    )
    tokens_used = input_tokens_used + output_tokens_used
    # VT-36: gate's self_evaluate calls count toward the model's tool-
    # dispatch budget. They also count toward the AgentResult's
    # observability tool_calls_made for parity with the enforcer count.
    if gate is not None:
        tool_calls_made += gate.evaluator_calls

    terminated_by: HardLimitAxis | None = None
    terminated_reason: str | None = None
    if ctx.is_cancelled:
        status = "terminated"
        terminated_by = ctx.cancelled_by
        terminated_reason = ctx.reason
        _emit_hard_limit_breach(
            context=context,
            axis=cast(HardLimitAxis, terminated_by),
            reason=cast(str, terminated_reason),
            tokens_used=tokens_used,
            tool_calls_made=tool_calls_made,
            wallclock_ms=wallclock_ms,
        )

    return AgentResult(
        status=cast(Any, status),
        terminated_by=terminated_by,
        output=output,
        tokens_used=tokens_used,
        tool_calls_made=tool_calls_made,
        wallclock_ms=wallclock_ms,
        cost_paise=cost_paise,
        raw_messages=raw_messages,
        terminated_reason=terminated_reason,
    )


def _emit_self_evaluate_attempt(
    *,
    context: SalesRecoveryContext,
    attempt_number: int,
    outcome: Any,
    rejection_feedback: Any,
    feedback_messages: list[dict[str, str]],
) -> None:
    """Write one pipeline_steps row per gate.run() — per-attempt
    self_evaluate telemetry (VT-SalesRecovery-Agent wiring).

    step_kind = 'self_evaluate_attempt'. output_envelope carries the
    attempt number + verdict + reasons (list-per-category preserved
    when present). RLS-scoped via tenant_connection. Best-effort —
    observability MUST NOT break the run."""
    from psycopg.types.json import Jsonb

    from orchestrator.db import tenant_connection

    envelope: dict[str, Any] = {
        "attempt_number": attempt_number,
        "outcome": outcome.value if outcome is not None else None,
    }
    # rejection_feedback is populated only on REJECTED outcomes; for
    # RETRY the feedback lives in feedback_messages (the structured
    # message bag the loop appends).
    if rejection_feedback is not None:
        envelope["reasons"] = {
            "schema": rejection_feedback.schema,
            "pillar": rejection_feedback.pillar,
            "consistency": rejection_feedback.consistency,
            "legal": rejection_feedback.legal,
        }
    elif feedback_messages:
        envelope["feedback_messages"] = feedback_messages

    try:
        with tenant_connection(UUID(context.tenant_id)) as conn, conn.transaction():
            # dict_row factory configured on the pool (graph.py); mypy
            # can't see it through psycopg's generic Row type, cast at
            # the seam (same pattern as error_router._log_decision).
            raw_next = conn.execute(
                "SELECT COALESCE(MAX(step_index), 0) + 1 AS next "
                "FROM pipeline_steps WHERE run_id = %s",
                (context.run_id,),
            ).fetchone()
            next_index_row = cast("dict[str, Any]", raw_next)
            next_index = int(next_index_row["next"])
            conn.execute(
                """
                INSERT INTO pipeline_steps
                    (run_id, tenant_id, step_index, step_kind, output_envelope)
                VALUES (%s, %s, %s, 'self_evaluate_attempt', %s)
                """,
                (
                    context.run_id,
                    context.tenant_id,
                    next_index,
                    Jsonb(envelope),
                ),
            )
    except Exception:
        # Observability never breaks recovery (CL-242 — same precedent
        # as orchestrator.error_router._log_decision).
        pass


def _emit_self_eval_rejected(
    *,
    context: SalesRecoveryContext,
    rejection_feedback: Any,
    attempt_number: int,
    tokens_used: int,
    tool_calls_made: int,
    wallclock_ms: int,
) -> None:
    """Route a FailureRecord(SELF_EVAL_REJECTED) — the gate exhausted
    its one-retry budget and the run is rejected. Router escalates
    to Fazal per the spec (severity HIGH, default_strategy
    ESCALATE_TO_FAZAL)."""
    reasons: dict[str, Any] = {}
    if rejection_feedback is not None:
        reasons = {
            "schema": rejection_feedback.schema,
            "pillar": rejection_feedback.pillar,
            "consistency": rejection_feedback.consistency,
            "legal": rejection_feedback.legal,
        }
    failure = FailureRecord(
        failure_type=FailureType.SELF_EVAL_REJECTED,
        message=(
            f"self_evaluate gate rejected after {attempt_number} attempts "
            "(initial draft + one retry)"
        ),
        occurred_at=datetime.now(UTC),
        tenant_id=UUID(context.tenant_id),
        run_id=UUID(context.run_id),
        metadata={
            "source": "self_evaluate_gate",
            "attempt_number": attempt_number,
            "reasons": reasons,
            "tokens_used": tokens_used,
            "tool_calls_made": tool_calls_made,
            "wallclock_ms": wallclock_ms,
        },
    )
    route_failure(failure)


def _emit_invalid_output(
    *,
    context: SalesRecoveryContext,
    reason: str,
    tokens_used: int,
    tool_calls_made: int,
    wallclock_ms: int,
) -> None:
    """Route a FailureRecord(AGENT_INVALID_OUTPUT) for a self-evaluate
    seam error (VT-36 / VT-3.6). Best-effort — routing failure must
    NOT re-raise into the run."""
    failure = FailureRecord(
        failure_type=FailureType.AGENT_INVALID_OUTPUT,
        message=reason,
        occurred_at=datetime.now(UTC),
        tenant_id=UUID(context.tenant_id),
        run_id=UUID(context.run_id),
        metadata={
            "source": "self_evaluate_gate",
            "tokens_used": tokens_used,
            "tool_calls_made": tool_calls_made,
            "wallclock_ms": wallclock_ms,
        },
    )
    route_failure(failure)


def _emit_hard_limit_breach(
    *,
    context: SalesRecoveryContext,
    axis: HardLimitAxis,
    reason: str,
    tokens_used: int,
    tool_calls_made: int,
    wallclock_ms: int,
) -> None:
    """Construct + route a FailureRecord for a hard-limit cancellation
    (VT-35 / VT-29 surface). Best-effort — a routing failure must NOT
    re-raise into the run (observability cannot break recovery; the
    error_router itself swallows + logs internally)."""
    failure = FailureRecord(
        failure_type=FailureType.AGENT_HARD_LIMIT_BREACH,
        message=reason,
        occurred_at=datetime.now(UTC),
        tenant_id=UUID(context.tenant_id),
        run_id=UUID(context.run_id),
        metadata={
            "axis": axis.value,
            "tokens_used": tokens_used,
            "tool_calls_made": tool_calls_made,
            "wallclock_ms": wallclock_ms,
        },
    )
    route_failure(failure)


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Best-effort serialisation of an Anthropic content block to a dict."""
    if hasattr(block, "model_dump"):
        return cast(dict[str, Any], block.model_dump())
    if isinstance(block, dict):
        return block
    return {
        "type": getattr(block, "type", None),
        "text": getattr(block, "text", None),
    }


__all__ = [
    "SalesRecoveryContext",
    "run_sales_recovery_agent",
]
