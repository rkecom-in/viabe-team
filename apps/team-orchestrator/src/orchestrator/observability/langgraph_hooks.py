"""VT-183 LangGraph node hooks — entry/exit state_transition writers.

Wraps a node callable so each execution writes one
``state_transition`` pipeline_steps row via VT-180 ``write_step()``.
Captures the transition atomically: ``from_node`` (resolved from state
or explicit kwarg) + the returned ``langgraph.types.Command``
(``goto`` / ``update`` shape per CL-175) + status.

Per CL-417 envelope minimalism: ONE row per node execution carrying
the transition atomically (Q2 Option A — Cowork plan-review locked).
``state_transition`` envelope (VT-179) has no ``output_envelope`` —
state transitions are one-side events, not request/response pairs.

Per CL-175: the row's ``input_envelope`` mirrors the returned
``langgraph.types.Command`` shape — ``from_node``, ``to_node``,
``langgraph_command`` dict.

Per Q3 Option A: hook catches exceptions → writes status='failed' +
error envelope → re-raises. Matches VT-181 @tool_step + VT-180
write_step patterns. Re-raise preserves LangGraph's error contract.

ContextVar discipline (VT-181 pattern): caller MUST enter
``observability_context(...)`` before invoking the graph. ContextVar
absent → log warning + skip write_step (observability best-effort
per CL-122). VT-186 CI gate (separate row) will enforce that callers
set the context.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any, Callable, TypeVar, cast

from orchestrator.observability.decorators import (
    _observability_context,
)
from orchestrator.observability.pipeline_observability import write_step

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def with_state_transition_hook(
    node_callable: F,
    *,
    node_name: str,
    from_node: str | None = None,
) -> F:
    """Wrap a LangGraph node so each execution writes one state_transition row.

    Args:
        node_callable: the node function (e.g., ``collapse_node``).
        node_name: the StateGraph identifier (``graph.add_node`` first arg).
            Used as ``step_name`` on the pipeline_steps row.
        from_node: optional override. If unset, the helper inspects
            ``state.current_node`` / ``state["__prev_node__"]`` /
            ``state["current_node"]`` to infer the previous node.
            Falls back to ``"<unknown>"`` when nothing resolves.
    """

    @functools.wraps(node_callable)
    def wrapper(state: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = _observability_context.get()
        t0 = time.monotonic()
        from_node_resolved = (
            from_node or _resolve_from_node(state) or "<unknown>"
        )

        try:
            result = node_callable(state, *args, **kwargs)
        except Exception as exc:
            _write_state_transition(
                ctx=ctx,
                node_name=node_name,
                from_node=from_node_resolved,
                langgraph_command=None,
                status="failed",
                error={
                    "exception_type": type(exc).__name__,
                    "exception_repr": repr(exc),
                },
                t0=t0,
            )
            raise

        langgraph_command = _to_command_dict(result)
        _write_state_transition(
            ctx=ctx,
            node_name=node_name,
            from_node=from_node_resolved,
            langgraph_command=langgraph_command,
            status="completed",
            error=None,
            t0=t0,
        )
        return result

    return cast("F", wrapper)


def _resolve_from_node(state: Any) -> str | None:
    """Best-effort resolution of the previous node identifier.

    LangGraph doesn't surface a "current node" attribute on the state
    by default. Callers can attach one (``state["__prev_node__"]``) or
    leave it absent — the helper returns None and the canonical column
    receives ``"<unknown>"``.
    """
    if state is None:
        return None
    if isinstance(state, dict):
        return state.get("__prev_node__") or state.get("current_node")
    return getattr(state, "current_node", None)


def _to_command_dict(result: Any) -> dict[str, Any] | None:
    """Coerce a node's return value into a serializable command dict.

    Handles:
    - ``None`` → None (no transition recorded; node ran but didn't
      update state)
    - ``dict`` → returned as-is
    - ``langgraph.types.Command`` → ``{"goto": ..., "update": ...}``
    - anything else → ``{"raw": repr(result)[:200]}`` for forensic
      preservation
    """
    if result is None:
        return None
    if isinstance(result, dict):
        return result
    if hasattr(result, "goto") or hasattr(result, "update"):
        return {
            "goto": getattr(result, "goto", None),
            "update": _safe_update_dump(getattr(result, "update", None)),
        }
    return {"raw": repr(result)[:200]}


def _safe_update_dump(update: Any) -> Any:
    """``langgraph.types.Command.update`` may carry non-JSON values.

    Coerce to dict-of-string-reprs to guarantee JSONB-safe serialization
    without crashing the hook on unexpected nested types.
    """
    if update is None:
        return None
    if isinstance(update, dict):
        return {str(k): _safe_value(v) for k, v in update.items()}
    return repr(update)[:200]


def _safe_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe_value(v) for k, v in value.items()}
    return repr(value)[:200]


def _write_state_transition(
    *,
    ctx: Any,
    node_name: str,
    from_node: str,
    langgraph_command: dict[str, Any] | None,
    status: str,
    error: dict[str, Any] | None,
    t0: float,
) -> None:
    """Skip-with-warning when ContextVar is absent (observability best-effort)."""
    if ctx is None:
        logger.warning(
            "state_transition hook: ObservabilityContext unset; skip write",
            extra={"node_name": node_name},
        )
        return
    duration_ms = int((time.monotonic() - t0) * 1000)
    to_node = "<terminal>"
    if langgraph_command and isinstance(langgraph_command, dict):
        goto = langgraph_command.get("goto")
        if goto:
            to_node = str(goto)
    try:
        write_step(
            step_kind="state_transition",
            run_id=ctx.run_id,
            tenant_id=ctx.tenant_id,
            step_name=node_name,
            input_envelope={
                "from_node": from_node,
                "to_node": to_node,
                "langgraph_command": langgraph_command or {},
            },
            output_envelope=None,
            status=status,
            error=error,
            parent_step_id=ctx.parent_step_id,
            tool_calls=[
                {"node_name": node_name, "duration_ms": duration_ms}
            ],
        )
    except Exception as exc:
        # Observability MUST NOT break the LangGraph caller (CL-122).
        logger.warning(
            "state_transition write_step raised; swallowed",
            extra={"node_name": node_name, "exc": repr(exc)},
        )


__all__ = ["with_state_transition_hook"]
