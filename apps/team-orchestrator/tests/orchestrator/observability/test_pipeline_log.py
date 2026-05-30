"""Tests for pipeline_log writer + schemas + query (VT-102).

Pure unit tests (PII redaction at write, schema validation) run
unconditionally. DB-backed integration tests use
``@pytest.mark.integration`` so they skip unless ``RUN_INTEGRATION_TESTS=1``
is set; the `orchestrator` CI job sets the var and runs them.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from uuid import UUID, uuid4

import pytest

# Skip the suite when langsmith isn't installed (smoke step). The redactor
# helpers live inside observability/ so even pure cases import the package.
pytest.importorskip("langsmith")
pytest.importorskip("psycopg")

from orchestrator.observability import (  # noqa: E402
    log_event,
    purge_pipeline_log_older_than,
    query_run,
)
from orchestrator.observability import log as log_mod  # noqa: E402
from orchestrator.observability.event_schemas import EVENT_SCHEMAS, validate  # noqa: E402
from orchestrator.observability.pii import redact_for_log  # noqa: E402


CANARY_TENANT_A = UUID("00000000-0000-4000-8000-000000aaaaaa")
CANARY_TENANT_B = UUID("00000000-0000-4000-8000-000000bbbbbb")


@pytest.fixture(autouse=True)
def _phone_salt(monkeypatch):
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "test-salt-vt102")


# ---------------------------------------------------------------------------
# Suite 2 — PII redaction at write (pure, no DB)
# ---------------------------------------------------------------------------

def test_phone_redacted_in_payload_string() -> None:
    redacted = redact_for_log("Customer called from +919876543210 yesterday")
    assert "9876543210" not in redacted
    assert "phone_tok_" in redacted


def test_named_pii_keys_tokenized_for_log() -> None:
    redacted = redact_for_log(
        {
            "customer_name": "Rajesh Kumar",
            "phone": "+919876543210",
            "body": "Hi I want to cancel",
            "stack_trace": "Traceback... +919876543210",
            "error_message": "failed for user Rajesh",
            "tenant_id": "tenant-x",
        }
    )
    assert redacted["customer_name"].startswith("<redacted:customer_name")
    assert redacted["phone"].startswith("phone_tok_")
    assert redacted["body"].startswith("body_tok_")
    assert redacted["stack_trace"].startswith("body_tok_")
    assert redacted["error_message"].startswith("body_tok_")
    assert redacted["tenant_id"] == "tenant-x"


# ---------------------------------------------------------------------------
# Suite — Schema validation (pure)
# ---------------------------------------------------------------------------

def test_known_event_type_with_valid_payload_passes() -> None:
    ok, errors = validate("db_write", {"table_name": "owner_inputs", "operation_type": "insert"})
    assert ok is True
    assert errors == []


def test_known_event_type_with_invalid_field_fails() -> None:
    ok, errors = validate("db_write", {"table_name": "owner_inputs", "operation_type": "truncate"})
    assert ok is False
    assert any("operation_type" in e for e in errors)


def test_known_event_type_missing_field_fails() -> None:
    ok, errors = validate("external_api_call", {"vendor": "anthropic"})
    assert ok is False
    assert any("endpoint" in e for e in errors)


def test_unknown_event_type_fails_softly() -> None:
    ok, errors = validate("never_heard_of_this_event", {"k": "v"})
    assert ok is False
    assert any("unknown event_type" in e for e in errors)


def test_event_schemas_covers_brief_taxonomy() -> None:
    # 14 canonical event types from the brief plus canary_test.
    expected = {
        "webhook_received",
        "webhook_signature_verified",
        "agent_dispatched",
        "tool_invoked",
        "tool_completed",
        "db_write",
        "external_api_call",
        "external_api_response",
        "error",
        "phase_transition",
        "scheduled_trigger_fired",
        "delivery_attempted",
        "payment_event",
        "consent_event",
        "canary_test",
    }
    assert expected.issubset(EVENT_SCHEMAS.keys())


# ---------------------------------------------------------------------------
# Suite 2 (pure) — log_event prepares payload through redactor + validator
# ---------------------------------------------------------------------------

def test_log_event_redacts_and_annotates_invalid(monkeypatch) -> None:
    """log_event prepares the payload before scheduling the insert.

    We assert the prepared payload contains redacted PII AND the validation
    flag when the event_type schema rejects it. The actual insert is captured
    via a monkeypatched _do_insert_sync so we don't need a DB.
    """
    captured: list[tuple[Any, ...]] = []

    def _capture(event_type, run_id, tenant_id, severity, component, payload, duration_ms):
        captured.append((event_type, run_id, tenant_id, severity, component, payload, duration_ms))

    monkeypatch.setattr(log_mod, "_do_insert_sync", _capture)

    log_event(
        event_type="db_write",  # known schema
        run_id=uuid4(),
        tenant_id=None,
        severity="info",
        component="canary",
        payload={
            "table_name": "x",
            "operation_type": "drop",  # invalid → soft validation failure
            "phone": "+919876543210",
            "customer_name": "Rajesh Kumar",
        },
    )
    # Allow the daemon thread to run.
    time.sleep(0.05)

    assert captured, "insert not scheduled"
    payload = captured[0][5]
    # Schema flagged the invalid operation_type
    assert payload.get("payload_validation_failed") is True
    # PII redacted
    assert payload["phone"].startswith("phone_tok_")
    assert payload["customer_name"].startswith("<redacted:customer_name")
    # Non-PII fields preserved
    assert payload["table_name"] == "x"
    assert payload["operation_type"] == "drop"


def test_log_event_coerces_invalid_severity(monkeypatch, capsys) -> None:
    monkeypatch.setattr(log_mod, "_do_insert_sync", lambda *a, **kw: None)
    log_event("canary_test", uuid4(), None, severity="NUKE", component="x", payload={"k": "v"})
    time.sleep(0.02)
    assert "invalid severity" in capsys.readouterr().err


def test_log_event_returns_immediately(monkeypatch) -> None:
    """The call site must not block on the insert; we prove this by hanging
    the insert for 200ms inside the daemon thread and asserting the caller
    returned within a much tighter wall-clock budget."""
    finished = threading.Event()

    def _slow(*args, **kwargs):
        time.sleep(0.2)
        finished.set()

    monkeypatch.setattr(log_mod, "_do_insert_sync", _slow)
    t0 = time.perf_counter()
    log_event("canary_test", uuid4(), None, "info", "x", {"k": "v"})
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.05, f"log_event blocked for {elapsed:.3f}s"
    finished.wait(timeout=1.0)


# ---------------------------------------------------------------------------
# Integration suite — DB-backed (gated)
# ---------------------------------------------------------------------------


@pytest.fixture
def _dbpool():
    """Open the orchestrator's connection pool for integration tests.

    The pool is normally initialised by DBOS workflow launch; for these
    tests we init it directly with the env's DATABASE_URL (the
    `orchestrator` CI job sets it) so we don't need DBOS substrate setup.
    """
    import os

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL not set; integration test requires real DB")

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            db_url,
            min_size=1,
            max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )

    yield get_pool()


def _sql(pool, statement: str, params: tuple = ()) -> list[Any]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(statement, params)
        if cur.description is None:
            return []
        return list(cur.fetchall())


def _seed_tenant(pool, tenant_id: UUID) -> None:
    """Idempotently insert a test tenants row so FK + RLS resolve."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-{tenant_id}"),
        )


