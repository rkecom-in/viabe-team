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


def test_get_most_recent_task(pool):
    """R7 — the bare-status fallback read: the most-recently-updated task, with the fields
    status_query renders an honest line from. None when the tenant has no task."""
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    assert ts.get_most_recent_task(tid) is None  # no task yet

    first = ts.create_task(tid, {"goal": "older"})
    second = ts.create_task(tid, {"goal": "newer"})
    # Advance `second` so it is the most-recently-updated (create order alone isn't relied on).
    ts.set_task_status(tid, second, "planned", expected_from=("clarifying",))

    row = ts.get_most_recent_task(tid)
    assert row is not None
    assert row["id"] == second
    assert row["status"] == "planned"
    # The render fields are present (terminal_outcome / owner_notification_status default unset/None).
    assert "terminal_outcome" in row and "owner_notification_status" in row
    assert row["objective"]["goal"] == "newer"
    assert first != second


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


# --- has_active_task (VT-606 round-3, used by the triage seam) ---------------------------------


def test_has_active_task_false_when_no_tasks_at_all(pool):
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    assert ts.has_active_task(tid) is False


def test_has_active_task_true_for_running(pool):
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    ts.create_task(tid, {"goal": "x"}, status="running")
    assert ts.has_active_task(tid) is True


def test_has_active_task_false_for_queued(pool):
    """'queued' is deliberately excluded from TASK_ACTIVE — a queued sibling doesn't itself hold
    the admission slot; the ACTIVE task ahead of it does."""
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    ts.create_task(tid, {"goal": "x"}, status="queued")
    assert ts.has_active_task(tid) is False


def test_has_active_task_false_for_shadow(pool):
    """VT-606 round-3 fix: 'shadow' is excluded from TASK_ACTIVE — a shadow-mode plan must never
    occupy the tenant's one-active-task admission slot."""
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    ts.create_task(tid, {"goal": "x"}, status="shadow")
    assert ts.has_active_task(tid) is False


def test_has_active_task_false_for_terminal_status(pool):
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    ts.create_task(tid, {"goal": "x"}, status="completed")
    assert ts.has_active_task(tid) is False


def test_has_active_task_tenant_isolation(pool):
    from orchestrator.manager import task_store as ts

    tid_a = _seed_tenant(pool)
    tid_b = _seed_tenant(pool)
    ts.create_task(tid_a, {"goal": "A's active task"}, status="running")

    assert ts.has_active_task(tid_a) is True
    assert ts.has_active_task(tid_b) is False


# --- has_active_integration_step (VT-608 ruling 1, the runner-gate DEFER check) -----------------


def test_has_active_integration_step_false_when_no_tasks(pool):
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    assert ts.has_active_integration_step(tid) is False


def test_has_active_integration_step_true_when_current_step_is_integration_agent(pool):
    from orchestrator.manager import plan_store
    from orchestrator.manager import task_store as ts
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep
    from uuid import uuid4

    tid = _seed_tenant(pool)
    plan = ManagerPlan(
        objective="connect a data source",
        steps=[PlanStep(step_seq=1, kind="specialist_dispatch", specialist="integration_agent")],
    )
    task_id = plan_store.create_plan(tid, plan, source_message_sid=f"SM{uuid4().hex}")
    plan_store.claim_next_step(tid, task_id)  # sets manager_tasks.current_step_id + status='running'

    assert ts.has_active_integration_step(tid) is True


def test_has_active_integration_step_false_for_a_different_specialists_current_step(pool):
    """A running task whose CURRENT step targets a DIFFERENT specialist (e.g. sales_recovery_agent)
    must not defer the runner gate — only integration_agent ownership does."""
    from orchestrator.manager import plan_store
    from orchestrator.manager import task_store as ts
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep
    from uuid import uuid4

    tid = _seed_tenant(pool)
    plan = ManagerPlan(
        objective="recover dormant customers",
        steps=[PlanStep(step_seq=1, kind="specialist_dispatch", specialist="sales_recovery_agent")],
    )
    task_id = plan_store.create_plan(tid, plan, source_message_sid=f"SM{uuid4().hex}")
    plan_store.claim_next_step(tid, task_id)

    assert ts.has_active_integration_step(tid) is False


