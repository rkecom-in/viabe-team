"""VT-605 (Loop Package 2) — the executable plan store (live Postgres, RLS-enforced).

Proves the Package 2 acceptance criteria verbatim:
  - Duplicate inbound events create ONE task and ONE plan (SID idempotency).
  - A plan survives process restart and resumes at the same step (restart = a fresh
    ``load_plan``/``claim_next_step`` call against durable state — no in-memory anything).
  - CAS prevents stale workers from advancing or regressing a task (``claim_next_step`` /
    ``complete_step`` / ``revise_plan``'s ``expected_plan_revision``).
  - A revised plan preserves prior step history (superseded PENDING steps only; a terminal step
    from the old revision is untouched).
  - Admission control: one active objective-bearing task per tenant; a later objective queues.

Mirrors ``test_task_store.py`` / ``test_task_producer.py``'s fixture (module-scoped pool via a
direct ``ConnectionPool``, not the full DBOS launch — lighter weight, same RLS enforcement).
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("pydantic")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-605 plan_store tests skipped",
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
            (tid, f"ps-{tid[:8]}"),
        )
    return tid


def _simple_plan(**overrides):
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    steps = overrides.pop("steps", None) or [
        PlanStep(
            step_seq=1, kind="specialist_dispatch", specialist="sales_recovery_agent",
            situation="60d dormant cohort", desired_outcome="re-engage",
        ),
        PlanStep(step_seq=2, kind="verification", situation="check results", desired_outcome="confirm"),
    ]
    fields = {"objective": "recover dormant customers", "acceptance_criteria": ["3+ re-engaged"], "steps": steps}
    fields.update(overrides)
    return ManagerPlan(**fields)


# --- create_plan: idempotency + atomic persistence ------------------------------------------------


def test_create_plan_persists_task_and_all_steps_atomically(pool):
    from orchestrator.manager import task_store as ts
    from orchestrator.manager import plan_store

    tid = _seed_tenant(pool)
    plan = _simple_plan()
    task_id = plan_store.create_plan(tid, plan, source_message_sid=f"SM{uuid4().hex}")

    row = ts.get_task(tid, task_id)
    assert row is not None
    assert row["status"] == "planned"  # no other active task for this tenant → runs immediately
    assert row["plan_revision"] == 1
    assert row["current_step_id"] is not None

    steps = ts.get_steps(tid, task_id)
    assert len(steps) == 2
    assert [s["step_seq"] for s in steps] == [1, 2]
    assert steps[0]["specialist"] == "sales_recovery_agent"
    assert steps[0]["status"] == "pending"


def test_create_plan_duplicate_sid_creates_one_task_and_one_plan(pool):
    """Package 2 acceptance, verbatim: duplicate inbound events → ONE task, ONE plan."""
    from orchestrator.manager import plan_store

    tid = _seed_tenant(pool)
    plan = _simple_plan()
    sid = f"SM{uuid4().hex}"

    task_a = plan_store.create_plan(tid, plan, source_message_sid=sid)
    task_b = plan_store.create_plan(tid, plan, source_message_sid=sid)  # a redelivered webhook
    assert task_a == task_b

    with pool.connection() as conn:
        n_tasks = conn.execute(
            "SELECT count(*) AS c FROM manager_tasks WHERE tenant_id = %s AND idempotency_key = %s",
            (tid, sid),
        ).fetchone()
        n_steps = conn.execute(
            "SELECT count(*) AS c FROM manager_task_steps WHERE tenant_id = %s AND task_id = %s",
            (tid, str(task_a)),
        ).fetchone()
    assert (n_tasks["c"] if isinstance(n_tasks, dict) else n_tasks[0]) == 1
    assert (n_steps["c"] if isinstance(n_steps, dict) else n_steps[0]) == 2  # not doubled


def test_create_plan_admission_control_queues_second_active_task(pool):
    """Package 2: admit ONE active objective-bearing task per tenant; a later objective queues."""
    from orchestrator.manager import task_store as ts
    from orchestrator.manager import plan_store

    tid = _seed_tenant(pool)
    first = plan_store.create_plan(tid, _simple_plan(), source_message_sid=f"SM{uuid4().hex}")
    assert ts.get_task(tid, first)["status"] == "planned"

    second = plan_store.create_plan(
        tid, _simple_plan(objective="second objective"), source_message_sid=f"SM{uuid4().hex}"
    )
    assert ts.get_task(tid, second)["status"] == "queued"
    assert first != second

    # Once the first task terminates, admission control is scoped to THIS check only (VT-605 does
    # not build the queued->active promotion — that is VT-606's scheduler); a THIRD objective while
    # the first is completed should be admitted 'planned' again (not blocked by a queued sibling).
    ts.set_task_status(tid, first, "completed")
    third = plan_store.create_plan(
        tid, _simple_plan(objective="third objective"), source_message_sid=f"SM{uuid4().hex}"
    )
    assert ts.get_task(tid, third)["status"] == "planned"


def test_create_plan_admission_control_does_not_regress_legacy_multi_run_concurrency(pool):
    """The one-active-task admission rule is a plan_store-level POLICY, not a table-wide DB
    constraint — the EXISTING legacy task_producer (VT-565) mints one ephemeral task PER RUN and
    legitimately has multiple concurrently-'running' tasks per tenant (e.g. an overlapping
    scheduled cadence + a live turn); a raw multi-active insert outside plan_store must NOT be
    rejected by the schema (that would break the pre-existing orphan_reaper test suite)."""
    from orchestrator.manager import task_store as ts

    tid = _seed_tenant(pool)
    a = ts.create_task(tid, {"goal": "run A"})
    ts.set_task_status(tid, a, "running", expected_from=("clarifying",))
    b = ts.create_task(tid, {"goal": "run B"})  # a second concurrently-active task — must succeed
    ts.set_task_status(tid, b, "running", expected_from=("clarifying",))
    assert ts.get_task(tid, a)["status"] == "running"
    assert ts.get_task(tid, b)["status"] == "running"


# --- load_plan: reconstruction + restart survival -------------------------------------------------


def test_load_plan_reconstructs_objective_and_steps(pool):
    from orchestrator.manager import plan_store

    tid = _seed_tenant(pool)
    plan = _simple_plan()
    task_id = plan_store.create_plan(tid, plan, source_message_sid=f"SM{uuid4().hex}")

    loaded = plan_store.load_plan(tid, task_id)
    assert loaded is not None
    assert loaded.objective == plan.objective
    assert loaded.acceptance_criteria == plan.acceptance_criteria
    assert loaded.plan_revision == 1
    assert [s.step_seq for s in loaded.steps] == [1, 2]
    assert loaded.steps[0].specialist == "sales_recovery_agent"
    assert loaded.steps[0].situation == "60d dormant cohort"
    assert loaded.steps[1].kind == "verification"


def test_load_plan_survives_restart(pool):
    """'Restart' for a stateless store = re-instantiating the call with no carried-over Python
    state — a fresh ``load_plan`` call must reconstruct the IDENTICAL plan from durable rows only."""
    from orchestrator.manager import plan_store

    tid = _seed_tenant(pool)
    plan = _simple_plan()
    task_id = plan_store.create_plan(tid, plan, source_message_sid=f"SM{uuid4().hex}")

    first_read = plan_store.load_plan(tid, task_id)
    second_read = plan_store.load_plan(tid, task_id)  # simulates a fresh process re-reading
    assert first_read == second_read


def test_load_plan_missing_task_returns_none(pool):
    from orchestrator.manager import plan_store

    tid = _seed_tenant(pool)
    assert plan_store.load_plan(tid, uuid4()) is None


# --- claim_next_step: ordered + CAS-guarded ---------------------------------------------------


def test_claim_next_step_returns_steps_in_order_and_marks_running(pool):
    from orchestrator.manager import task_store as ts
    from orchestrator.manager import plan_store

    tid = _seed_tenant(pool)
    task_id = plan_store.create_plan(tid, _simple_plan(), source_message_sid=f"SM{uuid4().hex}")

    first = plan_store.claim_next_step(tid, task_id)
    assert first["step_seq"] == 1
    assert first["specialist"] == "sales_recovery_agent"

    steps = {s["step_seq"]: s for s in ts.get_steps(tid, task_id)}
    assert steps[1]["status"] == "running"
    assert steps[2]["status"] == "pending"  # untouched

    second = plan_store.claim_next_step(tid, task_id)
    assert second["step_seq"] == 2  # step 1 no longer pending, step 2 is next


def test_claim_next_step_no_pending_step_returns_none(pool):
    from orchestrator.manager import plan_store
    from orchestrator.manager.plan_models import PlanStep

    tid = _seed_tenant(pool)
    plan = _simple_plan(steps=[PlanStep(step_seq=1, kind="effect")])
    task_id = plan_store.create_plan(tid, plan, source_message_sid=f"SM{uuid4().hex}")

    claimed = plan_store.claim_next_step(tid, task_id)
    assert claimed is not None
    # the only step is now running — nothing left to claim
    assert plan_store.claim_next_step(tid, task_id) is None


def test_claim_next_step_missing_task_returns_none(pool):
    from orchestrator.manager import plan_store

    tid = _seed_tenant(pool)
    assert plan_store.claim_next_step(tid, uuid4()) is None


# --- complete_step: CAS never regresses --------------------------------------------------------


def test_complete_step_cas_applies_once_then_no_ops(pool):
    from orchestrator.manager import task_store as ts
    from orchestrator.manager import plan_store
    from orchestrator.manager.plan_models import EvidenceRef

    tid = _seed_tenant(pool)
    task_id = plan_store.create_plan(tid, _simple_plan(), source_message_sid=f"SM{uuid4().hex}")
    step = plan_store.claim_next_step(tid, task_id)

    applied = plan_store.complete_step(
        tid, step["step_id"], "done", evidence=EvidenceRef(kind="pipeline_run", ref=str(uuid4()))
    )
    assert applied is True

    stale = plan_store.complete_step(tid, step["step_id"], "done")
    assert stale is False  # CAS no-op — already terminal, suppressed not raised

    steps = {s["step_seq"]: s for s in ts.get_steps(tid, task_id)}
    assert steps[1]["status"] == "done"
    assert steps[1]["evidence_kind"] == "pipeline_run"


def test_complete_step_stale_worker_cannot_regress_a_skipped_step(pool):
    from orchestrator.manager import task_store as ts
    from orchestrator.manager import plan_store

    tid = _seed_tenant(pool)
    task_id = plan_store.create_plan(tid, _simple_plan(), source_message_sid=f"SM{uuid4().hex}")
    step = plan_store.claim_next_step(tid, task_id)
    ts.set_step_status(tid, step["step_id"], "skipped", expected_from=("running",))

    # a stale worker (still believes the step is 'running') tries to complete it 'done'
    regressed = plan_store.complete_step(tid, step["step_id"], "done", expected_from=("running",))
    assert regressed is False
    assert ts.get_steps(tid, task_id)[0]["status"] == "skipped"


# --- revise_plan: supersede-not-edit + CAS ------------------------------------------------------


def test_revise_plan_supersedes_pending_and_preserves_terminal_history(pool):
    """Package 2 acceptance, verbatim: a revised plan preserves prior step history."""
    from orchestrator.manager import plan_store
    from orchestrator.manager.plan_models import EvidenceRef, PlanStep

    tid = _seed_tenant(pool)
    task_id = plan_store.create_plan(tid, _simple_plan(), source_message_sid=f"SM{uuid4().hex}")

    # step 1 completes for real — this is HISTORY that must survive the revision untouched.
    step1 = plan_store.claim_next_step(tid, task_id)
    plan_store.complete_step(
        tid, step1["step_id"], "done", evidence=EvidenceRef(kind="pipeline_run", ref=str(uuid4()))
    )

    new_plan = _simple_plan(
        objective="recover dormant customers (revised)",
        steps=[PlanStep(step_seq=1, kind="specialist_dispatch", specialist="sales_recovery_agent",
                        situation="revised", desired_outcome="re-engage more")],
    )
    revised = plan_store.revise_plan(tid, task_id, new_plan, expected_plan_revision=1)
    assert revised.plan_revision == 2

    # the CURRENT plan (load_plan) reflects ONLY the new revision's steps.
    current = plan_store.load_plan(tid, task_id)
    assert current.plan_revision == 2
    assert len(current.steps) == 1
    assert current.objective == "recover dormant customers (revised)"

    # but the OLD revision's rows are still in the DB: step 1 (done) untouched, step 2
    # (was pending) now superseded — real history, never edited or deleted.
    with pool.connection() as conn:
        old_rows = {
            r["step_seq"]: r["status"]
            for r in conn.execute(
                "SELECT step_seq, status FROM manager_task_steps "
                "WHERE tenant_id = %s AND task_id = %s AND plan_revision = 1",
                (tid, str(task_id)),
            ).fetchall()
        }
    assert old_rows[1] == "done"  # untouched — real history
    assert old_rows[2] == "superseded"  # orphaned by the revision, never deleted


def test_revise_plan_cas_conflict_on_stale_expected_revision(pool):
    """CAS prevents a stale worker from advancing/regressing a task via a revision built against
    an out-of-date plan_revision."""
    from orchestrator.manager import plan_store
    from orchestrator.manager.plan_models import PlanStep

    tid = _seed_tenant(pool)
    task_id = plan_store.create_plan(tid, _simple_plan(), source_message_sid=f"SM{uuid4().hex}")

    new_plan = _simple_plan(steps=[PlanStep(step_seq=1, kind="effect")])
    plan_store.revise_plan(tid, task_id, new_plan, expected_plan_revision=1)  # -> revision 2

    # a second (stale) worker still thinks the plan is at revision 1
    with pytest.raises(plan_store.PlanRevisionConflict) as exc_info:
        plan_store.revise_plan(tid, task_id, new_plan, expected_plan_revision=1)
    assert exc_info.value.expected == 1
    assert exc_info.value.actual == 2

    # the task's plan_revision is still 2 — the stale attempt changed NOTHING.
    current = plan_store.load_plan(tid, task_id)
    assert current.plan_revision == 2


def test_revise_plan_missing_task_raises(pool):
    from orchestrator.manager import plan_store
    from orchestrator.manager.plan_models import PlanStep

    tid = _seed_tenant(pool)
    with pytest.raises(ValueError):
        plan_store.revise_plan(
            tid, uuid4(), _simple_plan(steps=[PlanStep(step_seq=1, kind="effect")]),
            expected_plan_revision=1,
        )


# --- tenant isolation (RLS) still holds on the new plan-store surface --------------------------


def test_plan_store_tenant_isolation(pool):
    from orchestrator.manager import plan_store

    tid_a = _seed_tenant(pool)
    tid_b = _seed_tenant(pool)
    task_a = plan_store.create_plan(tid_a, _simple_plan(), source_message_sid=f"SM{uuid4().hex}")

    assert plan_store.load_plan(tid_b, task_a) is None  # RLS hides A's task from B
    assert plan_store.claim_next_step(tid_b, task_a) is None