def _poll_until(fn, want, *, timeout: float = 10.0, interval: float = 0.1):
    """VT-245: poll fn() until it equals `want` or timeout.

    `log_event` writes are not synchronous w.r.t. the caller; a fixed
    `time.sleep` raced the commit-visibility under CI load (the
    RLS service_count + chrono-order flakes: count came up short). Polling
    until the expected row count lands removes the timing dependency
    entirely — deterministic regardless of how fast/slow the writes commit.
    Returns the last observed value (caller asserts on it).
    """
    deadline = time.monotonic() + timeout
    last = fn()
    while last != want and time.monotonic() < deadline:
        time.sleep(interval)
        last = fn()
    return last


@pytest.mark.integration
def test_append_only_under_app_role(_dbpool) -> None:
    """Suite 1 — UPDATE / DELETE under app_role raise permission denied."""
    import psycopg

    _seed_tenant(_dbpool, CANARY_TENANT_A)
    run_id = uuid4()
    log_event("canary_test", run_id, CANARY_TENANT_A, "info", "test", {"k": "v"})
    time.sleep(0.3)

    with _dbpool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"SET app.current_tenant = '{CANARY_TENANT_A}'")
        cur.execute("SET ROLE app_role")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("UPDATE pipeline_log SET payload = '{}'::jsonb WHERE run_id = %s", (str(run_id),))
        conn.rollback()
        cur.execute("SET ROLE app_role")
        cur.execute(f"SET app.current_tenant = '{CANARY_TENANT_A}'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("DELETE FROM pipeline_log WHERE run_id = %s", (str(run_id),))


@pytest.mark.integration
def test_query_run_returns_chronologically_ordered(_dbpool) -> None:
    """Suite 3 — synthetic 50-event run is returned in ascending order."""
    _seed_tenant(_dbpool, CANARY_TENANT_A)
    run_id = uuid4()
    for i in range(50):
        log_event(
            "canary_test",
            run_id,
            CANARY_TENANT_A,
            "info",
            "test",
            {"k": f"v{i}"},
            duration_ms=i,
        )

    # VT-245: poll until all 50 land (was a fixed sleep(0.5) that raced the
    # async commit under CI load → intermittent `assert len == 50` with 47/48).
    count = _poll_until(lambda: len(query_run(run_id)), 50)
    events = query_run(run_id)
    assert len(events) == 50, f"expected 50 events, got {count}"
    timestamps = [e.created_at for e in events]
    assert timestamps == sorted(timestamps), "events not chronological"


@pytest.mark.integration
def test_cross_tenant_blocked(_dbpool) -> None:
    """Suite 4 — tenant_B can't read tenant_A's rows; tenant_A sees its own."""
    _seed_tenant(_dbpool, CANARY_TENANT_A)
    _seed_tenant(_dbpool, CANARY_TENANT_B)
    run_id = uuid4()
    log_event("canary_test", run_id, CANARY_TENANT_A, "info", "test", {"k": "v"})
    time.sleep(0.3)

    from orchestrator.db import tenant_connection

    with tenant_connection(CANARY_TENANT_A) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM pipeline_log WHERE run_id = %s", (str(run_id),))
        count_a = cur.fetchone()["c"]
    with tenant_connection(CANARY_TENANT_B) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM pipeline_log WHERE run_id = %s", (str(run_id),))
        count_b = cur.fetchone()["c"]
    assert count_a == 1
    assert count_b == 0


@pytest.mark.integration
def test_workspace_null_tenant_not_visible_under_app_role(_dbpool) -> None:
    """Suite 5 — tenant_id NULL rows are workspace-level: not visible via app_role."""
    _seed_tenant(_dbpool, CANARY_TENANT_A)
    run_id = uuid4()
    log_event("canary_test", run_id, None, "info", "test", {"k": "workspace"})

    def _service_count() -> int:
        with _dbpool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM pipeline_log WHERE run_id = %s",
                (str(run_id),),
            )
            return cur.fetchone()["c"]

    # VT-245: poll until the async NULL-tenant write lands (was sleep(0.3)
    # that raced the commit → intermittent `assert service_count == 1` with 0).
    service_count = _poll_until(_service_count, 1)
    # app_role does not see workspace-level rows
    from orchestrator.db import tenant_connection

    with tenant_connection(CANARY_TENANT_A) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM pipeline_log WHERE run_id = %s", (str(run_id),))
        app_count = cur.fetchone()["c"]
    assert service_count == 1
    assert app_count == 0