def test_has_active_integration_step_false_when_task_is_queued(pool):
    """A SECOND tenant task queued behind an active one, even if ITS current step targets
    integration_agent, must not defer — 'queued' is excluded from TASK_ACTIVE (it isn't
    RUNNING yet, so it doesn't actually own the tenant's phase-state writes)."""
    from orchestrator.manager import plan_store
    from orchestrator.manager import task_store as ts
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep
    from uuid import uuid4

    tid = _seed_tenant(pool)
    active_plan = ManagerPlan(
        objective="active", steps=[PlanStep(step_seq=1, kind="verification")]
    )
    plan_store.create_plan(tid, active_plan, source_message_sid=f"SM{uuid4().hex}")

    queued_plan = ManagerPlan(
        objective="connect a data source",
        steps=[PlanStep(step_seq=1, kind="specialist_dispatch", specialist="integration_agent")],
    )
    queued_task_id = plan_store.create_plan(tid, queued_plan, source_message_sid=f"SM{uuid4().hex}")
    assert ts.get_task(tid, queued_task_id)["status"] == "queued"

    assert ts.has_active_integration_step(tid) is False


def test_has_active_integration_step_tenant_isolation(pool):
    from orchestrator.manager import plan_store
    from orchestrator.manager import task_store as ts
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep
    from uuid import uuid4

    tid_a = _seed_tenant(pool)
    tid_b = _seed_tenant(pool)
    plan = ManagerPlan(
        objective="connect a data source",
        steps=[PlanStep(step_seq=1, kind="specialist_dispatch", specialist="integration_agent")],
    )
    task_id = plan_store.create_plan(tid_a, plan, source_message_sid=f"SM{uuid4().hex}")
    plan_store.claim_next_step(tid_a, task_id)

    assert ts.has_active_integration_step(tid_a) is True
    assert ts.has_active_integration_step(tid_b) is False


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


def test_stalled_reaper_fires_orphaned_task_alert(pool, monkeypatch):
    """VT-529 (B6): reaping a stalled task surfaces an orphaned_task alert (ops visibility)."""
    from orchestrator.alerts import dispatch as dispatch_mod
    from orchestrator.manager import task_store as ts
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    fired: list = []
    monkeypatch.setattr(dispatch_mod, "dispatch_alert", lambda t: fired.append(t) or None)

    tid = _seed_tenant(pool)
    stalled = ts.create_task(tid, {"goal": "stalled-alert"})
    ts.set_task_status(tid, stalled, "running", expected_from=("clarifying",))
    with pool.connection() as conn:
        conn.execute(
            "UPDATE manager_tasks SET updated_at = now() - interval '2 hours' WHERE id = %s",
            (str(stalled),),
        )
    reap_stalled_manager_tasks(pool=pool)
    orphaned = [t for t in fired if t.trigger_kind == "orphaned_task"]
    assert any(t.payload.get("task_id") == str(stalled) for t in orphaned)
    assert all(t.severity == "warning" for t in orphaned)


# --- VT-557: the retry-ladder → dead_letter → operator redrive lifecycle ------------------------


def _read_retry(pool, task_id):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT status, attempt, next_retry_at FROM manager_tasks WHERE id = %s",
            (str(task_id),),
        ).fetchone()


def _stall(pool, ts, tid, *, attempt: int = 0):
    task = ts.create_task(tid, {"goal": f"vt557-{attempt}"})
    ts.set_task_status(tid, task, "running", expected_from=("clarifying",))
    with pool.connection() as conn:
        conn.execute(
            "UPDATE manager_tasks SET attempt = %s, updated_at = now() - interval '2 hours' "
            "WHERE id = %s",
            (attempt, str(task)),
        )
    return task


def test_retry_ladder_records_attempt_and_backoff(pool):
    """A first-stall task (attempt 0) → RETRY: attempt=1, next_retry_at set (backoff gate), blocked."""
    from orchestrator.manager import task_store as ts
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    tid = _seed_tenant(pool)
    task = _stall(pool, ts, tid, attempt=0)
    reap_stalled_manager_tasks(pool=pool)
    r = _read_retry(pool, task)
    assert (r["status"] if isinstance(r, dict) else r[0]) == "blocked"
    assert (r["attempt"] if isinstance(r, dict) else r[1]) == 1
    assert (r["next_retry_at"] if isinstance(r, dict) else r[2]) is not None  # backoff gate armed


