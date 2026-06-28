"""VT-182 Anthropic Messages SDK observability callback.

Decorator that wraps each ``client.messages.create(...)`` round-trip
(the canonical seam at ``agent/sales_recovery.py:_messages_create``) so
every call writes one ``agent_reasoning_step`` pipeline_steps row via
VT-180's ``write_step()``.

Captured (per design-doc §2.3 + CL-249 + CL-56):
- input_envelope: context_bundle_hash, components, token_count,
  prior_tool_calls_count, prior_tool_calls_summary (caller-supplied
  via ``reasoning_step_input(...)`` context manager)
- output_envelope: think_text_redacted, action, action_args,
  logfire_trace_id (active Logfire/OTel span; None on graceful absent)
- decision_rationale: first 400 chars of think_text_redacted
- tokens_input/output: response.usage
- model_used: response.model
- cost_paise: compute_cost_paise(model, in_tokens, out_tokens)

PII: think_text flows through VT-104 ``redact_for_log`` before
persistence (CL-104 + CL-390). Raw text TO Anthropic is permitted per
Fazal directive (consent-gated); redaction is for the LOCAL pipeline_steps
row only.

Context: per Cowork plan-review Q2 Option A, callback reads VT-181's
``_observability_context`` ContextVar ONLY. Caller MUST enter
``observability_context(...)`` before the agent loop. ContextVar
absent → log warning + skip write_step. Pending input absent → skip.
"""

from __future__ import annotations

import functools
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from orchestrator.agent.cost import compute_cost_paise
from orchestrator.observability.decorators import (
    _observability_context,
)
from orchestrator.observability.pii import redact_for_log
from orchestrator.observability.pipeline_observability import write_step

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentReasoningInput:
    """Caller-supplied per-call input envelope for the next Messages.create.

    Set via ``reasoning_step_input(...)`` context manager BEFORE invoking
    the wrapped ``_messages_create``. Stored in a separate ContextVar
    from VT-181's run/tenant context so the agent loop driver can compose
    them cleanly.
    """

    context_bundle_hash: str
    context_bundle_components: list[str]
    context_bundle_token_count: int
    prior_tool_calls_count: int
    prior_tool_calls_summary: list[dict[str, Any]]


_pending_input: ContextVar[AgentReasoningInput | None] = ContextVar(
    "_vt182_pending_input", default=None
)


@contextmanager
def reasoning_step_input(
    *,
    context_bundle_hash: str,
    context_bundle_components: list[str],
    context_bundle_token_count: int,
    prior_tool_calls_count: int,
    prior_tool_calls_summary: list[dict[str, Any]] | None = None,
) -> Iterator[None]:
    """Stage the input envelope for the next wrapped Messages.create.

    The decorator reads ``_pending_input`` ContextVar inside the wrapped
    function call. Restored to the prior value on exit.
    """
    payload = AgentReasoningInput(
        context_bundle_hash=context_bundle_hash,
        context_bundle_components=context_bundle_components,
        context_bundle_token_count=context_bundle_token_count,
        prior_tool_calls_count=prior_tool_calls_count,
        prior_tool_calls_summary=prior_tool_calls_summary or [],
    )
    token = _pending_input.set(payload)
    try:
        yield
    finally:
        _pending_input.reset(token)


def with_reasoning_capture(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator for ``_messages_create``.

    Captures the response's usage + content + active Logfire trace_id and
    writes the ``agent_reasoning_step`` row. Observability is best-effort
    (per CL-122): write_step failures are swallowed so the agent caller
    keeps running.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        ctx = _observability_context.get()
        pending = _pending_input.get()
        response = func(*args, **kwargs)
        if ctx is None or pending is None:
            if ctx is None:
                logger.warning(
                    "VT-182 reasoning capture: ObservabilityContext unset; skip write",
                )
            if pending is None:
                logger.warning(
                    "VT-182 reasoning capture: pending input envelope unset; skip write",
                )
            return response
        try:
            _record_step(ctx=ctx, pending=pending, response=response)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "VT-182 reasoning capture: write_step raised; swallowed",
                extra={"exc": repr(exc)},
            )
        return response

    return wrapper


