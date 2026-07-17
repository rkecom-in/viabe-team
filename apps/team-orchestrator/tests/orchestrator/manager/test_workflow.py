"""VT-606 (Loop Package 3) — the durable ``manager_task_workflow`` (live Postgres + DBOS).

Covers the acceptance list verbatim: every decision branch, limits -> blocked+incident, queued
promotion, and the ask_owner wait/resume cycle. ``_dispatch_specialist_step`` (the ONE step that
would make a real LLM + graph.invoke call) is monkeypatched per test to a fake that ALSO applies
the SAME plan_store/task_store side effects the REAL ``manager_review()`` would have applied inside
that step (manager_review's own persistence is proven separately, DB-backed, in
``test_manager_review_db.py``) — so these tests prove the LOOP's OWN control flow (claim / limits /
wait-resume / queue-promotion wiring) against real durable state, without needing a live Anthropic
call. ``DBOS.sleep`` is monkeypatched to a no-op so the ask_owner wait doesn't block wall-clock time.

VT-606 round-3 fix: ``_dispatch_specialist_step`` now returns ``(outcome, revised_outcome)`` — every
fake dispatch below returns a 2-tuple (``revised_outcome=None`` unless a test is specifically
exercising MAJOR #4's revise_step-applies-the-revision fix, which has its OWN dedicated tests).
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


def _mock_verified(monkeypatch: pytest.MonkeyPatch, wf) -> None:
    """These tests exercise OTHER loop behavior (dispatch outcomes, ask_owner wait, limits) — they
    don't want to depend on the real completion-verification checkpoint's own logic (that has its
    OWN dedicated tests below). Mock it to always verify cleanly."""
    monkeypatch.setattr(wf, "_verify_completion_step", lambda tenant_id, task_id: ("verified", ""))


# --- happy path: single step, 'complete' -------------------------------------------------------


def test_single_step_complete_verifies_and_settles_completed(substrate, monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))
    _mock_verified(monkeypatch, wf)

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        _apply_outcome(tid, task_id, step_id, "complete")
        return "complete", None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "completed"
    task = task_store.get_task(tid, task_id)
    assert task["status"] == "completed"
    assert task["terminal_outcome"] == "completed_no_action"  # no step recorded evidence
    assert task["owner_notification_status"] == "pending"


def test_complete_with_evidence_settles_completed_with_effect(substrate, monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import plan_store, task_store
    from orchestrator.manager.plan_models import EvidenceRef

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))
    _mock_verified(monkeypatch, wf)

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        plan_store.complete_step(
            tid, step_id, "done",
            evidence=EvidenceRef(kind="pipeline_run", ref="pr-1"), expected_from=("running",),
        )
        task_store.set_task_status(tid, task_id, "verifying", expected_from=("running",))
        return "complete", None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "completed"
    assert task_store.get_task(tid, task_id)["terminal_outcome"] == "completed_with_effect"


# --- verification: not_verified retry cycle -------------------------------------------------------


def test_not_verified_retries_once_then_verifies(substrate, monkeypatch: pytest.MonkeyPatch):
    """The 'one revise cycle' — a not_verified completion appends a retry step (via
    plan_store.append_step) and re-dispatches; if the SECOND attempt verifies, the task settles
    completed."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))

    verdicts = iter([("not_verified", "insufficient evidence"), ("verified", "")])
    monkeypatch.setattr(wf, "_verify_completion_step", lambda tenant_id, tid_: next(verdicts))

    dispatch_calls = []

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        dispatch_calls.append(step_id)
        _apply_outcome(tid, task_id, step_id, "complete")
        return "complete", None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "completed"
    assert len(dispatch_calls) == 2  # original step + the appended retry step
    assert task_store.get_task(tid, task_id)["plan_revision"] == 2  # append_step bumped it