def test_retry_exhaustion_dead_letters_and_alerts(pool, monkeypatch):
    """A task at attempt == max-1 → DEAD_LETTER terminal + a dead_letter_task alert (VT-557)."""
    from orchestrator.alerts import dispatch as dispatch_mod
    from orchestrator.manager import task_store as ts
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    fired: list = []
    monkeypatch.setattr(dispatch_mod, "dispatch_alert", lambda t: fired.append(t) or None)

    tid = _seed_tenant(pool)
    task = _stall(pool, ts, tid, attempt=4)  # next stall reaches max_attempts=5
    reap_stalled_manager_tasks(pool=pool)
    r = _read_retry(pool, task)
    assert (r["status"] if isinstance(r, dict) else r[0]) == "dead_letter"
    assert (r["attempt"] if isinstance(r, dict) else r[1]) == 5
    assert (r["next_retry_at"] if isinstance(r, dict) else r[2]) is None  # no further retry
    dl = [t for t in fired if t.trigger_kind == "dead_letter_task"]
    assert any(t.payload.get("task_id") == str(task) for t in dl)
    assert all(t.severity == "warning" for t in dl)


def test_next_retry_at_gate_skips_backed_off_task(pool):
    """A stalled task whose backoff has NOT elapsed is skipped by the reaper (the retry gate)."""
    from orchestrator.manager import task_store as ts
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    tid = _seed_tenant(pool)
    task = ts.create_task(tid, {"goal": "backed-off"})
    ts.set_task_status(tid, task, "running", expected_from=("clarifying",))
    with pool.connection() as conn:
        conn.execute(
            "UPDATE manager_tasks SET updated_at = now() - interval '2 hours', "
            "next_retry_at = now() + interval '1 hour' WHERE id = %s",
            (str(task),),
        )
    reap_stalled_manager_tasks(pool=pool)
    r = _read_retry(pool, task)
    assert (r["status"] if isinstance(r, dict) else r[0]) == "running"  # skipped, backoff pending


def test_redrive_resets_dead_letter_to_planned(pool):
    """Operator redrive: a dead_letter task → planned, attempt=0, next_retry_at cleared (CAS)."""
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task = ts.create_task(tid, {"goal": "redrive"})
    with pool.connection() as conn:
        conn.execute(
            "UPDATE manager_tasks SET status = 'dead_letter', attempt = 5, next_retry_at = now() "
            "WHERE id = %s",
            (str(task),),
        )
        with conn.cursor() as cur:
            applied = ts.redrive_task(tid, task, conn=cur)
    assert applied is True
    r = _read_retry(pool, task)
    assert (r["status"] if isinstance(r, dict) else r[0]) == "planned"
    assert (r["attempt"] if isinstance(r, dict) else r[1]) == 0
    assert (r["next_retry_at"] if isinstance(r, dict) else r[2]) is None


def test_redrive_noop_on_non_redrivable(pool):
    """A completed task is NOT redrivable — redrive_task is a CAS no-op (False)."""
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    task = ts.create_task(tid, {"goal": "done"})
    ts.set_task_status(tid, task, "completed", expected_from=("clarifying",))
    with pool.connection() as conn, conn.cursor() as cur:
        assert ts.redrive_task(tid, task, conn=cur) is False


# --- VT-560 (Defect 1): the retry-ladder WAKE — blocked rows re-enter the ladder -----------------


def _park_blocked(pool, ts, tid, *, attempt: int, next_retry_at_sql: str) -> str:
    """Park a task at status='blocked' with an explicit attempt + next_retry_at (SQL expr)."""
    task = ts.create_task(tid, {"goal": f"parked-{attempt}"})
    with pool.connection() as conn:
        conn.execute(
            "UPDATE manager_tasks SET status = 'blocked', attempt = %s, "
            f"next_retry_at = {next_retry_at_sql} WHERE id = %s",
            (attempt, str(task)),
        )
    return task


def test_wake_flips_due_blocked_to_planned_keeping_attempt(pool):
    """A reaper-parked 'blocked' task whose next_retry_at is DUE is woken back to 'planned',
    attempt KEPT, next_retry_at cleared, wake audit stamped (this is what VT-557 never did)."""
    from orchestrator.manager import task_store as ts
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    tid = _seed_tenant(pool)
    task = _park_blocked(pool, ts, tid, attempt=2, next_retry_at_sql="now() - interval '1 minute'")
    reap_stalled_manager_tasks(pool=pool)
    r = _read_retry(pool, task)
    assert (r["status"] if isinstance(r, dict) else r[0]) == "planned"
    assert (r["attempt"] if isinstance(r, dict) else r[1]) == 2  # attempt kept, not reset
    assert (r["next_retry_at"] if isinstance(r, dict) else r[2]) is None  # gate cleared
    meta = ts.get_task(tid, task)["stall_metadata"]
    assert meta["woken_by"] == "vt560_retry_ladder"
    assert meta["woken_reason"] == "backoff_elapsed"


