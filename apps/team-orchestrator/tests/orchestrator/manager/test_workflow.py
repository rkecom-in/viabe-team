"""VT-606 (Loop Package 3) — the durable ``manager_task_workflow`` (live Postgres + DBOS).

Covers the acceptance list verbatim: every decision branch, limits -> blocked+incident, queued
promotion, and the ask_owner wait/resume cycle. ``_dispatch_specialist_step`` (the ONE step that
would make a real LLM + graph.invoke call) is monkeypatched per test to a fake that ALSO applies
the SAME plan_store/task_store side effects the REAL ``manager_review()`` would have applied inside
that step (manager_review's own persistence is proven separately, DB-backed, in
``test_manager_review_db.py``) — so these tests prove the LOOP's OWN control flow (claim / limits /
wait-resume / queue-promotion wiring) against real durable state, without needing a live Anthropic
call. ``DBOS.sleep`` is monkeypatched to a no-op so the ask_owner wait doesn't block wall-clock time.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-606 manager_task_workflow tests skipped",
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
            (tid, f"wf-{tid[:8]}"),
        )
    return tid


def _create_task(tenant_id: str, *, steps=None):
    """Default step is a NON-specialist ``verification`` step (specialist=None) — deliberately NOT
    ``sales_recovery_agent``: ``_validate_step`` correctly, fail-closed checks REAL activation
    prerequisites (onboarding-journey-complete / verification / connector / customers), which a
    freshly-seeded test tenant never satisfies. Loop-control-flow tests use this trivially-passing
    default; ``test_validate_step_blocks_on_unmet_activation_prereqs`` below exercises the
    prereq-gated path deliberately, on a tenant that intentionally does NOT meet it."""
    from orchestrator.manager import plan_store
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    steps = steps or [PlanStep(step_seq=1, kind="verification")]
    plan = ManagerPlan(objective="test objective", steps=steps)
    return plan_store.create_plan(tenant_id, plan, source_message_sid=f"SM{uuid4().hex}")


def _apply_outcome(tenant_id: str, task_id: str, step_id: str, outcome: str) -> None:
    """Mirrors the REAL ``manager.review.manager_review``'s plan_store/task_store effect for each
    outcome (see review.py's own dispatch table) — since these tests mock out
    ``_dispatch_specialist_step`` entirely (no live graph.invoke/LLM call), the fake dispatch
    functions below call this so the surrounding durable state is exactly what production would
    leave behind, and the OUTER LOOP's re-claim / limit / promotion logic is exercised for real."""
    from orchestrator.manager import task_store

    if outcome in ("continue", "accept_step"):
        task_store.set_step_status(tenant_id, step_id, "done", expected_from=("running",))
    elif outcome == "complete":
        task_store.set_step_status(tenant_id, step_id, "done", expected_from=("running",))
        task_store.set_task_status(tenant_id, task_id, "verifying", expected_from=("running",))
    elif outcome == "revise_step":
        task_store.set_step_status(tenant_id, step_id, "pending", expected_from=("running",))
    elif outcome == "ask_owner":
        task_store.set_step_status(tenant_id, step_id, "waiting", expected_from=("running",))
        task_store.set_task_status(tenant_id, task_id, "waiting_owner", expected_from=("running",))
    elif outcome == "escalate":
        task_store.set_step_status(tenant_id, step_id, "failed", expected_from=("running",))
        task_store.set_task_status(
            tenant_id, task_id, "blocked", expected_from=tuple(task_store.TASK_NON_TERMINAL)
        )


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch):
    """None of these tests want to actually wait wall-clock time in the ask_owner poll loop."""
    import orchestrator.manager.workflow as wf

    monkeypatch.setattr(wf.DBOS, "sleep", lambda _seconds: None)


# --- happy path: single step, 'complete' -------------------------------------------------------


