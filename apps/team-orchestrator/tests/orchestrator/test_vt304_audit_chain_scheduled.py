"""VT-304 — nightly audit-chain-verify scheduled handler canary.

The handler verifies the VT-80 privacy_audit_log hash-chain and, on a break,
raises a CRITICAL WORKSPACE alert direct to the OPS channel (not the per-tenant
tenant_alerts path — the chain spans NULL-tenant rows). Asserts: clean chain →
no alert; a tampered row → the OPS send IS invoked. CL-422 synthetic.
(The 8th-handler registration + idempotency is covered by test_scheduled_triggers.)
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-304 audit-chain canary skipped",
)


def _pool():
    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"], min_size=1, max_size=2,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


@pytest.fixture(scope="module")
def pool():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    return _pool()


def _seed_chain(pool, tenant_id: str, n: int = 3) -> None:
    from orchestrator.observability.audit_log import log_privacy_event

    with pool.connection() as conn:
        for i in range(n):
            log_privacy_event(
                conn, tenant_id=tenant_id, event_type="phone_token_resolved",
                payload={"i": i}, actor="vt304-test",
            )


def _install_alert_spy(monkeypatch) -> dict:
    """Spy on the OPS send primitives the chain-break alert uses."""
    calls = {"telegram": 0, "email": 0}

    async def _tg(*_a, **_k):
        calls["telegram"] += 1
        return True

    async def _em(*_a, **_k):
        calls["email"] += 1
        return True

    monkeypatch.setattr("orchestrator.alerts.clients.send_telegram", _tg)
    monkeypatch.setattr("orchestrator.alerts.clients.send_resend_email", _em)
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "x")
    monkeypatch.setenv("RESEND_API_KEY", "x")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "a@b.c")
    monkeypatch.setenv("RESEND_TO_EMAIL", "d@e.f")
    # VT-502: the chain-break alert is now dev-routing-gated (alert_is_dev_routed).
    # OPS Telegram + email is the PROD behaviour; on dev it routes to the DEV bot
    # and skips email. This canary asserts the full PROD path, so pin prod.
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    return calls


def test_audit_chain_scheduled_alerts_only_on_break(pool, monkeypatch):
    """Sequential (order-independent): clean chain → no alert; then tamper the
    newest row → the handler raises the CRITICAL OPS alert. Verifying the global
    chain, so a single sequential test avoids cross-test poisoning."""
    from datetime import UTC, datetime

    from orchestrator.observability.audit_verify import run_audit_chain_verify_body
    from orchestrator.scheduled_triggers import audit_chain_verify_scheduled

    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, 'vt304', 'founding', 'onboarding')", (tid,),
        )
    _seed_chain(pool, tid)
    now = datetime.now(UTC)

    # 1. Clean chain → body OK → handler fires NO alert.
    assert run_audit_chain_verify_body().ok is True
    calls = _install_alert_spy(monkeypatch)
    audit_chain_verify_scheduled(now, now)
    assert calls == {"telegram": 0, "email": 0}, "clean chain must not alert"

    # 2. Tamper the newest row's stored hash → integrity break → handler ALERTS.
    #    privacy_audit_log is append-only (VT-80 trigger blocks UPDATE), so a real
    #    tamper means bypassing that trigger (direct DB / superuser) — exactly what
    #    verify_chain is the backstop for. Simulate by disabling the trigger.
    with pool.connection() as conn:
        conn.execute("ALTER TABLE privacy_audit_log DISABLE TRIGGER privacy_audit_log_no_row_mutate")
        try:
            conn.execute(
                "UPDATE privacy_audit_log SET this_hash = %s "
                "WHERE seq = (SELECT max(seq) FROM privacy_audit_log)",
                ("deadbeef" * 8,),
            )
        finally:
            conn.execute("ALTER TABLE privacy_audit_log ENABLE TRIGGER privacy_audit_log_no_row_mutate")
    assert run_audit_chain_verify_body().ok is False  # body detects the break
    audit_chain_verify_scheduled(now, now)
    assert calls["telegram"] >= 1, "a chain break must raise the OPS Telegram alert"
    assert calls["email"] >= 1, "a chain break must raise the OPS email alert"
