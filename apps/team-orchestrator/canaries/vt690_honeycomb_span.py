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

    # BatchSpanProcessor exports async; force_flush blocks until the batch is sent (or times out).
    flushed = logfire.force_flush()
    print(f"[{'PASS' if flushed else 'FAIL'}] span emitted + force_flush -> {flushed}")
    print(f"  marker attribute: vt690_canary_marker={marker}")
    print("  -> confirm this marker appears in Honeycomb EU (service 'team-orchestrator') to close "
          "the wire proof")
    obs.shutdown()
    return 0 if flushed else 1


if __name__ == "__main__":
    raise SystemExit(main())