def test_not_verified_exhausts_budget_blocks_with_incident(substrate, monkeypatch: pytest.MonkeyPatch):
    """Two not_verified verdicts in a row (the original + the one allowed retry) exhaust the
    verification budget -> blocked + a limit_exhausted incident, never an infinite loop."""
    import orchestrator.manager.workflow as wf
    from orchestrator.observability.incident_store import get_incident

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))

    monkeypatch.setattr(
        wf, "_verify_completion_step", lambda tenant_id, tid_: ("not_verified", "still missing X")
    )

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        _apply_outcome(tid, task_id, step_id, "complete")
        return "complete", None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "blocked"
    with substrate.connection() as conn:
        row = conn.execute(
            "SELECT id FROM incidents WHERE tenant_id = %s AND run_id = %s", (tid, task_id)
        ).fetchone()
    assert row is not None
    incident = get_incident(tid, row["id"] if isinstance(row, dict) else row[0])
    assert incident is not None
    assert incident["incident_kind"] == "limit_exhausted"


def test_not_verified_at_eight_step_ceiling_blocks_immediately(substrate, monkeypatch: pytest.MonkeyPatch):
    """A plan already at PlanStep's 8-step ceiling can't append a retry step — budget-exhausted
    treatment applies immediately (no retry attempted, no crash). 7 of the 8 steps are
    pre-completed directly (not via the workflow) so reaching the ceiling doesn't itself burn
    through the 6-cycle limit."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store
    from orchestrator.manager.plan_models import PlanStep

    tid = _seed_tenant(substrate)
    steps = [PlanStep(step_seq=i, kind="verification") for i in range(1, 9)]
    task_id = str(_create_task(tid, steps=steps))
    for s in task_store.get_steps(tid, task_id)[:7]:
        task_store.set_step_status(tid, s["id"], "done", expected_from=("pending",))

    monkeypatch.setattr(
        wf, "_verify_completion_step", lambda tenant_id, tid_: ("not_verified", "gap")
    )

    def _dispatch(tenant_id, tid_, step_id, attempt, situation, desired_outcome, acceptance_criteria, specialist, has_next):
        _apply_outcome(tid, task_id, step_id, "complete")
        return "complete", None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "blocked"


# --- revise_step: applies the REVISED outcome (round-3 MAJOR #4) --------------------------------


def test_revise_step_applies_the_revised_outcome_on_redispatch(substrate, monkeypatch: pytest.MonkeyPatch):
    """THE bug this fix closes: a revise_step decision's reframed desired_outcome must actually be
    applied to the re-dispatch — not silently discarded (the old code just reset the SAME step to
    'pending' with its STALE original desired_outcome). Verify the SECOND dispatch's handoff
    (the desired_outcome argument _dispatch_specialist_step receives) carries the REVISED text."""
    import orchestrator.manager.workflow as wf

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))
    _mock_verified(monkeypatch, wf)

    seen_desired_outcomes = []
    call_state = {"n": 0}

    def _dispatch(tenant_id, tid_, step_id, attempt, situation, desired_outcome, acceptance_criteria, specialist, has_next):
        seen_desired_outcomes.append(desired_outcome)
        call_state["n"] += 1
        if call_state["n"] == 1:
            _apply_outcome(tid, task_id, step_id, "revise_step")
            return "revise_step", "a narrower, revised desired outcome"
        _apply_outcome(tid, task_id, step_id, "complete")
        return "complete", None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "completed"
    assert len(seen_desired_outcomes) == 2
    assert seen_desired_outcomes[1] == "a narrower, revised desired outcome"
    assert seen_desired_outcomes[0] != seen_desired_outcomes[1]

    # The replacement landed on a NEW step row (old one superseded, real history) at a bumped
    # plan_revision — never the old step's stale text re-claimed as-is.
    with substrate.connection() as conn:
        rows = conn.execute(
            "SELECT status, detail FROM manager_task_steps WHERE tenant_id = %s AND task_id = %s "
            "ORDER BY created_at",
            (tid, task_id),
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["status"] == "superseded"
    assert rows[1]["detail"]["desired_outcome"] == "a narrower, revised desired outcome"


def test_revise_step_missing_revised_outcome_is_a_defensive_no_op(substrate, monkeypatch: pytest.MonkeyPatch):
    """Structurally unreachable in production (decide_next_action only reaches REVISE via a
    pushback carrying proposed_outcome) — guarded anyway. Must not crash; the step is simply
    re-claimed with its original (unrevised) text rather than blocking the whole task."""
    import orchestrator.manager.workflow as wf

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))
    _mock_verified(monkeypatch, wf)

    call_state = {"n": 0}

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        call_state["n"] += 1
        if call_state["n"] == 1:
            _apply_outcome(tid, task_id, step_id, "revise_step")
            return "revise_step", None  # no revised_outcome text at all
        _apply_outcome(tid, task_id, step_id, "complete")
        return "complete", None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "completed"
    assert call_state["n"] == 2


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
        wf, "_dispatch_specialist_step",
        lambda *a, **k: dispatch_calls.append(1) or ("complete", None),
    )
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "blocked"
    assert dispatch_calls == []  # never reached dispatch — the prereq gate caught it first


def test_validate_step_blocks_on_unmet_activation_prereqs_for_integration_agent(
    substrate, monkeypatch: pytest.MonkeyPatch
):
    """VT-608: integration_agent now has its own _SPECIALIST_TO_ACTIVATION_KEY mapping + its own
    activation_registry entry (previously absent — the VT-606 review's own finding — an unmapped
    specialist here is 'no key configured' -> the check is SKIPPED, not enforced). A freshly-seeded
    test tenant satisfies none of integration_agent's declared prereqs (journey-complete,
    verification, ownership-verified) either, so this must ALSO block before any dispatch."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager.plan_models import PlanStep

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(
        tid, steps=[PlanStep(step_seq=1, kind="specialist_dispatch", specialist="integration_agent")]
    ))

    dispatch_calls = []
    monkeypatch.setattr(
        wf, "_dispatch_specialist_step",
        lambda *a, **k: dispatch_calls.append(1) or ("complete", None),
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
        wf, "_dispatch_specialist_step",
        lambda *a, **k: dispatch_calls.append(1) or ("complete", None),
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
    _mock_verified(monkeypatch, wf)

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        _apply_outcome(tid, task_id, step_id, "complete")
        return "complete", None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "completed"


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
    _mock_verified(monkeypatch, wf)

    def _dispatch(tenant_id, tid_, step_id, attempt, situation, desired_outcome, acceptance_criteria, specialist, has_next):
        outcome = "continue" if has_next else "complete"
        _apply_outcome(tid, task_id, step_id, outcome)
        return outcome, None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    status = wf.manager_task_workflow(tid, task_id)
    assert status == "completed"


# --- escalate: manager_review already settled the task blocked ---------------------------------


def test_escalate_outcome_ends_loop_without_further_dispatch(substrate, monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))

    calls = []

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        calls.append(1)
        _apply_outcome(tid, task_id, step_id, "escalate")
        return "escalate", None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    status = wf.manager_task_workflow(tid, task_id)

    assert status == "blocked"
    assert len(calls) == 1  # escalate must NOT trigger a further dispatch
    # VT-632 Step 5 — an escalate must arm the honest owner closure, never leave silence after the
    # interim ack (the seeded tenant has no owner_phone, so the notify itself defers and leaves the
    # status 'pending' — what we pin here is that the terminal_outcome + pending flag were SET).
    from orchestrator.manager import task_store as _ts
    _task = _ts.get_task(tid, task_id)
    assert _task["terminal_outcome"] == "escalated"
    assert _task["owner_notification_status"] == "pending"


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
        return "revise_step", f"revision attempt {dispatch_count['n']}"

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
    # VT-632 Step 5 — a limit block also arms the honest owner closure (terminal_outcome +
    # owner_notification_status='pending'), so a blocked task can never end in owner silence.
    from orchestrator.manager import task_store as _ts
    _task = _ts.get_task(tid, task_id)
    assert _task["terminal_outcome"] == "escalated"
    assert _task["owner_notification_status"] == "pending"


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
    monkeypatch.setattr(wf, "_dispatch_specialist_step", lambda *a, **k: ("complete", None))

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
        return "continue", None

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
    _mock_verified(monkeypatch, wf)

    call_state = {"asked": False}

    def _dispatch(tenant_id, tid_, step_id, attempt, situation, desired_outcome, acceptance_criteria, specialist, has_next):
        if not call_state["asked"]:
            call_state["asked"] = True
            _apply_outcome(tid, task_id, step_id, "ask_owner")
            pending_questions.ask(tid, "which cohort?", task_id=task_id)
            return "ask_owner", None
        # Post-answer: step1's retry has step2 still pending (has_next=True) -> continue; step2's
        # own dispatch is the LAST step (has_next=False) -> complete.
        outcome = "continue" if has_next else "complete"
        _apply_outcome(tid, task_id, step_id, outcome)
        return outcome, None

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
    assert status == "completed"
    assert pending_questions.get_open(tid, task_id=task_id) == []