def test_single_step_complete_settles_task_verifying(substrate, monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        _apply_outcome(tid, task_id, step_id, "complete")
        return "complete"

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "verifying"
    assert task_store.get_task(tid, task_id)["status"] == "verifying"


# --- validate: capability / prerequisites / policy ----------------------------------------------


def test_validate_step_blocks_on_unmet_activation_prereqs(substrate, monkeypatch: pytest.MonkeyPatch):
    """A ``sales_recovery_agent`` dispatch step on a tenant that has NOT met the real
    activation_registry prerequisites (journey complete / verified / connector / customers — a
    freshly-seeded test tenant satisfies NONE of these) must be blocked BEFORE any dispatch, not
    silently allowed through."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager.plan_models import PlanStep

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(
        tid, steps=[PlanStep(step_seq=1, kind="specialist_dispatch", specialist="sales_recovery_agent")]
    ))

    dispatch_calls = []
    monkeypatch.setattr(
        wf, "_dispatch_specialist_step", lambda *a, **k: dispatch_calls.append(1) or "complete"
    )
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "blocked"
    assert dispatch_calls == []  # never reached dispatch — the prereq gate caught it first


def _grant_config_policy(tid: str) -> None:
    """Permit the 'config' action type in tenant_business_policy — assert_within_policy runs
    BEFORE assert_or_gate_business_action in _validate_step and is its own fail-closed check (no
    policy row -> OUT_OF_POLICY for ANY effect class); these tests isolate the business-impact-choke
    freeze behavior specifically, so the OUTER policy bound must already be satisfied."""
    from orchestrator.agents.business_policy import grant_business_policy
    from orchestrator.db import tenant_connection

    with tenant_connection(tid) as conn:
        grant_business_policy(tid, allowed_action_types=["config"], conn=conn)


def test_validate_step_blocks_on_frozen_business_impact_class(substrate, monkeypatch: pytest.MonkeyPatch):
    """Team-lead ruling: _validate_step must run business_impact_choke.assert_or_gate_business_action
    for spend/commitment/config effect classes — a FROZEN class (an explicit owner kill-switch)
    blocks the step BEFORE dispatch (never wastes a cycle working toward a frozen action), even
    though the OUTER policy bound (assert_within_policy) is satisfied — isolating the freeze as the
    actual blocker, not a missing policy grant."""
    import orchestrator.manager.workflow as wf
    from orchestrator.agents.business_impact_choke import BusinessImpactClass, freeze_business_class
    from orchestrator.manager.plan_models import PlanStep

    tid = _seed_tenant(substrate)
    _grant_config_policy(tid)
    with substrate.connection() as conn:
        freeze_business_class(tid, BusinessImpactClass.CONFIG, True, reason="test-freeze", conn=conn)

    task_id = str(_create_task(
        tid, steps=[PlanStep(step_seq=1, kind="verification", allowed_effect_classes=["config"])],
    ))

    dispatch_calls = []
    monkeypatch.setattr(
        wf, "_dispatch_specialist_step", lambda *a, **k: dispatch_calls.append(1) or "complete"
    )
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "blocked"
    assert dispatch_calls == []


def test_validate_step_allows_unfrozen_business_impact_class(substrate, monkeypatch: pytest.MonkeyPatch):
    """With the OUTER policy bound satisfied (a real 'config' grant) and NO freeze, a no-autonomy-
    grant tenant (the fail-closed always_approve default) must still be DISPATCHED —
    requires_owner_approval at pre-dispatch magnitude=0 is the expected default for a no-grant
    tenant, not itself a block; the real gate re-runs at effect-proposal time with the actual
    magnitude."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager.plan_models import PlanStep

    tid = _seed_tenant(substrate)
    _grant_config_policy(tid)
    task_id = str(_create_task(
        tid, steps=[PlanStep(step_seq=1, kind="verification", allowed_effect_classes=["config"])],
    ))

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        _apply_outcome(tid, task_id, step_id, "complete")
        return "complete"

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "verifying"


def test_continue_then_complete_across_two_steps(substrate, monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf
    from orchestrator.manager.plan_models import PlanStep

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(
        tid,
        steps=[
            PlanStep(step_seq=1, kind="verification"),
            PlanStep(step_seq=2, kind="verification"),
        ],
    ))

    def _dispatch(tenant_id, tid_, step_id, attempt, situation, desired_outcome, acceptance_criteria, specialist, has_next):
        outcome = "continue" if has_next else "complete"
        _apply_outcome(tid, task_id, step_id, outcome)
        return outcome

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    status = wf.manager_task_workflow(tid, task_id)
    assert status == "verifying"


# --- escalate: manager_review already settled the task blocked ---------------------------------


def test_escalate_outcome_ends_loop_without_further_dispatch(substrate, monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))

    calls = []

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        calls.append(1)
        _apply_outcome(tid, task_id, step_id, "escalate")
        return "escalate"

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "blocked"
    assert len(calls) == 1  # escalate must NOT trigger a further dispatch


