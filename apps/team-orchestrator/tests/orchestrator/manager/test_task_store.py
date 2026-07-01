"""VT-525 (B2) — manager task/step store + stalled-task reaper (live Postgres, RLS-enforced).

Proves the persistence spine: create/mint (redelivery-safe), ordered steps, the CAS guard that
never regresses a terminal state, PII redaction at write, tenant isolation via RLS, and the
orphan detector that flips a stalled active task (no runnable step) to blocked.
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — manager task_store tests skipped",
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


def _seed_tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"tm-{tid[:8]}"),
        )
    return tid


def test_create_and_get(pool):
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task_id = ts.create_task(tid, {"goal": "recover lapsed cohort"})
    row = ts.get_task(tid, task_id)
    assert row is not None
    assert row["status"] == "clarifying"
    assert row["version"] == 1
    assert row["objective"]["goal"] == "recover lapsed cohort"


def test_idempotency_dedupe(pool):
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    key = f"msg-{uuid4().hex}"
    a = ts.create_task(tid, {"goal": "x"}, idempotency_key=key)
    b = ts.create_task(tid, {"goal": "y — redelivery"}, idempotency_key=key)
    assert a == b  # same source event → one task, ever
    with pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) AS c FROM manager_tasks WHERE tenant_id = %s AND idempotency_key = %s",
            (tid, key),
        ).fetchone()
    assert (n["c"] if isinstance(n, dict) else n[0]) == 1


def test_status_advances_and_versions(pool):
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task_id = ts.create_task(tid, {"goal": "x"})
    assert ts.set_task_status(tid, task_id, "planned", expected_from=("clarifying",)) is True
    assert ts.set_task_status(tid, task_id, "running", expected_from=("planned",)) is True
    row = ts.get_task(tid, task_id)
    assert row["status"] == "running"
    assert row["version"] == 3  # create=1, +planned, +running


def test_cas_never_regresses_terminal(pool):
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task_id = ts.create_task(tid, {"goal": "x"})
    ts.set_task_status(tid, task_id, "completed")  # unconditional terminal
    # a stale writer tries to move it back to running, guarded on non-terminal states
    applied = ts.set_task_status(
        tid, task_id, "running", expected_from=tuple(ts.TASK_NON_TERMINAL)
    )
    assert applied is False
    assert ts.get_task(tid, task_id)["status"] == "completed"


def test_add_step_and_step_cas(pool):
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task_id = ts.create_task(tid, {"goal": "x"})
    step_id = ts.add_step(tid, task_id, 1, "specialist_dispatch",
                          detail={"situation": "cohort went quiet"})
    assert ts.set_step_status(tid, step_id, "running", expected_from=("pending",)) is True
    assert ts.set_step_status(
        tid, step_id, "done", expected_from=("running",),
        evidence_kind="agent_work_item", evidence_ref="wi-123",
    ) is True
    # already terminal → a re-run to 'running' is a CAS no-op
    assert ts.set_step_status(tid, step_id, "running",
                              expected_from=tuple(ts.STEP_NON_TERMINAL)) is False
    steps = ts.get_steps(tid, task_id)
    assert len(steps) == 1
    assert steps[0]["status"] == "done"
    assert steps[0]["evidence_kind"] == "agent_work_item"
    assert steps[0]["evidence_ref"] == "wi-123"


def test_evidence_append(pool):
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task_id = ts.create_task(tid, {"goal": "x"})
    ts.set_task_status(tid, task_id, "completed",
                       evidence_entry={"kind": "campaign_plan", "ref": "cp-9"})
    refs = ts.get_task(tid, task_id)["evidence_refs"]
    assert any(e.get("ref") == "cp-9" for e in refs)


def test_redaction_at_write(pool):
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task_id = ts.create_task(tid, {"note": "ring the owner on +919876543210 today"})
    blob = json.dumps(ts.get_task(tid, task_id)["objective"])
    assert "9876543210" not in blob  # phone redacted before it hit the row


def test_tenant_isolation(pool):
    from orchestrator.manager import task_store as ts

    tid_a = _seed_tenant(pool)
    tid_b = _seed_tenant(pool)
    task_a = ts.create_task(tid_a, {"goal": "A only"})
    # tenant B, reading through RLS, cannot see A's task
    assert ts.get_task(tid_b, task_a) is None


def test_stalled_task_reaper(pool):
    from orchestrator.manager import task_store as ts
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    tid = _seed_tenant(pool)
    # stalled: running, no runnable step, backdated past the 1h floor
    stalled = ts.create_task(tid, {"goal": "stalled"})
    ts.set_task_status(tid, stalled, "running", expected_from=("clarifying",))
    # healthy: running WITH a pending step, also backdated — must NOT be reaped
    healthy = ts.create_task(tid, {"goal": "healthy"})
    ts.set_task_status(tid, healthy, "running", expected_from=("clarifying",))
    ts.add_step(tid, healthy, 1, "specialist_dispatch")  # a runnable step
    with pool.connection() as conn:
        conn.execute(
            "UPDATE manager_tasks SET updated_at = now() - interval '2 hours' WHERE id = ANY(%s)",
            ([str(stalled), str(healthy)],),
        )

    reaped = reap_stalled_manager_tasks(pool=pool)
    assert reaped >= 1
    assert ts.get_task(tid, stalled)["status"] == "blocked"
    assert ts.get_task(tid, stalled)["stall_metadata"]["reaped_reason"] == "no_runnable_step"
    assert ts.get_task(tid, healthy)["status"] == "running"  # protected by its pending step
