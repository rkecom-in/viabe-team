"""VT-690 Honeycomb export canary (Rule #15) — proves a span actually reaches Honeycomb EU.

Run INSIDE the target env so HONEYCOMB_API_KEY flows OS-env->process (never into operator context):

    railway run --environment development --service vt-orchestrator-service -- \
        uv run --directory apps/team-orchestrator python canaries/vt690_honeycomb_span.py

Configures tracing (send_to_logfire=False + Honeycomb OTLP), emits ONE marker span, then
force-flushes and asserts the export succeeded (a bad/rejected key surfaces as flush=False + the
OTLP exporter's stderr). A PASS means the key authenticated and the span left the process for
Honeycomb — confirm the marker (attribute vt690_canary_marker) appears in the Honeycomb EU UI to
close the wire-level proof.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4


def main() -> int:
    from orchestrator.observability import logfire as obs

    if not obs.is_enabled():
        print("[FAIL] HONEYCOMB_API_KEY not set in this env — cannot canary")
        return 1

    ok = obs.configure_logfire()
    if not ok:
        print("[FAIL] configure_logfire() returned False (see stderr for the reason)")
        return 1

    import logfire

    marker = f"vt690-canary-{uuid4()}"
    with logfire.span("vt690_honeycomb_canary", vt690_canary_marker=marker) as span:
        span.set_attribute("note", "VT-690 Honeycomb EU export proof — synthetic, no customer data")
        time.sleep(0.05)
    logfire.force_flush()  # best-effort async flush of the app path (return value is unreliable)

    # AUTHORITATIVE check: build one span via a SimpleSpanProcessor (synchronous) whose exporter is
    # the SAME Honeycomb OTLP exporter the app uses, and read the actual SpanExportResult. This is a
    # definitive yes/no on "did Honeycomb ACCEPT the span" — no reliance on force_flush's return.
    import os

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api.eu1.honeycomb.io").rstrip("/")
    inner = OTLPSpanExporter(
        endpoint=f"{endpoint}/v1/traces",
        headers={"x-honeycomb-team": os.environ["HONEYCOMB_API_KEY"]},
    )
    results: list[Any] = []

    class _Capture:
        def export(self, spans: Any) -> Any:
            r = inner.export(spans)
            results.append(r)
            return r

        def shutdown(self) -> Any:
            return inner.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return inner.force_flush(timeout_millis)

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_Capture()))
    tracer = provider.get_tracer("vt690-canary")
    with tracer.start_as_current_span("vt690_honeycomb_direct") as sp:
        sp.set_attribute("vt690_canary_marker", marker)
    provider.force_flush()
    provider.shutdown()

    accepted = bool(results) and results[-1] == SpanExportResult.SUCCESS
    print(f"[{'PASS' if accepted else 'FAIL'}] direct OTLP export -> "
          f"{results[-1] if results else 'no result'}")
    print(f"  marker attribute: vt690_canary_marker={marker}")
    if accepted:
        print("  Honeycomb ACCEPTED the span (HTTP 2xx). Confirm the marker in Honeycomb EU "
              "(service 'team-orchestrator') to eyeball the data.")
    obs.shutdown()
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