def _record_step(
    *,
    ctx: Any,
    pending: AgentReasoningInput,
    response: Any,
) -> None:
    think_text = _first_text_block(response)
    think_text_redacted: str | None = None
    if think_text:
        redacted = redact_for_log({"text": think_text})
        think_text_redacted = redacted.get("text") if isinstance(redacted, dict) else None

    action, action_target, action_args_summary = _first_tool_use_block(response)
    trace_id = _current_logfire_trace_id()

    usage = getattr(response, "usage", None)
    in_tokens = int(getattr(usage, "input_tokens", 0)) if usage else 0
    out_tokens = int(getattr(usage, "output_tokens", 0)) if usage else 0
    model_used = getattr(response, "model", None)

    cost_paise = 0
    rate_lookup_model = _normalize_model_for_rates(model_used) if model_used else None
    if rate_lookup_model:
        try:
            cost_paise = compute_cost_paise(
                model=rate_lookup_model,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "VT-182 compute_cost_paise raised; cost_paise=0",
                extra={"model": model_used, "rate_lookup_model": rate_lookup_model, "exc": repr(exc)},
            )

    decision_rationale = (
        think_text_redacted[:400] if think_text_redacted else None
    )

    write_step(
        step_kind="agent_reasoning_step",
        run_id=ctx.run_id,
        tenant_id=ctx.tenant_id,
        step_name="agent_turn",
        input_envelope={
            # VT-464 D4: prompt_token_count is a REQUIRED field on
            # AgentReasoningStepInput — it was previously omitted, so every
            # brain reasoning-step envelope soft-failed validation. The prompt
            # (input) token count for this turn is the response usage's
            # input_tokens.
            "prompt_token_count": in_tokens,
            "context_bundle_hash": pending.context_bundle_hash,
            "context_bundle_components": pending.context_bundle_components,
            "context_bundle_token_count": pending.context_bundle_token_count,
            "prior_tool_calls_count": pending.prior_tool_calls_count,
            "prior_tool_calls_summary": pending.prior_tool_calls_summary,
        },
        output_envelope={
            "think_text": think_text_redacted,
            "action": action,
            "action_args": {
                "target": action_target,
                "summary": action_args_summary,
            },
            "logfire_trace_id": trace_id,
        },
        decision_rationale=decision_rationale,
        parent_step_id=ctx.parent_step_id,
        status="completed",
        cost_paise=cost_paise,
        model_used=model_used,
        tokens_input=in_tokens,
        tokens_output=out_tokens,
    )


def _first_text_block(response: Any) -> str | None:
    content = getattr(response, "content", None)
    if not content:
        return None
    for block in content:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", None)
    return None


def _first_tool_use_block(
    response: Any,
) -> tuple[str | None, str | None, str | None]:
    content = getattr(response, "content", None)
    if content:
        for block in content:
            if getattr(block, "type", None) == "tool_use":
                tool_input = getattr(block, "input", None)
                args_summary = str(tool_input)[:200] if tool_input is not None else None
                return ("tool_use", getattr(block, "name", None), args_summary)
    return (getattr(response, "stop_reason", None), None, None)


_MODEL_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def _normalize_model_for_rates(model: str) -> str:
    """Strip Anthropic's trailing date suffix (e.g. `-20251001`) so the
    model id matches the alias keys in ``orchestrator.agent.cost.RATES``.

    Anthropic's API returns the fully-qualified id (``claude-haiku-4-5-20251001``)
    in ``response.model``; the RATES table uses the base alias
    (``claude-haiku-4-5``). Without this normalization every real-API call
    silently zeroes ``cost_paise``.
    """
    return _MODEL_DATE_SUFFIX_RE.sub("", model)


def _current_logfire_trace_id() -> str | None:
    """Best-effort fetch of the active OTel/Logfire span's trace_id.

    Returns None when:
    - opentelemetry is not installed
    - no active span / invalid span context
    - any unexpected failure (graceful degradation per CL-56)
    """
    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span is None:
            return None
        ctx = span.get_span_context()
        if not ctx.is_valid:
            return None
        return format(ctx.trace_id, "032x")
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "AgentReasoningInput",
    "reasoning_step_input",
    "with_reasoning_capture",
]
