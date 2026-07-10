"""VT-611 Phase B1 #1 — the manager_task state-machine transition matrix (live Postgres + DBOS).

The promotion-gate ask: "every LEGAL and ILLEGAL manager_task + step transition (task_store CAS
setters: TERMINAL_OUTCOMES, OWNER_NOTIFICATION_STATUSES, status flips). Assert illegal transitions
are rejected (CAS expected_from mismatch), legal ones commit."

This file does NOT re-prove the happy-path outcome branches — ``test_manager_review_db.py`` and
``test_workflow.py`` already cover those exhaustively (30 tests walking every decision branch
through the real DBOS loop). What's missing, and what this file adds, is the OTHER half of a state
machine: proof that a transition attempted from the WRONG predecessor state is safely rejected, not
silently applied — the exact shape of a duplicate/stale DBOS step-retry landing on a task that has
already moved on since the step last ran. Each of ``manager/workflow.py``'s CAS-guarded private step
functions is called DIRECTLY (not through the full loop) against a task deliberately forced into a
wrong predecessor state, so the CAS guard itself — not the loop's own ordering discipline, which
would never normally call these out of turn — is what's under test.

Real transition catalog this file is built from (cataloged by reading every ``task_store.
set_task_status``/``set_step_status`` call site in ``manager/plan_store.py`` + ``manager/review.py``
+ ``manager/workflow.py``):
  - ``plan_store.create_plan``:            INSERT -> planned | queued | shadow
  - ``plan_store.claim_next_step``:        {planned, running} -> running  (FAIL-CLOSED: raises,
                                            not a silent no-op — the one exception to the CAS-log-
                                            and-suppress convention every other setter uses)
  - ``review.manager_review`` (live path): running -> verifying | waiting_owner | blocked
                                            (blocked also reachable from ANY TASK_NON_TERMINAL)
  - ``workflow._resume_step_after_answer``:      waiting -> pending (step); waiting_owner -> running (task)
  - ``workflow._settle_verified_task``:          verifying -> completed (TERMINAL)
  - ``workflow._settle_declined_approval``:      verifying -> cancelled (TERMINAL)
  - ``workflow._append_verification_retry_step``: verifying -> running
  - ``workflow._resume_task_after_needs_changes``: verifying -> running
  - ``workflow._block_*``:                       ANY TASK_NON_TERMINAL -> blocked
  - ``queue_promotion.promote_next_queued_task``: queued -> planned (only when no active task)

Two states in ``TASK_STATUSES``/``OWNER_NOTIFICATION_STATUSES`` have NO live writer anywhere in the
codebase today (confirmed by exhaustive grep, not assumed): task status ``'clarifying'`` (every
live creator — ``plan_store.create_plan``, ``task_producer.create_task`` — inserts directly into
``planned``/``queued``/``shadow``, never ``clarifying``) and ``owner_notification_status``
``'not_required'``/``'accepted'`` (only ``'pending'``/``'delivered'``/``'failed'`` are ever written,
by ``workflow.py``'s settle steps + ``task_outcome.py``'s composer). Both are noted, not tested —
there is no real transition to pin, and inventing one would test a strawman, not the system.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-611 state-machine matrix tests skipped",
)


@pytest.fixture(scope="module")
def substrate():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "test-salt")

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        from orchestrator.graph import get_pool

        yield get_pool()
    finally:
        shutdown_dbos()


def _seed_tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"sm-{tid[:8]}"),
        )
    return tid


def _create_task(tenant_id: str, *, steps=None):
    from orchestrator.manager import plan_store
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    steps = steps or [PlanStep(step_seq=1, kind="verification")]
    plan = ManagerPlan(objective="test objective", steps=steps)
    return plan_store.create_plan(tenant_id, plan, source_message_sid=f"SM{uuid4().hex}")


def _force_task_status(pool, tenant_id: str, task_id, status: str) -> None:
    """Directly overwrite a task's status column, bypassing every CAS guard — the harness's way
    of putting a task into a deliberately-WRONG predecessor state. Real code never does this; it
    simulates "a task somehow reached state X" so the CAS guard's OWN rejection is what's under
    test, not how the task got there."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE manager_tasks SET status = %s WHERE tenant_id = %s AND id = %s",
            (status, tenant_id, str(task_id)),
        )


