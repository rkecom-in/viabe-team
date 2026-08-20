"""VT-736 — a permanently-blocked task must RELEASE the tenant's one active slot (live Postgres).

The wedge these pin: every `_block_*` path sets status='blocked' + terminal_outcome='escalated' and
arms NO next_retry_at. `blocked` is in TASK_ACTIVE so the row kept the slot; the workflow tail only
promoted the queue for TASK_TERMINAL and `blocked` is not terminal, so everything queued behind it
starved; and the reaper's ladder only wakes blocked rows whose next_retry_at has elapsed. Five dev
tenants sat wedged this way, the oldest for a month, each still being told "already in progress".

Each test below states the failure it prevents, because the whole class was invisible in aggregate:
a wedged tenant looks exactly like a quiet one.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-736 slot-release tests skipped",
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
            (tid, f"vt736-{tid[:8]}"),
        )
    return tid


def _status(pool, task_id) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT status FROM manager_tasks WHERE id = %s", (str(task_id),)
        ).fetchone()
    return row["status"]


def _block(pool, task_id, *, next_retry_at=None) -> None:
    """Reproduce what a `_block_*` path leaves behind: blocked + escalated, retry armed or not."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE manager_tasks SET status = 'blocked', terminal_outcome = 'escalated', "
            "       owner_notification_status = 'pending', next_retry_at = %s WHERE id = %s",
            (next_retry_at, str(task_id)),
        )


def test_escalated_block_settles_to_dead_letter(pool):
    """The core release. Left blocked, this row holds the slot forever."""
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task_id = ts.create_task(tid, {"goal": "wedge me"})
    _block(pool, task_id)

    assert ts.settle_unretryable_block(tid, task_id) is True
    assert _status(pool, task_id) == "dead_letter"


def test_settlement_frees_the_active_slot(pool):
    """The actual damage was the held slot, not the status string — assert the slot itself."""
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task_id = ts.create_task(tid, {"goal": "wedge me"})
    _block(pool, task_id)
    assert ts.has_active_task(tid) is True, "precondition: the block holds the slot"

    ts.settle_unretryable_block(tid, task_id)
    assert ts.has_active_task(tid) is False


def test_a_task_awaiting_retry_is_NOT_settled(pool):
    """A blocked row WITH next_retry_at is mid-ladder, not wedged. Settling it would cancel a
    retry the reaper is about to run — the one way this fix could cause data loss."""
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task_id = ts.create_task(tid, {"goal": "retry pending"})
    _block(pool, task_id, next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=5))

    assert ts.settle_unretryable_block(tid, task_id) is False
    assert _status(pool, task_id) == "blocked"


def test_settlement_is_idempotent(pool):
    """Workflow steps replay; a second settle must be a no-op, not an error or a second write."""
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task_id = ts.create_task(tid, {"goal": "wedge me"})
    _block(pool, task_id)

    assert ts.settle_unretryable_block(tid, task_id) is True
    assert ts.settle_unretryable_block(tid, task_id) is False
    assert _status(pool, task_id) == "dead_letter"


def test_a_running_task_is_never_settled(pool):
    """CAS is on status='blocked'. A live task must be untouchable by this path."""
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task_id = ts.create_task(tid, {"goal": "still working"})
    with pool.connection() as conn:
        conn.execute("UPDATE manager_tasks SET status = 'running' WHERE id = %s", (str(task_id),))

    assert ts.settle_unretryable_block(tid, task_id) is False
    assert _status(pool, task_id) == "running"


def test_terminal_outcome_survives_settlement(pool):
    """Ops must still see WHY it stopped. dead_letter + escalated is the reaper's own orphan shape."""
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task_id = ts.create_task(tid, {"goal": "wedge me"})
    _block(pool, task_id)
    ts.settle_unretryable_block(tid, task_id)

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT terminal_outcome, completed_at FROM manager_tasks WHERE id = %s",
            (str(task_id),),
        ).fetchone()
    assert row["terminal_outcome"] == "escalated"
    assert row["completed_at"] is not None


def test_settled_task_is_operator_redrivable(pool):
    """dead_letter was chosen over any new status precisely to keep redrive working."""
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task_id = ts.create_task(tid, {"goal": "wedge me"})
    _block(pool, task_id)
    ts.settle_unretryable_block(tid, task_id)

    with pool.connection() as conn:
        assert ts.redrive_task(tid, task_id, conn=conn) is True
    assert _status(pool, task_id) == "planned"


def test_a_queued_task_can_run_after_the_wedge_clears(pool):
    """The starvation half: a task queued behind the wedge could never be admitted. Once the slot
    is free, promotion has something to promote — this is the owner-visible half of the fix."""
    from orchestrator.manager import queue_promotion, task_store as ts

    tid = _seed_tenant(pool)
    wedged = ts.create_task(tid, {"goal": "wedge me"})
    _block(pool, wedged)

    queued = ts.create_task(tid, {"goal": "the owner's next ask"})
    with pool.connection() as conn:
        conn.execute("UPDATE manager_tasks SET status = 'queued' WHERE id = %s", (str(queued),))

    # Precondition: while the wedge holds the slot, nothing can be promoted.
    assert queue_promotion.promote_next_queued_task(tid) is None

    ts.settle_unretryable_block(tid, wedged)
    promoted = queue_promotion.promote_next_queued_task(tid)
    assert promoted is not None and str(promoted) == str(queued)
    assert _status(pool, queued) == "planned"
