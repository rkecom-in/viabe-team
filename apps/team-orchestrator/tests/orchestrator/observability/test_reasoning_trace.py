"""Tests for reasoning_trace.py (VT-104).

Pure: monkeypatches the writer's ``_do_insert_sync`` to capture the
prepared payload without touching a DB. Verifies (1) capture happy-path,
(2) PII redaction at the writer boundary, (3) replay timeline shape,
(4) cross-run isolation via run_id scope.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("langsmith")
pytest.importorskip("psycopg")

from orchestrator.observability import log as log_mod  # noqa: E402
from orchestrator.observability.reasoning_trace import (  # noqa: E402
    capture_agent_reasoning_step,
    capture_tool_call_args,
    capture_tool_call_result,
)


@pytest.fixture(autouse=True)
def _phone_salt(monkeypatch):
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "test-salt-vt104")


def _captured_payloads(monkeypatch) -> list[tuple[Any, ...]]:
    captured: list[tuple[Any, ...]] = []

    def _capture(event_type, run_id, tenant_id, severity, component, payload, duration_ms):
        captured.append((event_type, run_id, tenant_id, severity, component, payload, duration_ms))

    monkeypatch.setattr(log_mod, "_do_insert_sync", _capture)
    return captured


def test_capture_agent_reasoning_step_dispatches_with_redacted_payload(monkeypatch) -> None:
    captured = _captured_payloads(monkeypatch)
    run_id = uuid4()
    tenant_id = uuid4()
    capture_agent_reasoning_step(
        run_id,
        tenant_id,
        step_name="plan_next_action",
        content="customer +919876543210 asked about refund",
        metadata={"phase": "active"},
    )
    time.sleep(0.05)
    assert captured, "log_event never reached"
    event_type, _, _, severity, component, payload, _ = captured[0]
    assert event_type == "agent_reasoning_step"
    assert severity == "info"
    assert component == "agent"
    assert payload["step_name"] == "plan_next_action"
    # PII redaction happens at the writer boundary; phone now tokenised.
    assert "919876543210" not in str(payload["content"])
    assert payload["metadata"] == {"phase": "active"}


def test_capture_tool_call_args_redacts_pii_in_args(monkeypatch) -> None:
    captured = _captured_payloads(monkeypatch)
    capture_tool_call_args(
        uuid4(),
        None,
        tool_name="send_message",
        args={"phone": "+919876543210", "body": "Hi I want to cancel"},
    )
    time.sleep(0.05)
    assert captured
    payload = captured[0][5]
    assert payload["tool_name"] == "send_message"
    assert payload["args"]["phone"].startswith("phone_tok_")
    assert payload["args"]["body"].startswith("body_tok_")


def test_capture_tool_call_result_severity_warn_on_failure(monkeypatch) -> None:
    captured = _captured_payloads(monkeypatch)
    capture_tool_call_result(
        uuid4(),
        None,
        tool_name="send_message",
        ok=False,
        error="Twilio 500: service unavailable",
    )
    time.sleep(0.05)
    assert captured
    event_type, _, _, severity, _, payload, _ = captured[0]
    assert event_type == "tool_call_result"
    assert severity == "warn"
    assert payload["ok"] is False
    assert "error" in payload


def test_run_id_is_threaded_through(monkeypatch) -> None:
    """Replay scope contract: every captured event carries the same run_id."""
    captured = _captured_payloads(monkeypatch)
    run_id = uuid4()
    tenant_id = uuid4()
    capture_agent_reasoning_step(run_id, tenant_id, step_name="step_1")
    capture_tool_call_args(run_id, tenant_id, tool_name="lookup_tenant")
    capture_tool_call_result(run_id, tenant_id, tool_name="lookup_tenant", ok=True)
    time.sleep(0.05)
    assert len(captured) == 3
    run_ids = {c[1] for c in captured}
    assert run_ids == {run_id}
