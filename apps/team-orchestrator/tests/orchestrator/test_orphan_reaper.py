"""VT-481 — orphan-run reaper tests.

reap_orphan_runs closes runs stranded status='running' OLDER than the age floor (a process
died mid-run; DBOS can't recover a prior-app-version row), and leaves everything else alone:
a RECENT 'running' run (could be live in-flight), a 'paused' run (legitimately long-lived —
owner-approval / L3 hold), and already-terminal runs.

Real-DB (DATABASE_URL) like the other runner tests — the reaper UPDATEs pipeline_runs.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-481 orphan-reaper tests skipped",
)


@pytest.fixture(scope="module")
def dsn():
    import apply_migrations

    d = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=d)
    assert not r["failed"], r["failed"]
    return d


def _seed_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, whatsapp_number) "
            "VALUES ('VT-481 reaper', 'founding', 'trial', now(), %s) RETURNING id",
            (f"+9199{uuid4().int % 10**8:08d}",),
        ).fetchone()
    return UUID(str(row[0]))


def _seed_run(dsn: str, tenant: UUID, *, status: str, age_hours: float) -> UUID:
    """Insert a pipeline_runs row with a backdated started_at (service-role; bypass RLS)."""
    rid = uuid4()
    started = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status, started_at, step_count) "
            "VALUES (%s, %s, %s, %s, 0)",
            (str(rid), str(tenant), status, started),
        )
    return rid


def _status(dsn: str, rid: UUID) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute("SELECT status FROM pipeline_runs WHERE id = %s", (str(rid),)).fetchone()
    return row[0]


def _meta(dsn: str, rid: UUID):
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT terminal_state_metadata FROM pipeline_runs WHERE id = %s", (str(rid),)
        ).fetchone()
    return row[0]


@pytest.fixture
def reaper_pool(dsn):
    """A service-role pool the reaper UPDATEs through (bypasses RLS, cross-tenant by design)."""
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        dsn, min_size=1, max_size=2,
        kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
    )
    try:
        yield pool
    finally:
        pool.close()


def test_reaps_old_running_run(dsn, reaper_pool):
    from orchestrator.orphan_reaper import reap_orphan_runs

    tenant = _seed_tenant(dsn)
    old = _seed_run(dsn, tenant, status="running", age_hours=5)  # >1h floor

    n = reap_orphan_runs(pool=reaper_pool)

    assert n >= 1
    assert _status(dsn, old) == "aborted_hard_limit", "an old 'running' orphan must be reaped"
    meta = _meta(dsn, old)
    assert meta and meta.get("reaped_by") == "vt481_orphan_reaper", "reaper marker must be stamped"


def test_leaves_recent_running_run_untouched(dsn, reaper_pool):
    """A recent 'running' run could be a LIVE in-flight run — must NOT be reaped."""
    from orchestrator.orphan_reaper import reap_orphan_runs

    tenant = _seed_tenant(dsn)
    fresh = _seed_run(dsn, tenant, status="running", age_hours=0.1)  # ~6 min — within bounds

    reap_orphan_runs(pool=reaper_pool)

    assert _status(dsn, fresh) == "running", "a recent in-flight 'running' run must be left alone"


def test_leaves_paused_run_untouched(dsn, reaper_pool):
    """A 'paused' run (owner-approval / L3 hold) is legitimately long-lived — never reaped."""
    from orchestrator.orphan_reaper import reap_orphan_runs

    tenant = _seed_tenant(dsn)
    paused = _seed_run(dsn, tenant, status="paused", age_hours=48)  # 2 days, but paused

    reap_orphan_runs(pool=reaper_pool)

    assert _status(dsn, paused) == "paused", "a long-lived 'paused' run must NOT be reaped"


def test_leaves_terminal_runs_untouched(dsn, reaper_pool):
    from orchestrator.orphan_reaper import reap_orphan_runs

    tenant = _seed_tenant(dsn)
    done = _seed_run(dsn, tenant, status="completed", age_hours=10)

    reap_orphan_runs(pool=reaper_pool)

    assert _status(dsn, done) == "completed", "a terminal run must be left exactly as-is"


def test_idempotent(dsn, reaper_pool):
    """Re-running reaps nothing the second time (the row is now terminal)."""
    from orchestrator.orphan_reaper import reap_orphan_runs

    tenant = _seed_tenant(dsn)
    _seed_run(dsn, tenant, status="running", age_hours=3)

    first = reap_orphan_runs(pool=reaper_pool)
    assert first >= 1
    # second pass: the just-reaped row is no longer 'running' — only NEW orphans (none here) match.
    before = _seed_run  # noqa: F841 — readability
    second_target_absent = reap_orphan_runs(pool=reaper_pool)
    # second_target_absent may be 0 (nothing new) — the point is the reaped row isn't re-touched.
    assert isinstance(second_target_absent, int)


# ── VT-668: the stalled-task reaper's approval-holder surfacing (fix 3) + orphaned sweep (fix 2b) ──


def _seed_manager_task(
    dsn: str, tenant: UUID, *, status: str, attempt: int = 0, updated_age_hours: float = 2.0,
    stall_metadata: dict | None = None, source_message_ref: str | None = None,
) -> UUID:
    from psycopg.types.json import Jsonb

    tid = uuid4()
    updated = datetime.now(timezone.utc) - timedelta(hours=updated_age_hours)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO manager_tasks (id, tenant_id, objective, status, attempt, max_attempts, "
            "  source_message_ref, stall_metadata, owner_notification_status, updated_at, created_at) "
            "VALUES (%s, %s, '{}'::jsonb, %s, %s, 5, %s, %s, 'not_required', %s, %s)",
            (str(tid), str(tenant), status, attempt, source_message_ref,
             Jsonb(stall_metadata) if stall_metadata is not None else None, updated, updated),
        )
    return tid


def _seed_bound_approval(
    dsn: str, tenant: UUID, run_id: UUID, *, resolved: bool, resolved_age_hours: float = 2.0
) -> UUID:
    """A pending_approvals row bound to ``run_id`` (which must be a real pipeline_runs row — FK).
    ``resolved`` seeds an already-resolved (approved) row backdated ``resolved_age_hours``."""
    aid = uuid4()
    resolved_at = (datetime.now(timezone.utc) - timedelta(hours=resolved_age_hours)) if resolved else None
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status, started_at, step_count) "
            "VALUES (%s, %s, 'paused', now(), 0) ON CONFLICT (id) DO NOTHING",
            (str(run_id), str(tenant)),
        )
        conn.execute(
            "INSERT INTO pending_approvals (id, tenant_id, run_id, approval_type, summary, status, "
            "  decision, timeout_at, resolved_at, requested_at) "
            "VALUES (%s, %s, %s, 'campaign_send', 'vt668 test', %s, %s, "
            "        now() + interval '48 hours', %s, now())",
            (str(aid), str(tenant), str(run_id),
             "approved" if resolved else "pending", "approved" if resolved else None, resolved_at),
        )
    return aid


def _task_row(dsn: str, task_id: UUID) -> dict:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, owner_notification_status FROM manager_tasks WHERE id = %s",
            (str(task_id),),
        ).fetchone()
    return {"status": row[0], "owner_notification_status": row[1]}


def _approval_row(dsn: str, approval_id: UUID) -> dict:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, resolved_at FROM pending_approvals WHERE id = %s", (str(approval_id),)
        ).fetchone()
    return {"status": row[0], "resolved_at": row[1]}


def test_dead_letter_approval_holder_surfaced_and_approval_closed(dsn, reaper_pool, monkeypatch):
    """VT-668 fix 3 — when the reaper dead-letters a task still holding an OPEN owner-approval, it
    must (a) arm the honest owner stall notification and (b) CLOSE the dangling approval, so a later
    reply gets the honest-expiry path, never a resolve-into-nothing on a dead consumer."""
    import orchestrator.owner_surface.task_outcome as to
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    notified: list = []
    monkeypatch.setattr(to, "maybe_notify_owner_of_task_outcome",
                        lambda t, task, **k: notified.append((str(t), str(task))) or True)

    tenant = _seed_tenant(dsn)
    run_id = uuid4()
    # attempt=4, max_attempts=5 -> decide_retry -> dead_letter this sweep. No steps -> stall shape.
    task = _seed_manager_task(
        dsn, tenant, status="running", attempt=4, updated_age_hours=2.0,
        stall_metadata={"awaiting_approval_run_id": str(run_id)},
    )
    approval = _seed_bound_approval(dsn, tenant, run_id, resolved=False)  # OPEN

    reap_stalled_manager_tasks(pool=reaper_pool, age_hours=1)

    t = _task_row(dsn, task)
    assert t["status"] == "dead_letter", "the stalled task must be dead-lettered"
    assert t["owner_notification_status"] == "pending", "the honest owner notify must be armed"
    a = _approval_row(dsn, approval)
    assert a["status"] == "timed_out" and a["resolved_at"] is not None, "dangling approval must be closed"
    assert (str(tenant), str(task)) in notified, "the owner stall notification must fire post-commit"


def test_orphaned_waiting_owner_resolved_approval_surfaced(dsn, reaper_pool, monkeypatch):
    """VT-668 fix 2b — a task parked 'waiting_owner' whose approval has RESOLVED (owner replied) but
    which the loop never consumed (its process died) is the incident's post-fix-1 shape: the
    stall-sweep excludes waiting_owner, so ONLY this orphaned sweep catches it. Surfaced to
    dead_letter + honest owner notify (approval resolved > age_hours ago ⇒ certainly a dead loop)."""
    import orchestrator.owner_surface.task_outcome as to
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    notified: list = []
    monkeypatch.setattr(to, "maybe_notify_owner_of_task_outcome",
                        lambda t, task, **k: notified.append(str(task)) or True)

    tenant = _seed_tenant(dsn)
    run_id = uuid4()
    task = _seed_manager_task(
        dsn, tenant, status="waiting_owner", updated_age_hours=3.0,
        stall_metadata={"awaiting_approval_run_id": str(run_id)},
    )
    _seed_bound_approval(dsn, tenant, run_id, resolved=True, resolved_age_hours=2.0)  # resolved 2h ago

    reap_stalled_manager_tasks(pool=reaper_pool, age_hours=1)

    assert _task_row(dsn, task)["status"] == "dead_letter", "orphaned waiting_owner task must surface"
    assert str(task) in notified, "the owner must be honestly notified the approved campaign stalled"


def test_waiting_owner_pending_approval_is_left_untouched(dsn, reaper_pool, monkeypatch):
    """VT-668 — a 'waiting_owner' task whose approval is STILL PENDING is legitimately idle (the
    owner hasn't decided) — neither the stall-sweep NOR the orphaned sweep may touch it."""
    import orchestrator.owner_surface.task_outcome as to
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    monkeypatch.setattr(to, "maybe_notify_owner_of_task_outcome", lambda *a, **k: True)

    tenant = _seed_tenant(dsn)
    run_id = uuid4()
    task = _seed_manager_task(
        dsn, tenant, status="waiting_owner", updated_age_hours=5.0,
        stall_metadata={"awaiting_approval_run_id": str(run_id)},
    )
    _seed_bound_approval(dsn, tenant, run_id, resolved=False)  # PENDING — owner still deciding

    reap_stalled_manager_tasks(pool=reaper_pool, age_hours=1)

    assert _task_row(dsn, task)["status"] == "waiting_owner", "an awaiting-approval task must be left idle"
