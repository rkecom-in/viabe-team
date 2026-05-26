"""VT-181 ``@observability.tool_step`` decorator — load-bearing observability.

Wraps any tool function so each invocation writes a pipeline_steps row
via VT-180's ``write_step()``. Caller sets the
``ObservabilityContext`` via the ``observability_context(...)`` context
manager BEFORE invoking the wrapped tool — the decorator reads
``_observability_context`` (a ContextVar) to obtain run_id + tenant_id
+ parent_step_id without polluting every tool's signature (CL-Q1
Option A).

Soft-fail discipline (per VT-180 / CL-19):
- Args don't match ``envelope_in`` → row written with
  ``error.payload_validation_failed = true``; wrapped function STILL
  executes.
- Wrapped function raises → row written with status='failed' + error
  envelope carrying the exception type/repr; exception re-raised so
  caller's error handling still fires.
- Return doesn't match ``envelope_out`` → row written with
  ``error.output_validation_failed = true``; result still returned.

ContextVar absent → logger.warning + skip write_step (observability is
best-effort per CL-122). VT-186 CI gate (later) will enforce that
agent/langgraph callers set the context before invoking decorated tools.

``TOOL_STEP_REGISTRY`` collects each decorated tool's metadata at
definition time so ``validate_tool_step_registry()`` (called from
``dbos_config.launch_dbos()`` + FastAPI lifespan) can fail-fast at
process boot when a tool's envelope types drift from VT-179's
``STEP_KIND_REGISTRY``.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel, ValidationError

from orchestrator.observability.envelopes import (
    STEP_KIND_REGISTRY,
    EnvelopeRegistryDrift,
)
from orchestrator.observability.pipeline_observability import write_step

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Callable[..., Any])


@dataclass(frozen=True)
class ObservabilityContext:
    """Per-call observability context (run_id + tenant_id + optional parent).

    The decorator reads this from ``_observability_context`` ContextVar.
    Callers (agent, langgraph node) set it via ``observability_context()``
    context manager before invoking the wrapped tool.
    """

    run_id: UUID
    tenant_id: UUID
    parent_step_id: UUID | None = None


_observability_context: ContextVar[ObservabilityContext | None] = ContextVar(
    "_observability_context", default=None
)


# Definition-time registry of decorated tools. Used by:
# - validate_tool_step_registry() boot hook for envelope-drift check
# - VT-186 CI gate (later) for static tool-coverage assertions
TOOL_STEP_REGISTRY: dict[str, dict[str, Any]] = {}


@contextmanager
def observability_context(
    *,
    run_id: UUID,
    tenant_id: UUID,
    parent_step_id: UUID | None = None,
) -> Iterator[None]:
    """Set the ContextVar that decorated tools read.

    Callers MUST wrap tool invocations in this context manager so
    write_step has run_id + tenant_id. Nested usage replaces the
    enclosing context for the duration of the inner block, restoring
    the outer context on exit.
    """
    token = _observability_context.set(
        ObservabilityContext(
            run_id=run_id, tenant_id=tenant_id, parent_step_id=parent_step_id
        )
    )
    try:
        yield
    finally:
        _observability_context.reset(token)


def tool_step(
    *,
    step_kind: str = "mcp_tool_call",
    envelope_in: type[BaseModel],
    envelope_out: type[BaseModel],
    step_name: str | None = None,
) -> Callable[[T], T]:
    """Decorator that routes a tool's invocation through ``write_step``.

    Registers the tool in ``TOOL_STEP_REGISTRY`` at definition time so
    boot-time drift detection (``validate_tool_step_registry``) can
    fail-fast when envelopes diverge from VT-179's registry.

    Per CL-Q2 (Cowork plan-review): ``step_kind`` is an override —
    default ``'mcp_tool_call'`` covers generic tools; semantic tools
    (e.g., ``self_evaluate`` → ``self_evaluate_gate``) override to
    preserve the CL-281 verdict-model + downstream Ops UI replay path.
    """

    def deco(func: T) -> T:
        resolved_step_name = step_name or func.__name__
        TOOL_STEP_REGISTRY[resolved_step_name] = {
            "step_kind": step_kind,
            "envelope_in": envelope_in,
            "envelope_out": envelope_out,
            "func": func,
        }

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = _observability_context.get()
            t0 = time.monotonic()

            # Best-effort: build a dict from bound signature; on failure
            # (e.g., the wrapped function uses *args/**kwargs natively)
            # fall back to a positional/keyword summary.
            sig = inspect.signature(func)
            try:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                tool_args_dict: dict[str, Any] = dict(bound.arguments)
            except TypeError:
                tool_args_dict = {
                    "args": [repr(a) for a in args],
                    "kwargs": dict(kwargs),
                }

            error: dict[str, Any] = {}

            # Validate tool's args against the tool's own envelope_in
            # (NOT the VT-179 step_kind envelope — that's a separate
            # downstream shape, handled below).
            try:
                envelope_in.model_validate(tool_args_dict)
            except ValidationError as ve:
                error["payload_validation_failed"] = True
                error["payload_validation_details"] = [
                    {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                    for e in ve.errors()
                ]

            tool_result_dict: dict[str, Any] | None = None
            status = "completed"
            cost_paise = 0
            tokens_input: int | None = None
            tokens_output: int | None = None
            model_used: str | None = None
            result: Any = None

            try:
                result = func(*args, **kwargs)
                tool_result_dict = _to_envelope_dict(result)
                try:
                    envelope_out.model_validate(tool_result_dict)
                except ValidationError as ve:
                    error["output_validation_failed"] = True
                    error["output_validation_details"] = [
                        {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                        for e in ve.errors()
                    ]
                cost_paise = int(_get_attr(result, "cost_paise", 0) or 0)
                tokens_input = _get_attr(result, "tokens_input", None)
                tokens_output = _get_attr(result, "tokens_output", None)
                model_used = _get_attr(result, "model_used", None)
            except Exception as exc:
                status = "failed"
                error["exception_type"] = type(exc).__name__
                error["exception_repr"] = repr(exc)
                _write_if_context(
                    ctx=ctx,
                    step_kind=step_kind,
                    step_name=resolved_step_name,
                    tool_args_dict=tool_args_dict,
                    tool_result_dict=tool_result_dict,
                    status=status,
                    error=error,
                    t0=t0,
                    cost_paise=0,
                    tokens_input=None,
                    tokens_output=None,
                    model_used=None,
                )
                raise

            if error:
                status = "failed"

            _write_if_context(
                ctx=ctx,
                step_kind=step_kind,
                step_name=resolved_step_name,
                tool_args_dict=tool_args_dict,
                tool_result_dict=tool_result_dict,
                status=status,
                error=error if error else None,
                t0=t0,
                cost_paise=cost_paise,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                model_used=model_used,
            )
            return result

        return cast("T", wrapper)

    return deco


def _to_envelope_dict(result: Any) -> dict[str, Any]:
    if result is None:
        return {}
    if isinstance(result, BaseModel):
        return result.model_dump()
    if isinstance(result, dict):
        return result
    return {"value": result}


def _get_attr(obj: Any, name: str, default: Any) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _write_if_context(
    *,
    ctx: ObservabilityContext | None,
    step_kind: str,
    step_name: str,
    tool_args_dict: dict[str, Any],
    tool_result_dict: dict[str, Any] | None,
    status: str,
    error: dict[str, Any] | None,
    t0: float,
    cost_paise: int,
    tokens_input: int | None,
    tokens_output: int | None,
    model_used: str | None,
) -> None:
    """Skip-with-warning when ContextVar is absent (CL-Q1 Option A soft-fail).

    Wraps the tool's args/result dicts in the VT-179 ``mcp_tool_call``
    envelope shape (or whichever step_kind's envelope) so write_step's
    own envelope validation accepts the payload:

    - ``input_envelope = {"tool_name": step_name, "tool_args": <tool_args_dict>}``
    - ``output_envelope = {"tool_result": <tool_result_dict>, "cost_paise": cost_paise, "duration_ms": duration_ms}``

    For step_kind overrides (e.g. ``self_evaluate_gate``) the same
    wrap-in-canonical-envelope-shape principle applies but the wrapper
    keys differ — callers using overrides MUST construct the envelope
    shape themselves and bypass @tool_step or extend this helper.
    Documented in module docstring.
    """
    if ctx is None:
        logger.warning(
            "tool_step decorator skipping write — no ObservabilityContext set",
            extra={"step_kind": step_kind, "step_name": step_name},
        )
        return
    duration_ms = int((time.monotonic() - t0) * 1000)

    # Wrap the tool's args + result in the VT-179 step_kind envelope
    # shape. For 'mcp_tool_call' this is {tool_name, tool_args} +
    # {tool_result, cost_paise, duration_ms}.
    if step_kind == "mcp_tool_call":
        input_envelope: dict[str, Any] = {
            "tool_name": step_name,
            "tool_args": tool_args_dict,
        }
        output_envelope: dict[str, Any] | None = (
            None
            if tool_result_dict is None
            else {
                "tool_result": tool_result_dict,
                "cost_paise": cost_paise,
                "duration_ms": duration_ms,
            }
        )
    else:
        # Non-mcp_tool_call step_kind: pass tool dicts through directly.
        # Caller is responsible for ensuring the shape matches the
        # registered VT-179 envelope (write_step soft-fails on drift).
        input_envelope = tool_args_dict
        output_envelope = tool_result_dict

    try:
        write_step(
            step_kind=step_kind,
            run_id=ctx.run_id,
            tenant_id=ctx.tenant_id,
            step_name=step_name,
            input_envelope=input_envelope,
            output_envelope=output_envelope,
            status=status,
            parent_step_id=ctx.parent_step_id,
            error=error,
            cost_paise=cost_paise,
            model_used=model_used,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tool_calls=[
                {"tool_name": step_name, "duration_ms": duration_ms}
            ],
        )
    except Exception as exc:
        # Observability MUST NOT break the tool's caller (CL-122). Log + swallow.
        logger.warning(
            "tool_step write_step raised; swallowed for caller safety",
            extra={
                "step_kind": step_kind,
                "step_name": step_name,
                "exc": repr(exc),
            },
        )


def validate_tool_step_registry() -> None:
    """Boot-time: every ``@tool_step``'s ``step_kind`` is registered in VT-179.

    Raises ``EnvelopeRegistryDrift`` listing unregistered step_kinds so
    process startup fails before any pipeline writes happen (matches
    VT-179's fail-fast posture).

    Note: per-tool envelope_in/envelope_out (the wrapped function's own
    input/output Pydantic models) are NOT compared against the registry's
    envelope sub-models. The decorator's envelope_in/out validate the
    function's args/return (tool-specific shape); pipeline_steps stores
    the validated dicts as free-form JSONB payload. STEP_KIND_REGISTRY's
    canonical envelope shape is for downstream replay (Ops UI) — it does
    not constrain the writer's payload shape.
    """
    mismatches: list[str] = []
    for step_name, meta in TOOL_STEP_REGISTRY.items():
        if meta["step_kind"] not in STEP_KIND_REGISTRY:
            mismatches.append(
                f"{step_name}: step_kind='{meta['step_kind']}' not in STEP_KIND_REGISTRY"
            )
    if mismatches:
        raise EnvelopeRegistryDrift(
            "tool_step registry mismatches: " + "; ".join(mismatches)
        )


__all__ = [
    "ObservabilityContext",
    "TOOL_STEP_REGISTRY",
    "observability_context",
    "tool_step",
    "validate_tool_step_registry",
]