def _force_step_status(pool, tenant_id: str, step_id, status: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE manager_task_steps SET status = %s WHERE tenant_id = %s AND id = %s",
            (status, tenant_id, str(step_id)),
        )


# ---------------------------------------------------------------------------
# claim_next_step — the ONE fail-CLOSED transition (raises, never a silent no-op)
# ---------------------------------------------------------------------------


def test_claim_next_step_raises_and_rolls_back_when_task_is_not_claimable(substrate):
    """``plan_store.claim_next_step``'s task-level guard only accepts {'planned','running'}
    predecessors — every OTHER CAS setter in this codebase logs + suppresses a stale write, but
    this one raises (a claim against a blocked/queued/terminal task is a caller bug, never a
    legitimate path, per the code's own comment). Force the task to 'blocked' and assert BOTH the
    raise AND that the step claim rolled back with it inside the same transaction — never a step
    silently left 'running' under a task that never actually transitioned."""
    from orchestrator.manager import plan_store
    from orchestrator.manager import task_store as ts

    pool = substrate
    tid = _seed_tenant(pool)
    task_id = _create_task(tid)
    _force_task_status(pool, tid, task_id, "blocked")

    with pytest.raises(RuntimeError, match="not in a claimable state"):
        plan_store.claim_next_step(tid, task_id)

    steps = ts.get_steps(tid, task_id)
    assert steps[0]["status"] == "pending"  # rolled back, not silently claimed
    assert ts.get_task(tid, task_id)["status"] == "blocked"  # untouched


# ---------------------------------------------------------------------------
# workflow.py step functions — direct-call legal commit + illegal (wrong predecessor) reject pairs
# ---------------------------------------------------------------------------


