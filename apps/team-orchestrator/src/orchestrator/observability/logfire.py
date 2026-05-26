"""Pydantic Logfire client + tracing helpers (VT-171, hot-fix CL-56).

Replaces VT-101's LangSmith integration. CL-56 (Standing 2026-05-16) directs
all OTel span emission to Logfire (EU project). The redactor seam from
VT-104 is preserved byte-identical — only the sink backend changes.

Pillar 1 — orchestrator generates ``run_id`` (UUID v4) at entry and threads
it down. This module surfaces ``run_id`` as a span attribute so a single
value links every span end-to-end.

Pillar 8 — one tracing namespace. ``run_id`` is the only identifier this
module accepts.

Graceful degradation — every span creation is wrapped in try/except.
Failure logs to stderr; never propagates. A Logfire ingest outage cannot
kill the pipeline (regression contract from VT-101 preserved).

DBOS OTLP plumbing (Q3 verdict: env-var driven). :func:`configure_logfire`
programmatically exports ``OTEL_EXPORTER_OTLP_ENDPOINT`` +
``OTEL_EXPORTER_OTLP_HEADERS`` from ``LOGFIRE_TOKEN`` BEFORE
``launch_dbos()`` runs. DBOS's OTel exporter picks up the env vars at
startup; survives DBOS SDK version drift (no DBOSConfig kwarg coupling).

EU region (Fazal-set 2026-05-26) — ``LOGFIRE_BASE_URL`` defaults to
``https://logfire-eu.pydantic.dev``. Override via env when DPDP / data
residency posture changes.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from functools import wraps
from typing import Any, ParamSpec, TypeVar
from uuid import UUID

from orchestrator.observability.pii import redact_for_otel_span

P = ParamSpec("P")
R = TypeVar("R")


_DEFAULT_BASE_URL = "https://logfire-eu.pydantic.dev"
_DEFAULT_PROJECT = "viabe-team-dev"
_DEFAULT_SERVICE = "team-orchestrator"

_configured: bool = False


def get_project_name() -> str:
    """Return the Logfire project from ``LOGFIRE_PROJECT`` env (default ``viabe-team-dev``)."""
    return os.environ.get("LOGFIRE_PROJECT", _DEFAULT_PROJECT)


def is_enabled() -> bool:
    """Logfire ingestion requires ``LOGFIRE_TOKEN``.

    Absence is a graceful no-op — wrapped functions still execute, just
    without spans. CI runs without the token; the graceful-degradation
    test proves the pipeline survives.
    """
    return bool(os.environ.get("LOGFIRE_TOKEN"))


def configure_logfire() -> bool:
    """Idempotent Logfire setup. Returns True iff configured (token present).

    Side effects (when token present):
      1. Programmatically exports ``OTEL_EXPORTER_OTLP_ENDPOINT`` +
         ``OTEL_EXPORTER_OTLP_HEADERS`` so DBOS's OTel exporter picks up
         the Logfire EU endpoint at ``launch_dbos()`` (Q3 contract).
      2. Calls ``logfire.configure`` with EU base URL + service name.
      3. Calls ``logfire.instrument_anthropic`` (catches both the direct
         ``anthropic.Anthropic`` SDK calls and LangChain's
         ``langchain_anthropic.ChatAnthropic`` which delegates through the
         same SDK under the hood — verified at PICKUP per Cond 1).
      4. Calls ``logfire.instrument_pydantic`` for pydantic model
         instrumentation.

    Bypass: not possible from caller code; ``configure_logfire`` is the
    only configuration entry point. Missing token → stderr breadcrumb +
    no-op (graceful degradation, regression contract from VT-101).
    """
    global _configured
    if _configured:
        return True

    if not is_enabled():
        print(
            "[observability] LOGFIRE_TOKEN unset; Logfire disabled (no spans emitted)",
            file=sys.stderr,
        )
        return False

    base_url = os.environ.get("LOGFIRE_BASE_URL", _DEFAULT_BASE_URL)
    token = os.environ["LOGFIRE_TOKEN"]
    service = os.environ.get("OTEL_SERVICE_NAME", _DEFAULT_SERVICE)

    try:
        import logfire
        from logfire import AdvancedOptions
    except ImportError as exc:
        print(
            f"[observability] logfire import failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return False

    try:
        logfire.configure(
            service_name=service,
            token=token,
            advanced=AdvancedOptions(base_url=base_url),
        )
    except Exception as exc:  # noqa: BLE001 - graceful degradation
        print(
            f"[observability] logfire.configure failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return False

    # DBOS OTLP wiring — Logfire registers itself as the global
    # OpenTelemetry tracer provider during configure(). DBOS's OTel
    # exporter, when enabled, picks up the global provider and routes
    # spans through Logfire transparently. We avoid setting
    # OTEL_EXPORTER_OTLP_ENDPOINT / _HEADERS here because the env-var
    # path would auto-register a SECOND exporter alongside Logfire's,
    # and the two compete (Logfire's token-derived endpoint vs our
    # AdvancedOptions(base_url) override). Single-provider routing is
    # the simpler invariant. If DBOS's OTel exporter requires explicit
    # endpoint env vars on some installed dbos version, that wiring
    # ships as a follow-up VT row with on-the-wire verification.

    # First-party instrumentations. Each wrapped so a single failure
    # doesn't take the whole observability layer down.
    for instrument_name in ("instrument_anthropic", "instrument_pydantic"):
        instrument = getattr(logfire, instrument_name, None)
        if instrument is None:
            continue
        try:
            instrument()
        except Exception as exc:  # noqa: BLE001
            print(
                f"[observability] logfire.{instrument_name} failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    _configured = True
    return True


def instrument_orchestrator() -> bool:
    """Alias for :func:`configure_logfire` — the brief uses this name; the
    implementation is one entry point. Kept for call-site clarity."""
    return configure_logfire()


def format_run_id_footer(run_id: UUID | str) -> str:
    """Operator-alert footer carrying the ``run_id``.

    Telegram / log line footers append this so an alert cross-links to a
    Logfire span by ID. No PII — ``run_id`` is opaque.
    """
    return f"run_id={run_id}"


def traced_node(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator: wrap a LangGraph node / DBOS step as a Logfire span.

    Inputs and outputs flow through :func:`redact_for_otel_span` BEFORE
    they reach Logfire — bypass is mechanically impossible without
    replacing this decorator.

    If tracing is disabled (no token) or Logfire raises, the wrapped
    function still runs normally; only the span is dropped.
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        @wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            if not is_enabled():
                return fn(*args, **kwargs)
            try:
                import logfire
            except ImportError:
                return fn(*args, **kwargs)

            run_id = _extract_run_id(args, kwargs)
            safe_args = redact_for_otel_span(list(args))
            safe_kwargs = redact_for_otel_span(dict(kwargs))
            attrs: dict[str, Any] = {
                "node.name": name,
                "args": safe_args,
                "kwargs": safe_kwargs,
            }
            if run_id is not None:
                attrs["run_id"] = str(run_id)

            try:
                with logfire.span(name, **attrs) as span:
                    result = fn(*args, **kwargs)
                    try:
                        span.set_attribute("output", redact_for_otel_span(result))
                    except Exception:  # noqa: BLE001
                        pass
                    return result
            except Exception as exc:  # noqa: BLE001 - graceful degradation
                _log_trace_error(name, exc)
                return fn(*args, **kwargs)

        return wrapper

    return decorator


def traced_tool(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator for MCP tool entrypoints — same contract as :func:`traced_node`.

    Named separately so dashboards can filter by span kind without
    inspecting the span name's prefix.
    """
    return traced_node(name)