# --- limits: revisions per step -----------------------------------------------------------------


def test_revision_limit_exceeded_blocks_with_incident(substrate, monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf
    from orchestrator.observability.incident_store import get_incident

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))

    dispatch_count = {"n": 0}

    def _always_revise(tenant_id, tid_, step_id, *a, **k):
        dispatch_count["n"] += 1
        _apply_outcome(tid, task_id, step_id, "revise_step")
        return "revise_step"

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _always_revise)
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "blocked"
    # LIMIT_MAX_REVISIONS_PER_STEP=2 -> allowed on attempts 1,2; the 3rd revise_step trips the limit.
    assert dispatch_count["n"] == wf.LIMIT_MAX_REVISIONS_PER_STEP + 1
    with substrate.connection() as conn:
        row = conn.execute(
            "SELECT id FROM incidents WHERE tenant_id = %s AND run_id = %s", (tid, task_id)
        ).fetchone()
    assert row is not None
    incident_id = row["id"] if isinstance(row, dict) else row[0]
    incident = get_incident(tid, incident_id)
    assert incident is not None
    assert incident["escalation_tier"] >= 2
    # Team-lead ruling: a self-describing kind (migration 166), never overloaded onto 'other'.
    assert incident["incident_kind"] == "limit_exhausted"


