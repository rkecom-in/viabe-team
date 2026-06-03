"""VT-80 — privacy_audit_log hash-chain + append-only substrate tests.

Live Postgres via DATABASE_URL (CI ``orchestrator`` job). Exercises the real
chain writer + verifier + the immutability trigger against the migrated schema.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — audit hash-chain tests skipped",
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
            dsn,
            min_size=1,
            max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    return graph_mod.get_pool()


def _seed_tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s) ON CONFLICT (id) DO NOTHING",
            (tid, f"vt80t-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
    return tid


def test_two_appends_chain_and_verify_ok(pool):
    from orchestrator.observability.audit_log import list_events, log_privacy_event
    from orchestrator.observability.audit_verify import verify_chain

    tid = _seed_tenant(pool)
    with pool.connection() as conn:
        h1 = log_privacy_event(
            conn, tenant_id=tid, event_type="phone_token_resolved",
            payload={"phone_token": "t1", "resolved": True}, actor="test",
        )
        h2 = log_privacy_event(
            conn, tenant_id=tid, event_type="subject_data_purged",
            payload={"ticket_id": str(uuid4())}, actor="dsr_purge",
        )
        rows = {e["this_hash"]: e for e in list_events(conn, limit=5)}
        assert rows[h2]["prev_hash"] == h1
        assert h1 != h2
        assert verify_chain(conn).ok


def test_tamper_detected(pool):
    from orchestrator.observability.audit_log import log_privacy_event
    from orchestrator.observability.audit_verify import verify_chain

    tid = _seed_tenant(pool)
    with pool.connection() as conn:
        h = log_privacy_event(
            conn, tenant_id=tid, event_type="phone_token_resolved",
            payload={"phone_token": "tamper-me", "resolved": True}, actor="test",
        )
        conn.execute(
            "ALTER TABLE privacy_audit_log DISABLE TRIGGER privacy_audit_log_no_row_mutate"
        )
        try:
            conn.execute(
                "UPDATE privacy_audit_log SET payload = %s::jsonb WHERE this_hash = %s",
                ('{"phone_token": "X", "resolved": false}', h),
            )
            result = verify_chain(conn)
            # Restore the original payload so the shared-DB chain is left clean
            # (other test files run whole-table verify_chain).
            conn.execute(
                "UPDATE privacy_audit_log SET payload = %s::jsonb WHERE this_hash = %s",
                ('{"phone_token": "tamper-me", "resolved": true}', h),
            )
        finally:
            conn.execute(
                "ALTER TABLE privacy_audit_log ENABLE TRIGGER privacy_audit_log_no_row_mutate"
            )
    assert result.ok is False
    assert result.broken_seq is not None
    # chain restored → whole-table verify is clean again
    with pool.connection() as conn:
        assert verify_chain(conn).ok


def test_update_blocked_by_immutability_trigger(pool):
    from orchestrator.observability.audit_log import log_privacy_event

    tid = _seed_tenant(pool)
    with pool.connection() as conn:
        h = log_privacy_event(
            conn, tenant_id=tid, event_type="phone_token_resolved",
            payload={"phone_token": "ro"}, actor="test",
        )
    with pool.connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(
                "UPDATE privacy_audit_log SET actor = 'hax' WHERE this_hash = %s", (h,)
            )


def test_delete_blocked_by_immutability_trigger(pool):
    from orchestrator.observability.audit_log import log_privacy_event

    tid = _seed_tenant(pool)
    with pool.connection() as conn:
        h = log_privacy_event(
            conn, tenant_id=tid, event_type="phone_token_resolved",
            payload={"phone_token": "rodel"}, actor="test",
        )
    with pool.connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM privacy_audit_log WHERE this_hash = %s", (h,))


def test_event_type_check_rejects_unseeded(pool):
    from orchestrator.observability.audit_log import log_privacy_event

    tid = _seed_tenant(pool)
    with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            log_privacy_event(
                conn, tenant_id=tid, event_type="not_a_seeded_event",
                payload={}, actor="test",
            )
