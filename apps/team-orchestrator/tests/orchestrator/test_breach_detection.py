"""VT-79 — breach-detection Phase-1 slice tests.

Live Postgres via DATABASE_URL (CI orchestrator job). Exercises the 3 Phase-1
detectors (tenant-isolation, DSR-rate, PII-in-log) + find_pii + notify_owner.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — breach-detection tests skipped",
)


@pytest.fixture(scope="module")
def pool():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s)",
            (tid, f"vt79-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
    return tid


def _run_with_step(pool, tid: str, *, step_kind: str, input_envelope: str) -> str:
    rid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'twilio_inbound', 'completed')",
            (rid, tid),
        )
        conn.execute(
            "INSERT INTO pipeline_steps (run_id, tenant_id, step_seq, step_kind, "
            "input_envelope, status) VALUES (%s, %s, 0, %s, %s::jsonb, 'completed')",
            (rid, tid, step_kind, input_envelope),
        )
    return rid


# --- find_pii ---------------------------------------------------------------


def test_find_pii_detects_and_ignores():
    from orchestrator.alerts.pii_scrub import find_pii

    assert "phone" in find_pii("call +919812345678 now")
    assert find_pii("hello world") == []
    assert find_pii("[REDACTED:phone]") == []
    # Twilio SID is allowed provenance, not PII.
    assert find_pii(f"SM{'a' * 32}") == []


# --- Detector-5: PII in pipeline_step payloads ------------------------------


def test_detector5_pii_in_logs_fires_on_unredacted_phone(pool):
    from orchestrator.alerts.triggers import detect_pii_in_logs

    tid = _tenant(pool)
    _run_with_step(pool, tid, step_kind="webhook_received",
                   input_envelope='{"leaked": "+919812345678"}')
    triggers = detect_pii_in_logs(tid)
    assert any(t.trigger_kind == "pii_in_log" for t in triggers)
    assert all(t.severity == "critical" for t in triggers)


def test_detector5_clean_payload_no_fire(pool):
    from orchestrator.alerts.triggers import detect_pii_in_logs

    tid = _tenant(pool)
    _run_with_step(pool, tid, step_kind="webhook_received",
                   input_envelope='{"ok": "no pii here"}')
    assert detect_pii_in_logs(tid) == []


# --- Detector-1: tenant-isolation breach ------------------------------------


def test_detector1_tenant_isolation_breach_fires(pool):
    from orchestrator.alerts.triggers import detect_slow_triggers

    tid = _tenant(pool)
    _run_with_step(pool, tid, step_kind="tenant_isolation_breach",
                   input_envelope='{"violation": "cross-tenant read"}')
    triggers = detect_slow_triggers(tid)
    breach = [t for t in triggers if t.trigger_kind == "tenant_isolation_breach"]
    assert breach, "P0 tenant-isolation breach not detected"
    assert breach[0].severity == "critical"


# --- Detector-3: DSR rate anomaly -------------------------------------------


def test_detector3_dsr_rate_anomaly(pool):
    from orchestrator.alerts.triggers import detect_slow_triggers

    tid = _tenant(pool)
    with pool.connection() as conn:
        for _ in range(11):  # threshold is 10
            conn.execute(
                "INSERT INTO dsr_tickets (tenant_id, request_type, status, "
                "acknowledged_at) VALUES (%s, 'deletion', 'acknowledged', now())",
                (tid,),
            )
    triggers = detect_slow_triggers(tid)
    assert any(t.trigger_kind == "dsr_rate_anomaly" for t in triggers)


def test_detector3_under_threshold_no_fire(pool):
    from orchestrator.alerts.triggers import detect_slow_triggers

    tid = _tenant(pool)
    with pool.connection() as conn:
        for _ in range(3):
            conn.execute(
                "INSERT INTO dsr_tickets (tenant_id, request_type, status, "
                "acknowledged_at) VALUES (%s, 'deletion', 'acknowledged', now())",
                (tid,),
            )
    assert not any(
        t.trigger_kind == "dsr_rate_anomaly" for t in detect_slow_triggers(tid)
    )


# --- notify_owner -----------------------------------------------------------


def test_notify_owner_sends(pool, monkeypatch):
    import orchestrator.alerts.breach_notification as bn

    sent = {}
    monkeypatch.setattr(
        bn, "send_freeform_message",
        lambda body, phone, **kw: sent.update(body=body, phone=phone, **kw) or "SMfake",
    )
    tid = _tenant(pool)
    result = bn.notify_owner(tid, "P1", "test breach summary")
    assert result["sent"] is True
    assert result["sid"] == "SMfake"
    # VT-611 Package H0: tenant_id/surface must reach send_freeform_message so this notice lands
    # in the lifetime conversation_log (was bare -> _record_owner_conversation_turn no-op'd).
    assert sent["tenant_id"] == tid
    assert sent["surface"] == "system"


def test_notify_owner_no_phone(pool, monkeypatch):
    import orchestrator.alerts.breach_notification as bn

    monkeypatch.setattr(bn, "send_freeform_message", lambda body, phone, **kw: "x")
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, 'no-phone', 'standard', 'trial')",
            (tid,),
        )
    result = bn.notify_owner(tid, "P1", "x")
    assert result["sent"] is False
