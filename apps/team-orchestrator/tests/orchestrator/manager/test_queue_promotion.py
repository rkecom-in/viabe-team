"""VT-606 — ``queue_promotion.promote_next_queued_task`` (live Postgres).

VT-605's own report flagged the dequeue side as unbuilt; this is that missing half, tested
standalone (independent of ``manager_task_workflow``'s own wiring, tested separately).
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — queue_promotion tests skipped",
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
            (tid, f"qp-{tid[:8]}"),
        )
    return tid


def _simple_plan(objective: str):
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    return ManagerPlan(objective=objective, steps=[PlanStep(step_seq=1, kind="verification")])


def test_promotes_oldest_queued_when_no_active_task(pool):
    from orchestrator.manager import plan_store, queue_promotion, task_store

    tid = _seed_tenant(pool)
    first = plan_store.create_plan(tid, _simple_plan("first"), source_message_sid=f"SM{uuid4().hex}")
    second = plan_store.create_plan(tid, _simple_plan("second"), source_message_sid=f"SM{uuid4().hex}")
    third = plan_store.create_plan(tid, _simple_plan("third"), source_message_sid=f"SM{uuid4().hex}")
    assert task_store.get_task(tid, second)["status"] == "queued"
    assert task_store.get_task(tid, third)["status"] == "queued"

    task_store.set_task_status(tid, first, "completed", expected_from=("planned",))
    promoted = queue_promotion.promote_next_queued_task(tid)

    assert promoted == second  # OLDEST queued first, not third
    assert task_store.get_task(tid, second)["status"] == "planned"
    assert task_store.get_task(tid, third)["status"] == "queued"  # untouched


def test_returns_none_when_still_active(pool):
    from orchestrator.manager import plan_store, queue_promotion, task_store

    tid = _seed_tenant(pool)
    first = plan_store.create_plan(tid, _simple_plan("first"), source_message_sid=f"SM{uuid4().hex}")
    second = plan_store.create_plan(tid, _simple_plan("second"), source_message_sid=f"SM{uuid4().hex}")
    assert task_store.get_task(tid, first)["status"] == "planned"  # still active

    promoted = queue_promotion.promote_next_queued_task(tid)

    assert promoted is None
    assert task_store.get_task(tid, second)["status"] == "queued"  # unchanged


def test_returns_none_when_queue_empty(pool):
    from orchestrator.manager import plan_store, queue_promotion, task_store

    tid = _seed_tenant(pool)
    only = plan_store.create_plan(tid, _simple_plan("only"), source_message_sid=f"SM{uuid4().hex}")
    task_store.set_task_status(tid, only, "completed", expected_from=("planned",))

    assert queue_promotion.promote_next_queued_task(tid) is None


def test_tenant_isolation(pool):
    from orchestrator.manager import plan_store, queue_promotion, task_store

    tid_a = _seed_tenant(pool)
    tid_b = _seed_tenant(pool)
    plan_store.create_plan(tid_a, _simple_plan("a-active"), source_message_sid=f"SM{uuid4().hex}")
    b_active = plan_store.create_plan(tid_b, _simple_plan("b-first"), source_message_sid=f"SM{uuid4().hex}")
    b_queued = plan_store.create_plan(tid_b, _simple_plan("b-second"), source_message_sid=f"SM{uuid4().hex}")
    assert task_store.get_task(tid_b, b_queued)["status"] == "queued"
    task_store.set_task_status(tid_b, b_active, "completed", expected_from=("planned",))

    # tenant A still active — promoting A must not touch B's queue.
    assert queue_promotion.promote_next_queued_task(tid_a) is None
    promoted_b = queue_promotion.promote_next_queued_task(tid_b)
    assert promoted_b == b_queued