def test_ask_owner_answer_is_threaded_into_the_redispatch_situation(
    substrate, monkeypatch: pytest.MonkeyPatch
):
    """VT-611 pre-work #6 — the answer-threading fix's own proof. Before this fix, the resumed
    dispatch's ``situation`` was the step's ORIGINAL stored text, unchanged — the specialist had no
    idea the owner had just answered its own question and would re-ask it. Captures the ACTUAL
    ``situation`` argument _dispatch_specialist_step receives on the post-answer redispatch and
    asserts it contains both the question and the owner's answer text."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import pending_questions
    from orchestrator.manager.plan_models import PlanStep

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(
        tid,
        steps=[PlanStep(step_seq=1, kind="clarification", situation="original stored situation")],
    ))
    _mock_verified(monkeypatch, wf)

    seen_situations: list[str] = []
    call_state = {"asked": False}

    def _dispatch(tenant_id, tid_, step_id, attempt, situation, desired_outcome, acceptance_criteria, specialist, has_next):
        seen_situations.append(situation)
        if not call_state["asked"]:
            call_state["asked"] = True
            _apply_outcome(tid, task_id, step_id, "ask_owner")
            pending_questions.ask(tid, "which cohort should we target?", task_id=task_id)
            return "ask_owner", None
        _apply_outcome(tid, task_id, step_id, "complete")
        return "complete", None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)

    poll_calls = {"n": 0}
    real_still_open = wf._question_still_open

    def _still_open(tenant_id, tid_):
        poll_calls["n"] += 1
        if poll_calls["n"] >= 2:
            pending_questions.correlate_reply(tid, "the VIP cohort, please", None, task_id=task_id)
            return False
        return real_still_open(tenant_id, tid_)

    monkeypatch.setattr(wf, "_question_still_open", _still_open)

    status = wf.manager_task_workflow(tid, task_id)
    assert status == "completed"

    # The FIRST dispatch (pre-ask) got the original, un-augmented situation.
    assert seen_situations[0] == "original stored situation"
    # The SECOND dispatch (post-answer) is the fix's whole point: the owner's question AND answer
    # are both threaded in, on top of the original situation — never silently dropped.
    assert len(seen_situations) == 2
    resumed = seen_situations[1]
    assert "original stored situation" in resumed
    assert "which cohort should we target?" in resumed
    assert "the VIP cohort, please" in resumed


def test_ask_owner_no_answer_leaves_situation_unaugmented(substrate, monkeypatch: pytest.MonkeyPatch):
    """The threading state is scoped to an ACTUAL answered question — a defensive pin: if
    manager_review somehow reaches ask_owner again without ever recording an answer (e.g. the
    owner reply raced/never landed), the loop must not fabricate stale or empty Q&A text."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager.plan_models import PlanStep

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(
        tid, steps=[PlanStep(step_seq=1, kind="clarification", situation="s0")],
    ))

    assert wf._get_latest_answered_question(tid, task_id) is None


