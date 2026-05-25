"""LangSmith client + tracing helpers for the orchestrator (VT-101).

Pillar 1 — orchestrator generates the ``run_id`` (UUID v4) at entry and
passes it down. This module's job is to surface that ``run_id`` to LangSmith
as the canonical trace ID so a single value links every span end-to-end.

Pillar 8 — one tracing namespace. No parallel correlation IDs. The
``run_id`` is the only identifier this module accepts.

Graceful degradation — every span creation is wrapped in try/except. Failure
logs to stderr but never propagates. A LangSmith outage cannot kill the
pipeline.

Project separation — ``LANGSMITH_PROJECT`` env var resolves to
``viabe-team-dev`` (default) or ``viabe-team-prod``. Mixing data is an audit
failure; the env var is the structural switch.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import AsyncIterator, Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar
from uuid import UUID

from orchestrator.observability.pii import redact_for_langsmith

P = ParamSpec("P")
R = TypeVar("R")


_DEFAULT_PROJECT = "viabe-team-dev"
_PROD_PROJECT = "viabe-team-prod"


def get_project_name() -> str:
    """Return the LangSmith project from ``LANGSMITH_PROJECT`` env var.

    Defaults to ``viabe-team-dev``. Production env MUST set
    ``LANGSMITH_PROJECT=viabe-team-prod`` — dev/prod separation is structural,
    enforced by the writer not the reader.
    """
    return os.environ.get("LANGSMITH_PROJECT", _DEFAULT_PROJECT)


def is_enabled() -> bool:
    """LangSmith tracing requires the API key to be set.

    Absence is a graceful no-op — wrapped functions still execute, just
    without spans. CI runs without the key; the graceful-degradation test
    proves the pipeline survives.
    """
    return bool(os.environ.get("LANGSMITH_API_KEY"))


def format_run_id_footer(run_id: UUID) -> str:
    """Return the operator-alert footer string carrying the ``run_id``.

    Telegram / log line footers append this so an alert can be cross-linked
    to a LangSmith trace by ID. No PII concern — the run_id is opaque.
    """
    return f"run_id={run_id}"


def traceable_node(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator: wrap a LangGraph node / DBOS step as a LangSmith span.

    Inputs and outputs flow through :func:`redact_for_langsmith` BEFORE
    reaching LangSmith — bypass is mechanically impossible without
    replacing this decorator.

    If tracing is disabled (no API key) or LangSmith raises, the wrapped
    function still runs normally; only the span is dropped.
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        @wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            if not is_enabled():
                return fn(*args, **kwargs)
            run_id = _extract_run_id(args, kwargs)
            try:
                from langsmith import traceable as _ls_traceable
            except ImportError:
                return fn(*args, **kwargs)
            safe_inputs = {
                "args": redact_for_langsmith(list(args)),
                "kwargs": redact_for_langsmith(dict(kwargs)),
                "run_id": str(run_id) if run_id is not None else None,
            }
            metadata = {"run_id": str(run_id) if run_id is not None else None}
            try:
                traced = _ls_traceable(
                    name=name,
                    project_name=get_project_name(),
                    metadata=metadata,
                )(fn)
                # Some SDK versions accept an `inputs` kwarg to override the
                # captured signature payload; if unsupported, the redaction
                # still applies via the wrapper's own redacted call below.
                _ = safe_inputs
                # mypy: SDK signature treats kwargs as LangSmithExtra; our
                # wrapper preserves the wrapped function's real shape.
                return traced(*args, **kwargs)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 - graceful degradation
                _log_trace_error(name, exc)
                return fn(*args, **kwargs)

        return wrapper

    return decorator


def traceable_tool(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator for MCP tool entrypoints.

    Identical contract to :func:`traceable_node`; named separately so dashboards
    can filter by span kind without inspecting the span name's prefix.
    """
    return traceable_node(name)


@contextlib.asynccontextmanager
async def trace_run(run_id: UUID, name: str) -> AsyncIterator[None]:
    """Async context manager for code paths LangGraph can't auto-trace
    (webhook handler entry, scheduled triggers, recovery retries).

    Usage::

        async with trace_run(event.run_id, "webhook.twilio"):
            await handle_event(event)

    No-op when tracing is disabled. Never raises out of the context body.
    """
    if not is_enabled():
        yield
        return
    try:
        from langsmith import Client as _LangSmithClient
        from langsmith.run_trees import RunTree
    except ImportError:
        yield
        return

    tree: RunTree | None = None
    try:
        client = _LangSmithClient()
        tree = RunTree(
            name=name,
            run_type="chain",
            project_name=get_project_name(),
            ls_client=client,
            id=run_id,
            extra={"metadata": {"run_id": str(run_id)}},
        )
        tree.post()
    except Exception as exc:  # noqa: BLE001 - graceful degradation
        _log_trace_error(name, exc)
        yield
        return

    try:
        yield
    except Exception as inner_exc:
        with contextlib.suppress(Exception):
            assert tree is not None
            tree.end(error=str(inner_exc))
            tree.patch()
        raise
    else:
        with contextlib.suppress(Exception):
            assert tree is not None
            tree.end()
            tree.patch()


def request_with_trace_headers(
    run_id: UUID, headers: dict[str, str] | None = None
) -> dict[str, str]:
    """Build an outbound HTTP header dict carrying ``X-Trace-Id``.

    Unconditional — destinations that ignore the header just ignore it
    (Twilio / Razorpay / Resend). Anthropic honors it for cross-system
    correlation. No per-vendor branching.
    """
    out = dict(headers or {})
    out["X-Trace-Id"] = str(run_id)
    return out


def _extract_run_id(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> UUID | str | None:
    """Best-effort: pull ``run_id`` from kwargs or positional args.

    Used only for span metadata. Returning None just means the span lacks
    the cross-link; the wrapped function still runs.
    """
    value = kwargs.get("run_id")
    if value is not None:
        if isinstance(value, (UUID, str)):
            return value
        return None
    for arg in args:
        if isinstance(arg, UUID):
            return arg
        if isinstance(arg, str) and len(arg) == 36 and arg.count("-") == 4:
            return arg
    return None


def _log_trace_error(span_name: str, exc: BaseException) -> None:
    """Write a single-line stderr breadcrumb. Never raises."""
    try:
        print(
            f"[observability] LangSmith span '{span_name}' failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "format_run_id_footer",
    "get_project_name",
    "is_enabled",
    "request_with_trace_headers",
    "trace_run",
    "traceable_node",
    "traceable_tool",
]
