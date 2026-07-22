"""VT-690 phase 2 — the tm_audit -> OTel span-event mirror.

Proves the decision-reasoning (GETS/KNOWS/DECIDES/DOES + reasoning_ref) lands on the CURRENT span
as a redacted event, so it rides the Honeycomb trace; and that it is a fail-soft no-op when tracing
is off or no span records.
"""

from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from orchestrator.observability import tm_audit as tm  # noqa: E402


@pytest.fixture(autouse=True)
def _salt(monkeypatch):
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt690-span-mirror-salt")


def _one_span(monkeypatch, *, key: str | None, **mirror_kwargs):
    """Run _mirror_to_span inside a real recording span; return the finished span's events."""
    if key is None:
        monkeypatch.delenv("HONEYCOMB_API_KEY", raising=False)
    else:
        monkeypatch.setenv("HONEYCOMB_API_KEY", key)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("vt690-test")
    with tracer.start_as_current_span("parent"):
        tm._mirror_to_span(**mirror_kwargs)
    provider.force_flush()
    finished = exporter.get_finished_spans()
    return finished[0].events if finished else ()


_BASE = dict(
    event_layer="decides",
    event_kind="route_decided",
    actor="team_manager",
    run_id="11111111-1111-1111-1111-111111111111",
    severity="info",
    status="ok",
    tenant_id="22222222-2222-2222-2222-222222222222",
    input=None,
    action=None,
    result=None,
)


def test_mirror_adds_redacted_event_when_tracing_on(monkeypatch) -> None:
    events = _one_span(
        monkeypatch,
        key="hc_dummy",
        summary="Routing +919876543210 to sales_recovery because 8 customers lapsed",
        decision={"route": "sales_recovery", "confidence": 0.9},
        reasoning_ref={"run_id": "r-1", "step_seq": 3},
        **_BASE,
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.name == "tm_audit.decides.route_decided"
    attrs = dict(ev.attributes)
    assert attrs["tm_audit.layer"] == "decides"
    assert attrs["tm_audit.actor"] == "team_manager"
    assert attrs["run_id"] == "11111111-1111-1111-1111-111111111111"
    # reasoning_ref (the WHY) rides as a JSON attribute.
    assert "step_seq" in attrs["tm_audit.reasoning_ref"]
    assert "sales_recovery" in attrs["tm_audit.decision"]
    # PII in the summary is redacted BEFORE it reaches the span (same redactor as the DB write).
    assert "+919876543210" not in attrs["tm_audit.summary"]
    assert "phone_tok_" in attrs["tm_audit.summary"]


def test_mirror_noop_when_tracing_off(monkeypatch) -> None:
    events = _one_span(monkeypatch, key=None, summary="anything", decision={"x": 1},
                       reasoning_ref=None, **_BASE)
    assert events == () or all(e.name != "tm_audit.decides.route_decided" for e in events)


def test_mirror_never_raises_without_active_span(monkeypatch) -> None:
    monkeypatch.setenv("HONEYCOMB_API_KEY", "hc_dummy")
    # No start_as_current_span here — get_current_span() is the non-recording INVALID span.
    assert trace.get_current_span().is_recording() is False
    tm._mirror_to_span(summary="no span here", decision=None, reasoning_ref=None, **_BASE)  # no raise


def test_mirror_caps_large_field(monkeypatch) -> None:
    big = {"ctx": "x" * 20000}
    events = _one_span(monkeypatch, key="hc_dummy", summary=None, decision=big,
                       reasoning_ref=None, **_BASE)
    assert len(events) == 1
    assert len(dict(events[0].attributes)["tm_audit.decision"]) <= tm._SPAN_ATTR_MAX