def test_ask_owner_timeout_blocks_with_incident(substrate, monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        _apply_outcome(tid, task_id, step_id, "ask_owner")
        return "ask_owner", None

    # Force the max-polls path: question never answered, and cap it small for test speed.
    monkeypatch.setattr(wf, "_OWNER_WAIT_MAX_POLLS", 2)
    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    monkeypatch.setattr(wf, "_question_still_open", lambda *a, **k: True)
    monkeypatch.setattr(wf, "_maybe_reengage_stale", lambda *a, **k: False)

    status = wf.manager_task_workflow(tid, task_id)
    assert status == "blocked"


def test_reengage_stale_called_exactly_once_per_stale_window(substrate, monkeypatch: pytest.MonkeyPatch):
    """_maybe_reengage_stale must fire ONCE per ask_owner wait (the first poll that finds the
    question still open), never on every subsequent poll tick — the ``reengaged`` flag in the
    outer loop's own ask_owner branch is what prevents a re-send storm across a long wait."""
    import orchestrator.manager.workflow as wf

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        _apply_outcome(tid, task_id, step_id, "ask_owner")
        return "ask_owner", None

    reengage_calls = {"n": 0}

    def _reengage(tenant_id, tid_):
        reengage_calls["n"] += 1
        return True  # a real reengage send was attempted

    monkeypatch.setattr(wf, "_OWNER_WAIT_MAX_POLLS", 5)  # several polls remaining after reengage
    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    monkeypatch.setattr(wf, "_question_still_open", lambda *a, **k: True)  # never answered
    monkeypatch.setattr(wf, "_maybe_reengage_stale", _reengage)

    status = wf.manager_task_workflow(tid, task_id)

    assert status == "blocked"  # exhausted _OWNER_WAIT_MAX_POLLS without an answer
    assert reengage_calls["n"] == 1  # called on poll 1 only, never again across polls 2-5


# --- queued-task promotion end-to-end ------------------------------------------------------------


def test_terminal_task_promotes_oldest_queued(substrate, monkeypatch: pytest.MonkeyPatch):
    """A verified 'complete' now genuinely reaches task_store.TASK_TERMINAL ('completed') — this
    test proves the PROMOTION WIRING fires off the back of that real transition: the oldest queued
    sibling is promoted once the active task is genuinely done."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import plan_store, task_store
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    tid = _seed_tenant(substrate)
    active_task = _create_task(tid)
    second_plan = ManagerPlan(objective="second", steps=[PlanStep(step_seq=1, kind="verification")])
    queued_task = plan_store.create_plan(tid, second_plan, source_message_sid=f"SM{uuid4().hex}")
    assert task_store.get_task(tid, queued_task)["status"] == "queued"
    _mock_verified(monkeypatch, wf)

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        _apply_outcome(tid, str(active_task), step_id, "complete")
        return "complete", None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    final_status = wf.manager_task_workflow(tid, str(active_task))

    assert final_status == "completed"
    assert task_store.get_task(tid, queued_task)["status"] == "planned"


def test_blocked_outcome_does_not_promote_a_queued_sibling(substrate, monkeypatch: pytest.MonkeyPatch):
    """The OTHER half of the promotion-wiring test: a task that ends 'blocked' (non-terminal) must
    NOT free up the tenant's admission slot — only a TRUE terminal status does."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import plan_store, task_store
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    tid = _seed_tenant(substrate)
    active_task = _create_task(tid)
    second_plan = ManagerPlan(objective="second", steps=[PlanStep(step_seq=1, kind="verification")])
    queued_task = plan_store.create_plan(tid, second_plan, source_message_sid=f"SM{uuid4().hex}")
    assert task_store.get_task(tid, queued_task)["status"] == "queued"

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        _apply_outcome(tid, str(active_task), step_id, "escalate")
        return "escalate", None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    final_status = wf.manager_task_workflow(tid, str(active_task))

    assert final_status == "blocked"
    assert task_store.get_task(tid, queued_task)["status"] == "queued"  # unchanged — still waiting


# --- paused_approval: decision-aware resolution (VT-607 fix round, adversarial review) ---------
#
# Mirrors the ask_owner tests' own mocking convention: _approval_still_pending / _approval_
# decision_for_run are mocked (matching how _question_still_open is mocked above) so these prove
# the OUTER LOOP's real poll/route control flow against real durable state — the REAL pending_
# approvals row read path is proven separately (test_sr_loop_e2e.py, DB-backed end to end).


def _paused_after_complete(tenant_id: str, task_id: str, step_id: str) -> str:
    """The paused dispatch's manager_review decision was 'complete' (ACCEPT) — mirrors
    _apply_outcome's own 'complete' branch, since that's the ONLY decision that reaches
    'paused_approval' in production (collapse only runs on a produced campaign_plan, which
    only follows an ACCEPT-shaped decision)."""
    _apply_outcome(tenant_id, task_id, step_id, "complete")
    return "paused_approval"


def test_paused_approval_poll_polarity_pin(substrate, monkeypatch: pytest.MonkeyPatch):
    """MAJOR (adversarial review, fault-injection-proven): an INVERTED _approval_still_pending
    polarity must FAIL this test. The approval stays 'pending' for several polls (asserted via a
    call-count on the mock) BEFORE resolving — proving the loop genuinely re-polls rather than
    treating the first pending check as already-resolved (or vice-versa)."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))
    _mock_verified(monkeypatch, wf)

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        outcome = _paused_after_complete(tid, task_id, step_id)
        return outcome, None

    still_pending_calls = {"n": 0}

    def _still_pending(tenant_id, tid_, step_id, attempt):
        still_pending_calls["n"] += 1
        return still_pending_calls["n"] <= 3  # pending for the first 3 polls, resolved on the 4th

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    monkeypatch.setattr(wf, "_approval_still_pending", _still_pending)
    monkeypatch.setattr(wf, "_approval_decision_for_run", lambda *a, **k: "approved")
    monkeypatch.setattr(wf, "_OWNER_WAIT_MAX_POLLS", 10)  # comfortably above the 3 pending polls

    status = wf.manager_task_workflow(tid, task_id)

    assert still_pending_calls["n"] == 4, (
        "the loop did not poll the expected number of times — a polarity inversion would make "
        "this pass on either the 1st call (treating pending as resolved) or never resolve at all"
    )
    assert status == "completed"
    assert task_store.get_task(tid, task_id)["status"] == "completed"


def test_paused_approval_resolution_timeout_blocks_with_incident(substrate, monkeypatch: pytest.MonkeyPatch):
    """MAJOR (adversarial review, test adequacy): poll exhaustion (the approval NEVER resolves)
    must hit _block_limit_exceeded — 'blocked' + a limit_exhausted incident, mirroring
    test_ask_owner_timeout_blocks_with_incident's own pattern for the SAME class of durable wait."""
    import orchestrator.manager.workflow as wf
    from orchestrator.observability.incident_store import get_incident

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        outcome = _paused_after_complete(tid, task_id, step_id)
        return outcome, None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    monkeypatch.setattr(wf, "_approval_still_pending", lambda *a, **k: True)  # never resolves
    monkeypatch.setattr(wf, "_OWNER_WAIT_MAX_POLLS", 2)

    status = wf.manager_task_workflow(tid, task_id)

    assert status == "blocked"
    with substrate.connection() as conn:
        row = conn.execute(
            "SELECT id FROM incidents WHERE tenant_id = %s AND run_id = %s", (tid, task_id)
        ).fetchone()
    assert row is not None
    incident = get_incident(tid, row["id"] if isinstance(row, dict) else row[0])
    assert incident is not None
    assert incident["incident_kind"] == "limit_exhausted"


def test_paused_approval_approved_settles_completed_with_effect(substrate, monkeypatch: pytest.MonkeyPatch):
    """The 'approved' decision path: re-enters the SAME verify-then-settle handling 'complete'
    uses — a TRUE terminal 'completed' with terminal_outcome='completed_with_effect' (the paused
    step's own evidence, recorded by _apply_outcome's 'complete' branch via manager_review's real
    plan_store effect elsewhere, is proxied here structurally the same way test_complete_with_
    evidence_settles_completed_with_effect proves it)."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import plan_store, task_store
    from orchestrator.manager.plan_models import EvidenceRef

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))
    _mock_verified(monkeypatch, wf)

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        plan_store.complete_step(
            tid, step_id, "done",
            evidence=EvidenceRef(kind="pipeline_run", ref="pr-approved-1"), expected_from=("running",),
        )
        task_store.set_task_status(tid, task_id, "verifying", expected_from=("running",))
        return "paused_approval", None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    monkeypatch.setattr(wf, "_approval_still_pending", lambda *a, **k: False)
    monkeypatch.setattr(wf, "_approval_decision_for_run", lambda *a, **k: "approved")

    status = wf.manager_task_workflow(tid, task_id)

    assert status == "completed"
    task = task_store.get_task(tid, task_id)
    assert task["terminal_outcome"] == "completed_with_effect"
    assert task["owner_notification_status"] == "pending"

    # §7D — the campaign_execution_result audit row's reasoning_ref must reference task_id (NOT
    # loop_run_id(task_id, step_id, attempt)): every reasoning-capturing write in a loop dispatch
    # (langchain_callback.py's orchestrator turn, specialists via with_reasoning_capture) is keyed
    # by the ObservabilityContext's run_id, which _dispatch_specialist_step sets to UUID(task_id)
    # regardless of attempt — task_id is what actually joins to that turn's reasoning_turn row.
    with substrate.connection() as conn:
        row = conn.execute(
            "SELECT reasoning_ref FROM tm_audit_log WHERE tenant_id = %s "
            "AND event_kind = 'campaign_execution_result' ORDER BY created_at DESC LIMIT 1",
            (tid,),
        ).fetchone()
    assert row is not None
    reasoning_ref = row["reasoning_ref"] if isinstance(row, dict) else row[0]
    assert reasoning_ref == {"run_id": str(task_id), "step_name": "orchestrator_agent_turn"}


def test_paused_approval_parks_waiting_owner_during_wait(substrate, monkeypatch: pytest.MonkeyPatch):
    """VT-668 fix 1 — while the owner is deciding, the task MUST sit 'waiting_owner' (EXCLUDED from
    the stall-sweep reaper), NOT 'verifying'/'running' (an active-work state with a 'done' step,
    which the reaper walks to dead_letter — the incident). The approval's loop_run_id is stamped
    into stall_metadata (the reverse-join anchor), and the pre-pause status is restored on
    resolution so the settle path is byte-for-byte unchanged (settles 'completed')."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))
    _mock_verified(monkeypatch, wf)

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        return _paused_after_complete(tid, task_id, step_id), None  # -> step 'done', task 'verifying'

    observed: dict = {"status": None, "stamp": None}

    def _still_pending(tenant_id, tid_, step_id, attempt):
        # First poll: fix 1 must already have parked the task 'waiting_owner' + stamped the run_id.
        if observed["status"] is None:
            t = task_store.get_task(tid, task_id)
            observed["status"] = t["status"]
            observed["stamp"] = (t.get("stall_metadata") or {}).get("awaiting_approval_run_id")
        return False  # resolve now

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    monkeypatch.setattr(wf, "_approval_still_pending", _still_pending)
    monkeypatch.setattr(wf, "_approval_decision_for_run", lambda *a, **k: "approved")

    status = wf.manager_task_workflow(tid, task_id)

    assert observed["status"] == "waiting_owner", "task not parked waiting_owner during the wait"
    assert observed["stamp"], "approval run_id not stamped into stall_metadata for the reverse join"
    assert status == "completed"  # pre-pause 'verifying' restored -> verify-then-settle unchanged


def test_paused_approval_rejected_settles_cancelled_and_promotes_queued(
    substrate, monkeypatch: pytest.MonkeyPatch
):
    """CRITICAL fix's own proof: a REJECTED decision must NEVER settle 'completed_with_effect' —
    it settles the TRUE terminal 'cancelled', terminal_outcome='cancelled' (the notification must
    read a decline, never a success), the step's own 'done' status is UNTOUCHED (the work
    genuinely happened — the owner declined the EFFECT), and promote-next fires (cancelled is
    terminal) — the OTHER half of test_terminal_task_promotes_oldest_queued's own proof, now for
    the rejected path specifically."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import plan_store, task_store
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    tid = _seed_tenant(substrate)
    active_task = _create_task(tid)
    second_plan = ManagerPlan(objective="second", steps=[PlanStep(step_seq=1, kind="verification")])
    queued_task = plan_store.create_plan(tid, second_plan, source_message_sid=f"SM{uuid4().hex}")
    assert task_store.get_task(tid, queued_task)["status"] == "queued"

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        outcome = _paused_after_complete(tid, str(active_task), step_id)
        return outcome, None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    monkeypatch.setattr(wf, "_approval_still_pending", lambda *a, **k: False)
    monkeypatch.setattr(wf, "_approval_decision_for_run", lambda *a, **k: "rejected")

    status = wf.manager_task_workflow(tid, str(active_task))

    assert status == "cancelled"
    task = task_store.get_task(tid, str(active_task))
    assert task["terminal_outcome"] == "cancelled"
    assert task["owner_notification_status"] == "pending"
    steps = task_store.get_steps(tid, str(active_task))
    assert steps[0]["status"] == "done"  # untouched — the work happened, the effect was declined
    assert task_store.get_task(tid, queued_task)["status"] == "planned"  # promoted


def test_paused_approval_needs_changes_revises_the_step(substrate, monkeypatch: pytest.MonkeyPatch):
    """A 'needs_changes' decision supersedes the (already 'done') step and inserts a fresh
    replacement carrying a revised framing, transitions the task back to 'running' (claim_next_step
    only accepts 'planned'/'running' predecessors — a task stuck 'verifying' would strand the
    replacement forever), and re-claims it — governed by the SAME per-step revision budget the
    plain revise_step path already enforces."""
    import orchestrator.manager.workflow as wf
    from orchestrator.manager import task_store

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))
    _mock_verified(monkeypatch, wf)

    dispatch_calls = {"n": 0}

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        dispatch_calls["n"] += 1
        if dispatch_calls["n"] == 1:
            outcome = _paused_after_complete(tid, task_id, step_id)
            return outcome, None
        # second attempt (the replacement step, re-claimed) — end the test cleanly.
        _apply_outcome(tid, task_id, step_id, "complete")
        return "complete", None

    decision_calls = {"n": 0}

    def _decision(tenant_id, tid_, step_id, attempt):
        decision_calls["n"] += 1
        return "needs_changes"

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    monkeypatch.setattr(wf, "_approval_still_pending", lambda *a, **k: False)
    monkeypatch.setattr(wf, "_approval_decision_for_run", _decision)

    status = wf.manager_task_workflow(tid, task_id)

    assert dispatch_calls["n"] == 2  # the original attempt + the re-claimed replacement
    assert decision_calls["n"] == 1  # only the FIRST paused_approval reaches a decision read
    assert status == "completed"  # the replacement's own 'complete' settled cleanly

    steps = task_store.get_steps(tid, task_id)
    assert len(steps) == 2
    original, replacement = (s for s in sorted(steps, key=lambda s: s["created_at"]))
    assert original["status"] == "superseded"
    assert replacement["detail"]["desired_outcome"], "the replacement must carry a revised framing"


def test_paused_approval_needs_changes_budget_exhausted_blocks(substrate, monkeypatch: pytest.MonkeyPatch):
    """The needs_changes revision budget is the SAME LIMIT_MAX_REVISIONS_PER_STEP the plain
    revise_step path enforces — exhausting it blocks + incidents rather than looping forever."""
    import orchestrator.manager.workflow as wf

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))
    _mock_verified(monkeypatch, wf)

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        outcome = _paused_after_complete(tid, task_id, step_id)
        return outcome, None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    monkeypatch.setattr(wf, "_approval_still_pending", lambda *a, **k: False)
    monkeypatch.setattr(wf, "_approval_decision_for_run", lambda *a, **k: "needs_changes")
    monkeypatch.setattr(wf, "LIMIT_MAX_REVISIONS_PER_STEP", 1)

    status = wf.manager_task_workflow(tid, task_id)

    assert status == "blocked"


def test_paused_approval_timeout_blocks_with_incident(substrate, monkeypatch: pytest.MonkeyPatch):
    """A 'timeout' decision (the approval itself timed out — the 48h scheduled sweep resolved it,
    NOT this loop's own poll-exhaustion) must block + incident, never silence, never an
    auto-success settle."""
    import orchestrator.manager.workflow as wf
    from orchestrator.observability.incident_store import get_incident

    tid = _seed_tenant(substrate)
    task_id = str(_create_task(tid))

    def _dispatch(tenant_id, tid_, step_id, *a, **k):
        outcome = _paused_after_complete(tid, task_id, step_id)
        return outcome, None

    monkeypatch.setattr(wf, "_dispatch_specialist_step", _dispatch)
    monkeypatch.setattr(wf, "_approval_still_pending", lambda *a, **k: False)
    monkeypatch.setattr(wf, "_approval_decision_for_run", lambda *a, **k: "timeout")

    status = wf.manager_task_workflow(tid, task_id)

    assert status == "blocked"
    with substrate.connection() as conn:
        row = conn.execute(
            "SELECT id, detail FROM incidents WHERE tenant_id = %s AND run_id = %s", (tid, task_id)
        ).fetchone()
    assert row is not None
    incident = get_incident(tid, row["id"] if isinstance(row, dict) else row[0])
    assert incident is not None
    assert incident["incident_kind"] == "other"
    detail = row["detail"] if isinstance(row, dict) else row[1]
    assert detail["reason"] == "owner_unreachable"
