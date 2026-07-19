"""VT-599 — ``resolve_lane_tenant`` / ``lane_tenant_error`` (the shared lane tool-tenant seam).

Pins the helper EVERY lane tool with a ``tenant_id`` param now calls before touching the DB:

  1. An ambient dispatch ``ObservabilityContext`` is ALWAYS authoritative when present — a
     matching model value, a foreign (but valid) UUID, or a non-UUID (a business name) all
     resolve to the CONTEXT tenant; only the mismatch cases log a WARNING.
  2. No ambient context falls back to parsing the model value as a UUID — returns it when valid,
     ``None`` when not (and when the model value itself is absent).
  3. The warning never carries more than a 20-char prefix of the model's value (log hygiene).
  4. ``lane_tenant_error`` is the structured, non-raising tool-error a lane tool returns on
     ``None`` — mirrors the ``record_business_objective`` / ``search_conversation_history``
     precedent in ``agent/orchestrator_agent.py``.

Constructing ``ObservabilityContext`` / calling ``resolve_lane_tenant`` pulls
``orchestrator.observability.decorators`` -> ``pipeline_observability`` -> ``psycopg`` at CALL
time (lazy import inside the function) — skip under the dep-less smoke where psycopg is absent.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.agent.lane_tenant import lane_tenant_error, resolve_lane_tenant  # noqa: E402
from orchestrator.observability.decorators import observability_context  # noqa: E402


# --- (1) ambient context is ALWAYS authoritative -------------------------------------------------


def test_context_present_no_model_value_returns_context_tenant() -> None:
    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        assert resolve_lane_tenant(None, tool_name="some_tool") == tenant_id


def test_context_present_model_value_matches_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A model value that happens to equal the context tenant resolves silently (no mismatch)."""
    run_id, tenant_id = uuid4(), uuid4()
    with caplog.at_level(logging.WARNING, logger="orchestrator.agent.lane_tenant"):
        with observability_context(run_id=run_id, tenant_id=tenant_id):
            resolved = resolve_lane_tenant(str(tenant_id), tool_name="some_tool")
    assert resolved == tenant_id
    assert not caplog.records


def test_context_present_model_supplied_business_name_overridden(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The VT-599 live defect: the model fills tenant_id with the business NAME, not a UUID.

    The tool must execute against the CONTEXT tenant, not raise, and log a mismatch warning
    naming the tool."""
    run_id, tenant_id = uuid4(), uuid4()
    with caplog.at_level(logging.WARNING, logger="orchestrator.agent.lane_tenant"):
        with observability_context(run_id=run_id, tenant_id=tenant_id):
            resolved = resolve_lane_tenant(
                "Sundaram Stores Diwali campaign", tool_name="list_recent_campaigns"
            )
    assert resolved == tenant_id
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "list_recent_campaigns" in msg
    assert "mismatch" in msg.lower()


def test_context_present_model_supplied_foreign_uuid_overridden(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A syntactically-valid but WRONG (foreign-tenant) UUID from the model is still overridden by
    the context tenant + logged — the IDOR-class case (VT-293/294): a model-authored foreign UUID
    must never win over the run's own scope."""
    run_id, tenant_id, foreign_tenant = uuid4(), uuid4(), uuid4()
    with caplog.at_level(logging.WARNING, logger="orchestrator.agent.lane_tenant"):
        with observability_context(run_id=run_id, tenant_id=tenant_id):
            resolved = resolve_lane_tenant(str(foreign_tenant), tool_name="check_send_intent")
    assert resolved == tenant_id
    assert resolved != foreign_tenant
    assert len(caplog.records) == 1
    assert "check_send_intent" in caplog.records[0].getMessage()


def test_mismatch_warning_truncates_model_value_to_20_chars(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The logged model-value fragment never exceeds 20 chars (log hygiene — never the full value)."""
    run_id, tenant_id = uuid4(), uuid4()
    long_name = "A" * 200
    with caplog.at_level(logging.WARNING, logger="orchestrator.agent.lane_tenant"):
        with observability_context(run_id=run_id, tenant_id=tenant_id):
            resolve_lane_tenant(long_name, tool_name="draft_content")
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert long_name not in msg
    assert "A" * 20 in msg
    assert "A" * 21 not in msg


# --- (2) no ambient context: fall back to parsing the model value --------------------------------


def test_no_context_valid_model_uuid_is_returned() -> None:
    tid = uuid4()
    assert resolve_lane_tenant(str(tid), tool_name="some_tool") == tid


def test_no_context_invalid_model_value_returns_none() -> None:
    assert resolve_lane_tenant("Sundaram Stores", tool_name="some_tool") is None


def test_no_context_no_model_value_returns_none() -> None:
    assert resolve_lane_tenant(None, tool_name="some_tool") is None


# --- (3) the structured, non-raising tool error --------------------------------------------------


def test_lane_tenant_error_shape() -> None:
    err = lane_tenant_error("list_recent_campaigns")
    assert err == {
        "status": "error",
        "error": "list_recent_campaigns: no resolvable tenant context",
    }