def test_wake_skips_not_yet_due_blocked(pool):
    """A reaper-parked 'blocked' task whose backoff has NOT elapsed stays blocked (the gate)."""
    from orchestrator.manager import task_store as ts
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    tid = _seed_tenant(pool)
    task = _park_blocked(pool, ts, tid, attempt=1, next_retry_at_sql="now() + interval '1 hour'")
    reap_stalled_manager_tasks(pool=pool)
    r = _read_retry(pool, task)
    assert (r["status"] if isinstance(r, dict) else r[0]) == "blocked"  # backoff pending
    assert (r["attempt"] if isinstance(r, dict) else r[1]) == 1


def test_wake_skips_blocked_without_next_retry_at(pool):
    """A 'blocked' task with NO next_retry_at (a non-ladder / explicit blocker) is NEVER
    auto-woken — only reaper-parked rows (next_retry_at set) wake. Left for a human."""
    from orchestrator.manager import task_store as ts
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    tid = _seed_tenant(pool)
    task = _park_blocked(pool, ts, tid, attempt=0, next_retry_at_sql="NULL")
    reap_stalled_manager_tasks(pool=pool)
    r = _read_retry(pool, task)
    assert (r["status"] if isinstance(r, dict) else r[0]) == "blocked"  # untouched


def test_ladder_walks_to_dead_letter_across_sweeps(pool):
    """VT-560: with the wake in place the ladder actually PROGRESSES. Alternating stall+wake
    sweeps (timing fields aged as wall-clock would) walk a permanently-stalled task attempt
    1→2→3→4 and finally to dead_letter at max_attempts=5 — the path that was UNREACHABLE
    before the wake (a blocked row was never re-swept)."""
    from orchestrator.manager import task_store as ts
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    tid = _seed_tenant(pool)
    task = ts.create_task(tid, {"goal": "permanently-stalled"})
    ts.set_task_status(tid, task, "running", expected_from=("clarifying",))

    def _age_active():  # the >1h stall floor elapses on the runnable row
        with pool.connection() as conn:
            conn.execute(
                "UPDATE manager_tasks SET updated_at = now() - interval '2 hours' WHERE id = %s",
                (str(task),),
            )

    def _make_due():  # the backoff window elapses on the blocked row
        with pool.connection() as conn:
            conn.execute(
                "UPDATE manager_tasks SET next_retry_at = now() - interval '1 second' WHERE id = %s",
                (str(task),),
            )

    for expected_attempt in (1, 2, 3, 4):
        _age_active()
        reap_stalled_manager_tasks(pool=pool)  # stall sweep → blocked, attempt++
        r = _read_retry(pool, task)
        assert (r["status"] if isinstance(r, dict) else r[0]) == "blocked"
        assert (r["attempt"] if isinstance(r, dict) else r[1]) == expected_attempt
        _make_due()
        reap_stalled_manager_tasks(pool=pool)  # wake sweep → planned, attempt kept
        r = _read_retry(pool, task)
        assert (r["status"] if isinstance(r, dict) else r[0]) == "planned"
        assert (r["attempt"] if isinstance(r, dict) else r[1]) == expected_attempt

    # attempt is 4 and planned; one more aged stall reaches max_attempts=5 → dead_letter terminal
    _age_active()
    reap_stalled_manager_tasks(pool=pool)
    r = _read_retry(pool, task)
    assert (r["status"] if isinstance(r, dict) else r[0]) == "dead_letter"
    assert (r["attempt"] if isinstance(r, dict) else r[1]) == 5
    assert (r["next_retry_at"] if isinstance(r, dict) else r[2]) is None


def test_wake_does_not_double_increment_same_tick(pool):
    """VT-560: a task just blocked THIS tick (next_retry_at in the future) is not immediately
    re-woken, and a task just woken is not re-caught by the already-run stall query — attempt
    advances by exactly ONE per sweep. Drives a fresh stall and asserts a single increment."""
    from orchestrator.manager import task_store as ts
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    tid = _seed_tenant(pool)
    task = _stall(pool, ts, tid, attempt=0)  # running, no step, updated 2h ago
    reap_stalled_manager_tasks(pool=pool)
    r = _read_retry(pool, task)
    # one sweep → exactly one rung: blocked, attempt 1 (NOT 2 from a same-tick wake+re-stall)
    assert (r["status"] if isinstance(r, dict) else r[0]) == "blocked"
    assert (r["attempt"] if isinstance(r, dict) else r[1]) == 1