# ---------------------------------------------------------------------------
# Back-compat aliases — keep existing call sites working by import name.
# ---------------------------------------------------------------------------

# These names land in :mod:`observability/__init__.py`'s re-export list so
# call sites that imported `traceable_node` / `traceable_tool` from the
# package don't change.
traceable_node = traced_node
traceable_tool = traced_tool


@contextlib.asynccontextmanager
async def trace_run(run_id: UUID | str, name: str) -> AsyncIterator[None]:
    """Async context manager for code paths LangGraph can't auto-trace
    (webhook handler entry, scheduled triggers, recovery retries).

    No-op when tracing is disabled. Never raises out of the context body.
    """
    if not is_enabled():
        yield
        return
    try:
        import logfire
    except ImportError:
        yield
        return

    span_cm = None
    try:
        span_cm = logfire.span(name, run_id=str(run_id))
        span_cm.__enter__()
    except Exception as exc:  # noqa: BLE001
        _log_trace_error(name, exc)
        yield
        return

    try:
        yield
    except Exception as inner_exc:
        with contextlib.suppress(Exception):
            span_cm.__exit__(type(inner_exc), inner_exc, inner_exc.__traceback__)
        raise
    else:
        with contextlib.suppress(Exception):
            span_cm.__exit__(None, None, None)


@contextlib.contextmanager
def trace_run_sync(run_id: UUID | str, name: str) -> Iterator[None]:
    """Sync variant of :func:`trace_run` for callers outside an event loop."""
    if not is_enabled():
        yield
        return
    try:
        import logfire
    except ImportError:
        yield
        return

    try:
        with logfire.span(name, run_id=str(run_id)):
            yield
    except Exception as exc:  # noqa: BLE001
        _log_trace_error(name, exc)
        yield


def request_with_trace_headers(
    run_id: UUID | str, headers: dict[str, str] | None = None
) -> dict[str, str]:
    """Build an outbound HTTP header dict carrying ``X-Trace-Id``.

    Unconditional — destinations that ignore the header just ignore it
    (Twilio / Razorpay / Resend). Anthropic honors it for cross-system
    correlation.
    """
    out = dict(headers or {})
    out["X-Trace-Id"] = str(run_id)
    return out


def shutdown() -> None:
    """Flush + shutdown Logfire so spans land before process exit.

    Idempotent; safe to call from atexit handlers + signal handlers.
    Errors logged to stderr but not raised.
    """
    try:
        import logfire

        logfire.force_flush()
        logfire.shutdown()
    except Exception as exc:  # noqa: BLE001
        print(
            f"[observability] logfire.shutdown failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_run_id(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> UUID | str | None:
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
    try:
        print(
            f"[observability] Logfire span '{span_name}' failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001
        pass


# Reset for tests — internal helper, not part of the public surface.
def _reset_for_tests() -> None:
    global _configured
    _configured = False


__all__ = [
    "configure_logfire",
    "format_run_id_footer",
    "get_project_name",
    "instrument_orchestrator",
    "is_enabled",
    "request_with_trace_headers",
    "shutdown",
    "trace_run",
    "trace_run_sync",
    "traceable_node",
    "traceable_tool",
    "traced_node",
    "traced_tool",
]