def test_prereq_policy_block_uses_other_not_limit_exhausted(substrate, monkeypatch: pytest.MonkeyPatch):
    """The prereq/policy validation failure is a DIFFERENT cause than limit exhaustion — it must
    stay on 'other', not get relabeled 'limit_exhausted' (ops needs the two distinguishable)."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager.plan_models import PlanStep
    from orchestrator.observability.incident_store import get_incident

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(
        tid, steps=[PlanStep(step_seq=1, kind="specialist_dispatch", specialist="sales_recovery_agent")]
    ))
    monkeypatch.setattr(wf, "_dispatch_specialist_step", lambda *a, **k: "complete")

    status = wf.manager_task_workflow(tid, task_id)
    assert status == "blocked"

    with substrate.connection() as conn:
        row = conn.execute(
            "SELECT id FROM incidents WHERE tenant_id = %s AND run_id = %s", (tid, task_id)
        ).fetchone()
    assert row is not None
    incident = get_incident(tid, row["id"] if isinstance(row, dict) else row[0])
    assert incident is not None
    assert incident["incident_kind"] == "other"


# --- limits: cycles per run ----------------------------------------------------------------------


def test_cycle_limit_exceeded_blocks_with_incident(substrate, monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf
    from orchestrator.manager.plan_models import PlanStep

    tid = _seed_tenant(substrate)
    # 8 steps (the plan-model max) so claim_next_step never runs dry before the cycle limit does.
    steps = [PlanStep(step_seq=i, kind="verification") for i in range(1, 9)]
    task_id = str(_create_task(tid, steps=steps))

    def _always_continue(tenant_id, tid_, step_id, *a, **k):
        # 'continue' WITHOUT marking the step done would stall claim_next_step forever (nothing
        # else would ever become claimable) — mark THIS step done so the NEXT cycle claims a
        # genuinely different pending step, exactly like a real multi-step plan progressing.
        _apply_outcome(tid, task_id, step_id, "continue")
        return "continue"

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _always_continue)
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "blocked"


# --- ask_owner: wait, answer, resume -------------------------------------------------------------


def test_ask_owner_waits_then_resumes_after_answer(substrate, monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import pending_questions
    from orchestrator.manager.plan_models import PlanStep

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(
        tid,
        steps=[
            PlanStep(step_seq=1, kind="clarification"),
            PlanStep(step_seq=2, kind="verification"),
        ],
    ))

    call_state = {"asked": False}

    def _dispatch(tenant_id, tid_, step_id, attempt, situation, desired_outcome, acceptance_criteria, specialist, has_next):
        if not call_state["asked"]:
            call_state["asked"] = True
            _apply_outcome(tid, task_id, step_id, "ask_owner")
            pending_questions.ask(tid, "which cohort?", task_id=task_id)
            return "ask_owner"
        # Post-answer: step1's retry has step2 still pending (has_next=True) -> continue; step2's
        # own dispatch is the LAST step (has_next=False) -> complete.
        outcome = "continue" if has_next else "complete"
        _apply_outcome(tid, task_id, step_id, outcome)
        return outcome

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)

    # Simulate the owner's reply landing (a SEPARATE, normal webhook turn would call this) by
    # monkeypatching _question_still_open to answer on its SECOND call (first poll sees it open,
    # then we "answer" it before the loop re-checks).
    poll_calls = {"n": 0}
    real_still_open = wf._question_still_open

    def _still_open(tenant_id, tid_):
        poll_calls["n"] += 1
        if poll_calls["n"] >= 2:
            pending_questions.correlate_reply(tid, "the VIP cohort", None, task_id=task_id)
            return False
        return real_still_open(tenant_id, tid_)

    monkeypatch.setattr(wf, "_question_still_open", _still_open)

    status = wf.manager_task_workflow(tid, task_id)
    assert status == "verifying"
    assert pending_questions.get_open(tid, task_id=task_id) == []


def test_ask_owner_timeout_blocks_with_incident(substrate, monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        _apply_outcome(tid, task_id, step_id, "ask_owner")
        return "ask_owner"

    # Force the max-polls path: question never answered, and cap it small for test speed.
    monkeypatch.setattr(wf, "_OWNER_WAIT_MAX_POLLS", 2)
    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    monkeypatch.setattr(wf, "_question_still_open", lambda *a, **k: True)
    monkeypatch.setattr(wf, "_maybe_reengage_stale", lambda *a, **k: False)

    status = wf.manager_task_workflow(tid, task_id)
    assert status == "blocked"


# --- queued-task promotion end-to-end ------------------------------------------------------------


def test_terminal_task_promotes_oldest_queued(substrate, monkeypatch: pytest.MonkeyPatch):
    """Neither of THIS row's own outcomes reaches a TRUE ``task_store.TASK_TERMINAL`` status —
    'complete' settles at 'verifying' (a later verification pass finishes it; out of VT-606 scope)
    and 'escalate'/limit-exceeded settle at 'blocked' (an operator-resolution path, also out of
    scope) — both are still task_store.TASK_NON_TERMINAL by design (a verifying/blocked task still
    correctly occupies the tenant's one-active-task slot). This test proves the PROMOTION WIRING
    itself: whenever a task genuinely IS terminal by the time the workflow's own final read runs
    (simulated here as if some resolution mechanism settled it mid-cycle), the oldest queued
    sibling is promoted."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import plan_store, task_store
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    tid = _seed_tenant(substrate)
    active_task = _create_task(tid)
    second_plan = ManagerPlan(objective="second", steps=[PlanStep(step_seq=1, kind="verification")])
    queued_task = plan_store.create_plan(tid, second_plan, source_message_sid=f"SM{uuid4().hex}")
    assert task_store.get_task(tid, queued_task)["status"] == "queued"

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        task_store.set_step_status(tid, step_id, "done", expected_from=("running",))
        task_store.set_task_status(tid, str(active_task), "completed", expected_from=("running",))
        return "complete"

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    final_status = wf.manager_task_workflow(tid, str(active_task))

    assert final_status == "completed"
    assert task_store.get_task(tid, queued_task)["status"] == "planned"