@pytest.mark.integration
def test_workspace_null_tenant_write_read_loop(_dbpool) -> None:
    """Suite 5a (review condition #1) — service-role INSERT of NULL row succeeds;
    SELECT under service role returns 1; SELECT under tenant_A app_role returns 0.
    Closes the write+read loop for workspace events."""
    _seed_tenant(_dbpool, CANARY_TENANT_A)
    run_id = uuid4()
    # Service-role direct insert (mirrors log.py's NULL-tenant path).
    with _dbpool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_log
                (run_id, tenant_id, event_type, severity, component, payload)
            VALUES (%s, NULL, 'canary_test', 'info', 'test', '{"k": "workspace5a"}')
            """,
            (str(run_id),),
        )

    with _dbpool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM pipeline_log WHERE run_id = %s", (str(run_id),))
        service_count = cur.fetchone()["c"]
    from orchestrator.db import tenant_connection

    with tenant_connection(CANARY_TENANT_A) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM pipeline_log WHERE run_id = %s", (str(run_id),))
        app_count = cur.fetchone()["c"]
    assert service_count == 1, "service role should see its own NULL-tenant insert"
    assert app_count == 0, "app_role must NOT see workspace-level rows"


@pytest.mark.integration
def test_schema_validation_writes_flag_on_invalid(_dbpool) -> None:
    """Suite 6 — invalid payload still writes; the flag annotation is present."""
    _seed_tenant(_dbpool, CANARY_TENANT_A)
    run_id = uuid4()
    log_event(
        "db_write",
        run_id,
        CANARY_TENANT_A,
        "info",
        "test",
        {"table_name": "x", "operation_type": "drop"},  # invalid op
    )
    time.sleep(0.3)
    events = query_run(run_id)
    assert len(events) == 1
    assert events[0].payload.get("payload_validation_failed") is True


@pytest.mark.integration
def test_log_event_from_running_asyncio_loop(_dbpool) -> None:
    """Suite 2b (review condition #2, async path) — log_event from inside a
    running asyncio loop schedules the insert via loop.create_task; the row
    lands in DB."""
    _seed_tenant(_dbpool, CANARY_TENANT_A)
    run_id = uuid4()

    async def _runner() -> None:
        log_event("canary_test", run_id, CANARY_TENANT_A, "info", "test", {"k": "loop"})
        # Yield to let the scheduled task complete.
        await asyncio.sleep(0.5)

    asyncio.run(_runner())
    events = query_run(run_id)
    assert len(events) == 1
    assert events[0].payload["k"] == "loop"


@pytest.mark.integration
def test_log_event_from_sync_context(_dbpool) -> None:
    """Suite 2b (review condition #2, sync path) — log_event from a synchronous
    context dispatches on a daemon thread; the row lands in DB."""
    _seed_tenant(_dbpool, CANARY_TENANT_A)
    run_id = uuid4()
    log_event("canary_test", run_id, CANARY_TENANT_A, "info", "test", {"k": "sync"})
    time.sleep(0.5)
    events = query_run(run_id)
    assert len(events) == 1
    assert events[0].payload["k"] == "sync"


@pytest.mark.integration
def test_retention_sweep_honors_days(_dbpool) -> None:
    """Suite 7 — purge_pipeline_log_older_than(90) deletes rows >90 days old,
    leaves rows <90 days."""
    _seed_tenant(_dbpool, CANARY_TENANT_A)
    run_id_old = uuid4()
    run_id_new = uuid4()
    # Insert old + new rows under service role with manual created_at.
    with _dbpool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_log (run_id, tenant_id, event_type, severity, component, payload, created_at)
            VALUES (%s, NULL, 'canary_test', 'info', 'test', '{"age":"old"}'::jsonb, now() - INTERVAL '91 days')
            """,
            (str(run_id_old),),
        )
        cur.execute(
            """
            INSERT INTO pipeline_log (run_id, tenant_id, event_type, severity, component, payload, created_at)
            VALUES (%s, NULL, 'canary_test', 'info', 'test', '{"age":"new"}'::jsonb, now() - INTERVAL '1 day')
            """,
            (str(run_id_new),),
        )

    deleted = purge_pipeline_log_older_than(90)
    assert deleted >= 1

    with _dbpool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM pipeline_log WHERE run_id = %s", (str(run_id_old),))
        assert cur.fetchone()["c"] == 0
        cur.execute("SELECT COUNT(*) AS c FROM pipeline_log WHERE run_id = %s", (str(run_id_new),))
        assert cur.fetchone()["c"] == 1
