"""OpenTelemetry tracing → Honeycomb EU (VT-690; supersedes the Logfire-SaaS backend of VT-171/CL-56).

We KEEP the Pydantic ``logfire`` LIBRARY (it is free/OSS and has the best auto-instrumentation —
``instrument_anthropic`` captures every Claude call's full prompt→response, the agent's thinking
I/O) but STOP sending to the paid Logfire SaaS. ``configure`` runs with ``send_to_logfire=False``
and an ``additional_span_processors`` OTLP exporter pointed at Honeycomb's EU ingest — so every
span (DBOS workflow/step, app spans, LLM I/O, Pydantic validation) lands in Honeycomb, at zero
vendor cost. The module + function names stay ``logfire`` / ``configure_logfire`` to avoid a
tree-wide import churn; the BACKEND is Honeycomb.

Enable-switch: presence of ``HONEYCOMB_API_KEY`` (the Ingest Key — Key ID + Key Secret concatenated,
no separator, per Honeycomb's OTLP contract). Absent → graceful no-op (spans dropped, pipeline
runs). Different key VALUE per env (dev key → dev Honeycomb Environment, prod key → prod), same
NAME — that keeps dev/prod traces cleanly separated (the mistake the Logfire config had).

EU region (DPDP residency, carried over from the Logfire EU choice): the default OTLP endpoint is
``https://api.eu1.honeycomb.io``. Override via ``OTEL_EXPORTER_OTLP_ENDPOINT`` if the residency
posture changes. Dataset is DERIVED from ``service.name`` (Environments & Services mode) — no
dataset env needed.

Pillar 1 — orchestrator generates ``run_id`` (UUID v4) at entry and threads it down; surfaced as a
span attribute so one value links every span end-to-end. Pillar 8 — one tracing namespace.

Graceful degradation — every span creation is wrapped in try/except; failure logs to stderr, never
propagates. A Honeycomb ingest outage cannot kill the pipeline (regression contract from VT-101).

DBOS spans: ``logfire.configure`` registers the global OTel TracerProvider (with our Honeycomb
processor attached); DBOS's OTel exporter picks up that global provider, so DBOS workflow/step spans
route to Honeycomb transparently — no ``OTEL_EXPORTER_OTLP_*`` env coupling.

Content policy (Fazal 2026-07-21): full LLM I/O + business content ships (the whole point is to see
what the agent read + decided). ``logfire``'s DEFAULT scrubbing still redacts secret-shaped fields
(our own API keys/tokens never land in a span); the app's ``redact_for_otel_span`` still runs on the
explicit app-span attributes below. Customer business content stays visible under EU residency.
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


# VT-690 — Honeycomb EU OTLP HTTP ingest (dataset derived from service.name; no dataset env).
_HONEYCOMB_EU_ENDPOINT = "https://api.eu1.honeycomb.io"
_DEFAULT_SERVICE = "team-orchestrator"

_configured: bool = False


def get_project_name() -> str:
    """Return the Honeycomb service/dataset name (``OTEL_SERVICE_NAME``, default the service).

    Modern Honeycomb (Environments & Services) derives the dataset from ``service.name``; this is
    that name. Retained under the historical function name for the call sites that display it.
    """
    return os.environ.get("OTEL_SERVICE_NAME", _DEFAULT_SERVICE)


def is_enabled() -> bool:
    """Tracing requires ``HONEYCOMB_API_KEY`` (VT-690; was ``LOGFIRE_TOKEN``).

    Absence is a graceful no-op — wrapped functions still execute, just without spans. CI + any env
    lacking the key runs span-free; the graceful-degradation test proves the pipeline survives.
    """
    return bool(os.environ.get("HONEYCOMB_API_KEY"))


def configure_logfire() -> bool:
    """Idempotent tracing setup → Honeycomb EU (VT-690). Returns True iff configured (key present).

    Side effects (when ``HONEYCOMB_API_KEY`` present):
      1. Builds an OTLP-HTTP span exporter to Honeycomb EU (``…/v1/traces``, ``x-honeycomb-team``
         header = the concatenated Ingest Key).
      2. Calls ``logfire.configure`` with ``send_to_logfire=False`` (the paid SaaS is OFF — free)
         and that exporter as an ``additional_span_processor``. This registers the global OTel
         TracerProvider, so DBOS's OTel exporter routes workflow/step spans to Honeycomb too.
      3. Calls ``logfire.instrument_anthropic`` (direct ``anthropic.Anthropic`` AND LangChain's
         ``ChatAnthropic``, which delegates through the same SDK) — the LLM prompt→response capture.
      4. Calls ``logfire.instrument_pydantic`` for model-validation spans.

    Bypass: not possible from caller code; this is the only configuration entry point. Missing key
    → stderr breadcrumb + no-op (graceful degradation, regression contract from VT-101).
    """
    global _configured
    if _configured:
        return True

    if not is_enabled():
        print(
            "[observability] HONEYCOMB_API_KEY unset; tracing disabled (no spans emitted)",
            file=sys.stderr,
        )
        return False

    api_key = os.environ["HONEYCOMB_API_KEY"]
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", _HONEYCOMB_EU_ENDPOINT).rstrip("/")
    service = os.environ.get("OTEL_SERVICE_NAME", _DEFAULT_SERVICE)

    try:
        import logfire
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        print(
            f"[observability] tracing import failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return False

    try:
        exporter = OTLPSpanExporter(
            endpoint=f"{endpoint}/v1/traces",
            headers={"x-honeycomb-team": api_key},
        )
        logfire.configure(
            service_name=service,
            # The paid Logfire SaaS is OFF — we keep only the logfire LIBRARY (free, best-in-class
            # auto-instrumentation) and export spans to Honeycomb via the processor below.
            send_to_logfire=False,
            additional_span_processors=[BatchSpanProcessor(exporter)],
            # logfire's DEFAULT scrubbing stays ON — it redacts secret-shaped fields so our OWN
            # API keys/tokens never land in a span; business content (the agent's I/O) stays visible.
        )
    except Exception as exc:  # noqa: BLE001 - graceful degradation
        print(
            f"[observability] tracing configure failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return False

    # DBOS spans: logfire.configure registered the global OTel TracerProvider (with our Honeycomb
    # BatchSpanProcessor attached). DBOS's OTel exporter picks up that global provider and routes
    # workflow/step spans to Honeycomb transparently — no OTEL_EXPORTER_OTLP_* env coupling.

    # First-party instrumentations. Each wrapped so a single failure doesn't take the layer down.
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