def test_settle_verified_task_commits_from_verifying(substrate, monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store as ts

    pool = substrate
    tid = _seed_tenant(pool)
    task_id = str(_create_task(tid))
    _force_task_status(pool, tid, task_id, "verifying")
    monkeypatch.setattr(
        "orchestrator.manager.verification.resolve_terminal_outcome",
        lambda tenant_id, task_id, steps: "completed_no_action",
    )

    wf._settle_verified_task(tid, task_id)

    task = ts.get_task(tid, task_id)
    assert task["status"] == "completed"
    assert task["terminal_outcome"] == "completed_no_action"
    assert task["owner_notification_status"] == "pending"


def test_settle_verified_task_rejected_when_task_is_not_verifying(
    substrate, monkeypatch: pytest.MonkeyPatch
):
    """A stale/duplicate call (e.g. a DBOS step-retry re-running this step after the task ALREADY
    settled via some other path, or got blocked) must never regress/overwrite the task's actual
    current state."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store as ts

    pool = substrate
    tid = _seed_tenant(pool)
    task_id = str(_create_task(tid))
    _force_task_status(pool, tid, task_id, "blocked")
    monkeypatch.setattr(
        "orchestrator.manager.verification.resolve_terminal_outcome",
        lambda tenant_id, task_id, steps: "completed_no_action",
    )

    wf._settle_verified_task(tid, task_id)

    task = ts.get_task(tid, task_id)
    assert task["status"] == "blocked"  # unchanged — the CAS guard rejected the stale settle
    assert task["terminal_outcome"] is None
    assert task["owner_notification_status"] == "not_required"  # column default, never touched


def test_settle_declined_approval_commits_from_verifying(substrate):
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store as ts

    pool = substrate
    tid = _seed_tenant(pool)
    task_id = str(_create_task(tid))
    _force_task_status(pool, tid, task_id, "verifying")

    wf._settle_declined_approval(tid, task_id)

    task = ts.get_task(tid, task_id)
    assert task["status"] == "cancelled"
    assert task["terminal_outcome"] == "cancelled"
    assert task["owner_notification_status"] == "pending"


def test_settle_declined_approval_rejected_when_task_is_not_verifying(substrate):
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store as ts

    pool = substrate
    tid = _seed_tenant(pool)
    task_id = str(_create_task(tid))
    _force_task_status(pool, tid, task_id, "completed")  # already a DIFFERENT terminal

    wf._settle_declined_approval(tid, task_id)

    task = ts.get_task(tid, task_id)
    assert task["status"] == "completed"  # unchanged — never regressed to 'cancelled'
    assert task["terminal_outcome"] is None


def test_resume_task_after_needs_changes_commits_from_verifying(substrate):
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store as ts

    pool = substrate
    tid = _seed_tenant(pool)
    task_id = str(_create_task(tid))
    _force_task_status(pool, tid, task_id, "verifying")

    wf._resume_task_after_needs_changes(tid, task_id)

    assert ts.get_task(tid, task_id)["status"] == "running"


def test_resume_task_after_needs_changes_rejected_when_task_is_not_verifying(substrate):
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store as ts

    pool = substrate
    tid = _seed_tenant(pool)
    task_id = str(_create_task(tid))
    _force_task_status(pool, tid, task_id, "waiting_owner")

    wf._resume_task_after_needs_changes(tid, task_id)

    assert ts.get_task(tid, task_id)["status"] == "waiting_owner"  # unchanged


def test_resume_step_after_answer_commits_from_waiting(substrate):
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store as ts

    pool = substrate
    tid = _seed_tenant(pool)
    task_id = str(_create_task(tid))
    step_id = str(ts.get_steps(tid, task_id)[0]["id"])
    _force_task_status(pool, tid, task_id, "waiting_owner")
    _force_step_status(pool, tid, step_id, "waiting")

    wf._resume_step_after_answer(tid, task_id, step_id)

    assert ts.get_steps(tid, task_id)[0]["status"] == "pending"
    assert ts.get_task(tid, task_id)["status"] == "running"


def test_resume_step_after_answer_step_level_rejected_when_step_already_terminal(substrate):
    """A step already 'done' (a different resume path beat this one to it, or a duplicate
    correlate_reply resumed the loop twice) must never be regressed back to 'pending'."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store as ts

    pool = substrate
    tid = _seed_tenant(pool)
    task_id = str(_create_task(tid))
    step_id = str(ts.get_steps(tid, task_id)[0]["id"])
    _force_task_status(pool, tid, task_id, "waiting_owner")
    _force_step_status(pool, tid, step_id, "done")

    wf._resume_step_after_answer(tid, task_id, step_id)

    assert ts.get_steps(tid, task_id)[0]["status"] == "done"  # unchanged


def test_resume_step_after_answer_task_level_rejected_when_task_already_moved_on(substrate):
    """The task itself moved on (e.g. an unrelated 'blocked' while the answer was in flight) — the
    step-level flip may still apply (it has its own independent CAS guard), but the task must NOT
    be force-marched back to 'running' out from under whatever else happened to it."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store as ts

    pool = substrate
    tid = _seed_tenant(pool)
    task_id = str(_create_task(tid))
    step_id = str(ts.get_steps(tid, task_id)[0]["id"])
    _force_task_status(pool, tid, task_id, "blocked")
    _force_step_status(pool, tid, step_id, "waiting")

    wf._resume_step_after_answer(tid, task_id, step_id)

    assert ts.get_task(tid, task_id)["status"] == "blocked"  # unchanged


def test_append_verification_retry_step_transitions_task_from_verifying(substrate):
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store as ts

    pool = substrate
    tid = _seed_tenant(pool)
    task_id = str(_create_task(tid))
    _force_task_status(pool, tid, task_id, "verifying")

    applied = wf._append_verification_retry_step(tid, task_id, reason="gap found")

    assert applied is True
    assert ts.get_task(tid, task_id)["status"] == "running"


def test_append_verification_retry_step_task_status_rejected_when_not_verifying(substrate):
    """VT-611 matrix finding (observation, not a fix in this row — team-lead scoped B1 to TESTS):
    ``_append_verification_retry_step`` appends its replacement step UNCONDITIONALLY before
    attempting the task-status CAS — so a stale call still grows the plan by one step even though
    the task-status transition itself is correctly rejected. The task's OWN status is never
    incorrectly forced to 'running', which is the invariant this test pins; the extra-step
    side effect on a stale call is a separate, low-severity finding (no owner-facing effect, no
    duplicate send) noted for a future row, not fixed here."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store as ts

    pool = substrate
    tid = _seed_tenant(pool)
    task_id = str(_create_task(tid))
    _force_task_status(pool, tid, task_id, "blocked")

    wf._append_verification_retry_step(tid, task_id, reason="gap found")

    assert ts.get_task(tid, task_id)["status"] == "blocked"  # the task-level guard held


# ---------------------------------------------------------------------------
# OWNER_NOTIFICATION_STATUSES — real-DB CAS (task_outcome.py's tests mock this; this proves the
# actual column CAS against a real row)
# ---------------------------------------------------------------------------


def test_owner_notification_status_pending_to_delivered_commits(substrate):
    from orchestrator.manager import task_store as ts

    pool = substrate
    tid = _seed_tenant(pool)
    task_id = str(_create_task(tid))
    assert ts.get_task(tid, task_id)["owner_notification_status"] == "not_required"
    # the column defaults 'not_required'; a real settle sets it 'pending' explicitly (see
    # _settle_verified_task above) — seed that starting point directly here.
    ts.set_task_status(tid, task_id, "verifying", expected_from=("planned", "running"))
    ts.set_task_status(
        tid, task_id, "completed", expected_from=("verifying",),
        terminal_outcome="completed_no_action", owner_notification_status="pending",
    )

    applied = ts.set_owner_notification_status(
        tid, task_id, "delivered", expected_from=("pending",)
    )

    assert applied is True
    assert ts.get_task(tid, task_id)["owner_notification_status"] == "delivered"


def test_owner_notification_status_rejected_once_already_delivered(substrate):
    """A second flip attempt against an already-'delivered' row (a crash-replay landing AFTER a
    prior attempt's flip already committed, or a duplicate notify call) must be rejected — never
    silently overwritten to 'failed' by a stale second attempt racing behind a successful one."""
    from orchestrator.manager import task_store as ts

    pool = substrate
    tid = _seed_tenant(pool)
    task_id = str(_create_task(tid))
    ts.set_task_status(tid, task_id, "verifying", expected_from=("planned", "running"))
    ts.set_task_status(
        tid, task_id, "completed", expected_from=("verifying",),
        terminal_outcome="completed_no_action", owner_notification_status="pending",
    )
    assert ts.set_owner_notification_status(tid, task_id, "delivered", expected_from=("pending",))

    applied = ts.set_owner_notification_status(tid, task_id, "failed", expected_from=("pending",))

    assert applied is False
    assert ts.get_task(tid, task_id)["owner_notification_status"] == "delivered"  # unchanged


# ---------------------------------------------------------------------------
# Invalid enum values — fail closed before any write (pure; no seeded task needed, the checks in
# task_store.set_task_status run BEFORE the connection opens)
# ---------------------------------------------------------------------------


def test_set_task_status_rejects_unknown_status_value(substrate):
    from orchestrator.manager import task_store as ts

    with pytest.raises(ValueError, match="unknown task status"):
        ts.set_task_status(uuid4(), uuid4(), "not_a_real_status")


def test_set_task_status_rejects_unknown_expected_from_value(substrate):
    from orchestrator.manager import task_store as ts

    with pytest.raises(ValueError, match="unknown expected_from"):
        ts.set_task_status(uuid4(), uuid4(), "running", expected_from=("not_a_real_status",))


def test_set_task_status_rejects_unknown_terminal_outcome_value(substrate):
    from orchestrator.manager import task_store as ts

    with pytest.raises(ValueError, match="unknown terminal_outcome"):
        ts.set_task_status(uuid4(), uuid4(), "completed", terminal_outcome="not_a_real_outcome")


def test_set_task_status_rejects_unknown_owner_notification_status_value(substrate):
    from orchestrator.manager import task_store as ts

    with pytest.raises(ValueError, match="unknown owner_notification_status"):
        ts.set_task_status(
            uuid4(), uuid4(), "completed", owner_notification_status="not_a_real_value"
        )


def test_set_owner_notification_status_rejects_unknown_value(substrate):
    from orchestrator.manager import task_store as ts

    with pytest.raises(ValueError, match="unknown owner_notification_status"):
        ts.set_owner_notification_status(uuid4(), uuid4(), "not_a_real_value")
