"""VT-3.8 tests — Pre-Filter Gate (Stage 1) + 5 direct handlers.

Require a live Postgres via ``DATABASE_URL`` plus the dbos / langgraph stack.
Skipped in the lightweight unit-test job; run in the CI ``orchestrator`` job.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("langgraph")

import psycopg  # noqa: E402 — imported after the dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — pre-filter tests skipped",
)


@pytest.fixture(scope="module")
def gate():
    """Apply migrations, launch DBOS, expose the gate + handler registry."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    apply_migrations.apply(dsn=dsn)
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos
    from orchestrator import types
    from orchestrator.direct_handlers import HANDLERS
    from orchestrator.pre_filter_gate import pre_filter

    launch_dbos()
    try:
        yield SimpleNamespace(
            dsn=dsn, pre_filter=pre_filter, HANDLERS=HANDLERS, t=types
        )
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at) "
            "VALUES ('VT-3.8 Test', 'founding', 'trial', now()) RETURNING id"
        ).fetchone()
    assert row is not None
    return str(row[0])


def _inbound(gate, body: str):
    return gate.t.WebhookEvent(body=body, sender_phone="+910000000000")


def _callback(gate, state: str):
    return gate.t.WebhookEvent(
        message_type="status_callback",
        status_callback_state=state,
        twilio_message_sid="SM-test",
    )


# --- Routing rules -----------------------------------------------------------


def test_opt_out_keyword_en_routes_and_sets_flag(gate):
    tenant_id = _new_tenant(gate.dsn)
    tenant = gate.t.Tenant(tenant_id=tenant_id)
    result = gate.pre_filter(_inbound(gate, "STOP"), tenant)

    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "opt_out_handler"

    outcome = gate.HANDLERS["opt_out_handler"](_inbound(gate, "STOP"), tenant)
    assert outcome["opt_out_set"] is True
    assert outcome["confirmation_sent"] is True

    with psycopg.connect(gate.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT opt_out FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    assert row == (True,)


def test_opt_out_keyword_hi_routes_and_sets_flag(gate):
    tenant_id = _new_tenant(gate.dsn)
    tenant = gate.t.Tenant(tenant_id=tenant_id, preferred_language="hi")
    result = gate.pre_filter(_inbound(gate, "बंद करो"), tenant)

    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "opt_out_handler"

    gate.HANDLERS["opt_out_handler"](_inbound(gate, "बंद करो"), tenant)
    with psycopg.connect(gate.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT opt_out FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    assert row == (True,)


def test_dsr_keyword_routes_creates_ticket(gate):
    tenant_id = _new_tenant(gate.dsn)
    tenant = gate.t.Tenant(tenant_id=tenant_id)
    event = _inbound(gate, "I want data deletion for my account")

    result = gate.pre_filter(event, tenant)
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "dsr_handler"

    outcome = gate.HANDLERS["dsr_handler"](event, tenant)
    assert outcome["acknowledgment_sent"] is True
    assert outcome["dsr_ticket_id"] is not None

    with psycopg.connect(gate.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, request_type FROM dsr_tickets WHERE id = %s",
            (outcome["dsr_ticket_id"],),
        ).fetchone()
    assert row == ("acknowledged", "deletion")


def test_status_callback_delivered_is_rejected(gate):
    tenant = gate.t.Tenant(tenant_id=uuid4())
    result = gate.pre_filter(_callback(gate, "delivered"), tenant)
    assert isinstance(result, gate.t.Reject)
    assert "observability" in result.reason


def test_status_callback_failed_routes_to_template_error_handler(gate):
    tenant = gate.t.Tenant(tenant_id=uuid4())
    result = gate.pre_filter(_callback(gate, "failed"), tenant)
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "template_error_handler"


def test_status_ping_routes_to_status_ping_handler(gate):
    tenant_id = _new_tenant(gate.dsn)
    tenant = gate.t.Tenant(tenant_id=tenant_id)
    result = gate.pre_filter(_inbound(gate, "hi"), tenant)
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "status_ping_handler"

    outcome = gate.HANDLERS["status_ping_handler"](_inbound(gate, "hi"), tenant)
    assert outcome["reply_sent"] is True
    assert "trial" in outcome["status_text"]  # accurate phase, no fabrication


def test_substantive_message_routes_to_brain(gate):
    tenant = gate.t.Tenant(tenant_id=uuid4())
    result = gate.pre_filter(
        _inbound(gate, "I want to launch a campaign for dormant customers"), tenant
    )
    assert isinstance(result, gate.t.RouteToBrain)
    assert "substantive owner message" in result.reason


def test_ambiguous_message_routes_to_brain(gate):
    tenant = gate.t.Tenant(tenant_id=uuid4())
    result = gate.pre_filter(
        _inbound(gate, "how are things going with the cafe customers"), tenant
    )
    assert isinstance(result, gate.t.RouteToBrain)


# --- Capture ratio -----------------------------------------------------------


def test_capture_ratio_on_synthetic_event_mix(gate):
    """A synthetic 100-event mix routes 60-80% to direct handlers / reject.

    Capture ratio = (RouteToDirectHandler + Reject) / total (Notion §4).
    """
    tenant = gate.t.Tenant(tenant_id=uuid4())
    events: list = []
    events += [_inbound(gate, "STOP") for _ in range(20)]
    events += [_inbound(gate, "बंद करो") for _ in range(20)]
    events += [_inbound(gate, "please process data deletion") for _ in range(15)]
    events += [_inbound(gate, "hi") for _ in range(5)]
    events += [_inbound(gate, "any update") for _ in range(5)]
    events += [_callback(gate, "failed") for _ in range(5)]
    events += [_callback(gate, "delivered") for _ in range(5)]
    events += [
        _inbound(gate, f"I want to plan campaign number {i} for my shop")
        for i in range(25)
    ]
    assert len(events) == 100

    captured = 0
    for event in events:
        result = gate.pre_filter(event, tenant)
        if isinstance(result, (gate.t.RouteToDirectHandler, gate.t.Reject)):
            captured += 1

    ratio = captured / len(events)
    assert 0.60 <= ratio <= 0.80, f"capture ratio {ratio:.2f} outside 60-80%"
