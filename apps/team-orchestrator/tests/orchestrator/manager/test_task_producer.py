"""VT-565 (B2) — the LIVE producer for manager_tasks / manager_task_steps (live Postgres, RLS).

Proves the missing half of B2: nothing on a live run ever CALLED create_task/add_step, so the
VT-557/560 retry ladder + reaper + ops redrive operated on an empty table. task_producer wires the
real dispatch seams. These tests exercise the producer against the store + the actual reaper:

  * an objective-bearing dispatch (a specialist spawn) mints task ('planned' → 'running'), NO step;
  * a pure-conversational turn ('terminal' route) mints NOTHING;
  * the successful terminal completes the step + the task;
  * an owner-approval pause parks the task at 'waiting_owner' — which the stalled reaper EXCLUDES,
    so it never mis-walks to dead_letter;
  * a run that dies mid-specialist leaves an active task with no step — exactly the reaper's stall
    predicate — and the ladder walks it (retry → dead_letter) + the operator redrive re-arms it;
  * every producer write is fail-soft (a store failure never propagates to dispatch/routing).
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-565 task_producer tests skipped",
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
            (tid, f"tp-{tid[:8]}"),
        )
    return tid


def _state(tid: str, rid: str) -> dict:
    """The minimal graph state the delegate seam reads (tenant/run/trigger)."""
    return {"tenant_id": tid, "run_id": rid, "trigger_reason": "weekly_cadence"}


def _idem(rid: str) -> str:
    return f"live_dispatch:{rid}"


def _age(pool, task_id: str, *, hours: float, attempt: int | None = None) -> None:
    """Backdate updated_at past the stall floor (and optionally set attempt) — service-role."""
    with pool.connection() as conn:
        if attempt is None:
            conn.execute(
                "UPDATE manager_tasks SET updated_at = now() - make_interval(hours => %s) "
                "WHERE id = %s",
                (hours, str(task_id)),
            )
        else:
            conn.execute(
                "UPDATE manager_tasks SET updated_at = now() - make_interval(hours => %s), "
                "attempt = %s WHERE id = %s",
                (hours, attempt, str(task_id)),
            )


# --- mint on delegate / nothing on a conversational turn ----------------------------------------


def test_objective_bearing_dispatch_mints_running_task_no_step(pool):
    from orchestrator.manager import task_producer as tp
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    rid = str(uuid4())
    tp.on_route_decided(_state(tid, rid), "spawn")

    task_id = ts.find_task_id(tid, _idem(rid))
    assert task_id is not None
    row = ts.get_task(tid, task_id)
    assert row["status"] == "running"  # planned → running at the delegate seam
    assert row["assigned_function"] == "spawn"
    assert row["objective"]["route_key"] == "spawn"
    assert row["objective"]["kind"] == "specialist_dispatch"
    # THE walkability window: an active task with NO non-terminal step (nothing shields it from
    # the reaper if the process now dies).
    assert ts.get_steps(tid, task_id) == []


def test_conversational_turn_mints_nothing(pool):
    from orchestrator.manager import task_producer as tp
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    rid = str(uuid4())
    tp.on_route_decided(_state(tid, rid), "terminal")  # the no-spawn sentinel
    assert ts.find_task_id(tid, _idem(rid)) is None


def test_mint_is_idempotent_per_run(pool):
    from orchestrator.manager import task_producer as tp

    tid = _seed_tenant(pool)
    rid = str(uuid4())
    tp.on_route_decided(_state(tid, rid), "spawn")
    tp.on_route_decided(_state(tid, rid), "spawn")  # a re-fired conditional edge
    with pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) AS c FROM manager_tasks WHERE tenant_id = %s AND idempotency_key = %s",
            (tid, _idem(rid)),
        ).fetchone()
    assert (n["c"] if isinstance(n, dict) else n[0]) == 1


# --- successful terminal: the specialist return completes the step + the task -------------------


def test_run_completed_writes_done_step_and_completes_task(pool):
    from orchestrator.manager import task_producer as tp
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    rid = str(uuid4())
    tp.on_route_decided(_state(tid, rid), "spawn")
    tp.on_run_completed(tid, rid)

    task_id = ts.find_task_id(tid, _idem(rid))
    row = ts.get_task(tid, task_id)
    assert row["status"] == "completed"
    assert any(e.get("ref") == rid for e in row["evidence_refs"])  # evidence → this pipeline_run
    steps = ts.get_steps(tid, task_id)
    assert len(steps) == 1  # ONE step per delegation
    assert steps[0]["status"] == "done"
    assert steps[0]["kind"] == "specialist_dispatch"
    assert steps[0]["evidence_kind"] == "pipeline_run"
    assert steps[0]["evidence_ref"] == rid


def test_run_completed_no_task_is_clean_noop(pool):
    from orchestrator.manager import task_producer as tp
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    rid = str(uuid4())
    tp.on_run_completed(tid, rid)  # a conversational run reached its terminal — nothing to close
    assert ts.find_task_id(tid, _idem(rid)) is None


def test_run_failed_writes_failed_step_and_fails_task(pool):
    from orchestrator.manager import task_producer as tp
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    rid = str(uuid4())
    tp.on_route_decided(_state(tid, rid), "spawn")
    tp.on_run_failed(tid, rid, reason="hard_limit:tokens")

    task_id = ts.find_task_id(tid, _idem(rid))
    assert ts.get_task(tid, task_id)["status"] == "failed"
    steps = ts.get_steps(tid, task_id)
    assert len(steps) == 1
    assert steps[0]["status"] == "failed"


# --- owner-approval pause: 'waiting_owner' is NEVER reaped as stalled ----------------------------


def test_paused_task_is_waiting_owner_and_not_reaped(pool):
    from orchestrator.manager import task_producer as tp
    from orchestrator.manager import task_store as ts
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    tid = _seed_tenant(pool)
    rid = str(uuid4())
    tp.on_route_decided(_state(tid, rid), "spawn")
    tp.on_run_paused(tid, rid)

    task_id = ts.find_task_id(tid, _idem(rid))
    assert ts.get_task(tid, task_id)["status"] == "waiting_owner"
    # backdate FAR past the stall floor — a waiting_owner task must stay untouched (never walks to
    # dead_letter): the reaper scans only planned/running/verifying.
    _age(pool, task_id, hours=5)
    reap_stalled_manager_tasks(pool=pool)
    assert ts.get_task(tid, task_id)["status"] == "waiting_owner"


# --- a run that dies mid-specialist IS walkable by the VT-557/560 ladder -------------------------


def test_dead_mid_specialist_first_stall_retries(pool):
    """Delegate mints 'running' + no step; the run then DIES before any terminal seam. The reaper's
    stall predicate (active state + no runnable step) matches → first stall RETRIES: blocked +
    backoff armed + attempt incremented."""
    from orchestrator.manager import task_producer as tp
    from orchestrator.manager import task_store as ts
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    tid = _seed_tenant(pool)
    rid = str(uuid4())
    tp.on_route_decided(_state(tid, rid), "spawn")
    task_id = ts.find_task_id(tid, _idem(rid))
    assert ts.get_task(tid, task_id)["status"] == "running"
    assert ts.get_steps(tid, task_id) == []  # the death state — no step to protect it

    _age(pool, task_id, hours=2)
    reaped = reap_stalled_manager_tasks(pool=pool)
    assert reaped >= 1
    row = ts.get_task(tid, task_id)
    assert row["status"] == "blocked"
    assert row["stall_metadata"]["reaped_reason"] == "no_runnable_step"
    with pool.connection() as conn:
        r = conn.execute(
            "SELECT attempt, next_retry_at FROM manager_tasks WHERE id = %s", (str(task_id),)
        ).fetchone()
    assert (r["attempt"] if isinstance(r, dict) else r[0]) == 1
    assert (r["next_retry_at"] if isinstance(r, dict) else r[1]) is not None  # backoff gate armed


def test_dead_mid_specialist_dead_letters_at_budget_then_redrivable(pool):
    """At the retry budget, an aged stall walks the produced task to the dead_letter terminal — and
    the ops redrive re-arms it. Proves the produced rows are REAL for the whole VT-557 machinery."""
    from orchestrator.manager import task_producer as tp
    from orchestrator.manager import task_store as ts
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    tid = _seed_tenant(pool)
    rid = str(uuid4())
    tp.on_route_decided(_state(tid, rid), "spawn")
    task_id = ts.find_task_id(tid, _idem(rid))
    # one more stall at attempt==max-1 reaches max_attempts=5 → dead_letter
    _age(pool, task_id, hours=2, attempt=4)
    reap_stalled_manager_tasks(pool=pool)
    assert ts.get_task(tid, task_id)["status"] == "dead_letter"

    with pool.connection() as conn, conn.cursor() as cur:
        assert ts.redrive_task(tid, task_id, conn=cur) is True
    assert ts.get_task(tid, task_id)["status"] == "planned"


# --- fail-soft: a store failure inside the producer never propagates to dispatch/routing --------


def test_producer_mint_is_fail_soft(pool, monkeypatch):
    from orchestrator.manager import task_producer as tp
    from orchestrator.manager import task_store as ts

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ts, "create_task", boom)
    # must NOT raise — routing must continue even if the bookkeeping write fails.
    tp.on_route_decided(_state(str(uuid4()), str(uuid4())), "spawn")


def test_producer_finalize_is_fail_soft(pool, monkeypatch):
    from orchestrator.manager import task_producer as tp
    from orchestrator.manager import task_store as ts

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ts, "find_task_id", boom)
    # must NOT raise on any terminal seam.
    tp.on_run_completed(str(uuid4()), str(uuid4()))
    tp.on_run_failed(str(uuid4()), str(uuid4()), reason="x")
    tp.on_run_paused(str(uuid4()), str(uuid4()))
