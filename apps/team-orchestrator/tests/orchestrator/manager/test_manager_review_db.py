"""VT-606 (Loop Package 3) — ``manager.review.manager_review``'s full DB-backed effects (live
Postgres). The LLM structured-extraction call is mocked (a fake Anthropic client, canned JSON) so
these tests prove the plan_store/task_store/incident persistence for real, per outcome branch.
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — manager_review DB tests skipped",
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
            (tid, f"rv-{tid[:8]}"),
        )
    return tid


def _create_and_claim(pool, tid: str):
    from orchestrator.manager import plan_store
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    plan = ManagerPlan(
        objective="test",
        steps=[PlanStep(step_seq=1, kind="verification"), PlanStep(step_seq=2, kind="verification")],
    )
    task_id = plan_store.create_plan(tid, plan, source_message_sid=f"SM{uuid4().hex}")
    step = plan_store.claim_next_step(tid, task_id)
    return task_id, step["step_id"]


def _FakeClient(payload: dict):  # noqa: N802 — factory keeps the call sites readable
    """VT-732 transport double: the extraction call goes through the multi-provider seam, so the
    injected object is a text-returning callable rather than an Anthropic SDK client."""
    text = json.dumps(payload)

    def _call(tier: str, **kwargs):  # noqa: ANN003, ANN202 — test double
        return text

    return _call


def test_manager_review_continue_persists_evidence_and_advances(pool):
    from orchestrator.manager import task_store
    from orchestrator.manager.review import manager_review

    tid = _seed_tenant(pool)
    task_id, step_id = _create_and_claim(pool, tid)

    result = manager_review(
        tid, task_id, step_id,
        situation="s", desired_outcome="d", acceptance_criteria=["done"],
        raw_output="did the thing",
        has_next_step=True,
        text_call=_FakeClient(
            {"status": "completed", "action_summary": "did it", "outcome_summary": "ok",
             "evidence_refs": [{"kind": "pipeline_run", "ref": str(uuid4())}]}
        ),
    )
    assert result.outcome == "continue"
    steps = {s["step_seq"]: s for s in task_store.get_steps(tid, task_id)}
    assert steps[1]["status"] == "done"
    assert steps[1]["evidence_kind"] == "pipeline_run"


def test_manager_review_decision_audit_row_joins_to_the_turns_reasoning(pool):
    """§7D — when the caller passes ``run_id`` (the ACTIVE ObservabilityContext's run_id — see
    manager_review's own docstring on why this is NOT state['run_id'] for a loop dispatch), the
    manager_review_decision audit row must carry a reasoning_ref pointing at the SAME (run_id,
    step_name='orchestrator_agent_turn') the turn's own reasoning_turn row uses."""
    from orchestrator.manager.review import manager_review

    tid = _seed_tenant(pool)
    task_id, step_id = _create_and_claim(pool, tid)
    ctx_run_id = uuid4()

    manager_review(
        tid, task_id, step_id,
        situation="s", desired_outcome="d", acceptance_criteria=["done"],
        raw_output="did the thing",
        has_next_step=True,
        text_call=_FakeClient(
            {"status": "completed", "action_summary": "did it", "outcome_summary": "ok"}
        ),
        run_id=ctx_run_id,
    )

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT reasoning_ref FROM tm_audit_log WHERE tenant_id = %s "
            "AND event_kind = 'manager_review_decision' ORDER BY created_at DESC LIMIT 1",
            (tid,),
        ).fetchone()
    assert row is not None
    reasoning_ref = row["reasoning_ref"] if isinstance(row, dict) else row[0]
    assert reasoning_ref == {"run_id": str(ctx_run_id), "step_name": "orchestrator_agent_turn"}


def test_manager_review_complete_settles_task_verifying(pool):
    from orchestrator.manager import task_store
    from orchestrator.manager.review import manager_review

    tid = _seed_tenant(pool)
    task_id, step_id = _create_and_claim(pool, tid)

    result = manager_review(
        tid, task_id, step_id,
        situation="s", desired_outcome="d", acceptance_criteria=["done"],
        raw_output="finished everything",
        has_next_step=False,
        text_call=_FakeClient({"status": "completed", "action_summary": "finished", "outcome_summary": "done"}),
    )
    assert result.outcome == "complete"
    assert task_store.get_task(tid, task_id)["status"] == "verifying"


def test_manager_review_revise_step_resets_pending(pool):
    from orchestrator.manager import task_store
    from orchestrator.manager.review import manager_review

    tid = _seed_tenant(pool)
    task_id, step_id = _create_and_claim(pool, tid)

    result = manager_review(
        tid, task_id, step_id,
        situation="s", desired_outcome="d", acceptance_criteria=["done"],
        raw_output="pushed back",
        has_next_step=True,
        text_call=_FakeClient(
            {"status": "blocked", "action_summary": "", "outcome_summary": "wrong framing",
             "reason_code": "wrong_framing", "proposed_outcome": "try a narrower cohort"}
        ),
    )
    assert result.outcome == "revise_step"
    steps = {s["step_seq"]: s for s in task_store.get_steps(tid, task_id)}
    assert steps[1]["status"] == "pending"


def test_manager_review_ask_owner_opens_pending_question(pool, monkeypatch):
    """VT-755 scope 0 LANDED: the park is now conditional on CONFIRMED DELIVERY.

    The previous version of this test asserted `waiting_owner` unconditionally and carried a note
    saying so — parking on a question that was never emitted is precisely the wedge (nothing can wake
    it, and the stall reaper excludes `waiting_owner`). With the emitter in place the question is sent
    through the single owner-emission choke first, and only a real send earns the park.
    """
    from orchestrator.manager import pending_questions, task_store
    from orchestrator.manager.review import manager_review

    tid = _seed_tenant(pool)
    task_id, step_id = _create_and_claim(pool, tid)

    # The recipient is stubbed rather than written to `tenants.owner_phone`: owner_phone is UNIQUE
    # across tenants, and no real number belongs in a test fixture (a seeded live number is one
    # harness away from a real send).
    sent: list[str] = []
    monkeypatch.setattr("orchestrator.manager.owner_ask._owner_phone", lambda _t: "+910000000001")
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack",
        lambda tenant_id, recipient, body: sent.append(body) or True,
    )

    result = manager_review(
        tid, task_id, step_id,
        situation="s", desired_outcome="d", acceptance_criteria=["done"],
        raw_output="needs input",
        has_next_step=True,
        text_call=_FakeClient({"status": "needs_owner_input", "owner_question": "which cohort?"}),
    )
    assert result.outcome == "ask_owner"
    assert sent == ["which cohort?"], "the question was parked on without ever being sent"
    assert task_store.get_task(tid, task_id)["status"] == "waiting_owner"
    delivered = pending_questions.get_open(tid, task_id=task_id)
    assert len(delivered) == 1, "a sent question must be answerable (delivered_at stamped)"
    # VT-755: get_open() shows DELIVERED questions by default. This test is about the review branch
    # OPENING a question row, not about the owner being able to answer it, so it reads the undelivered
    # view explicitly. (The neighbouring VT-606 test's docstring already states this row's principle —
    # "nothing would ever answer a question that was never asked" — but applied it only to the
    # empty-question-text case; VT-755 is that same principle holding for EVERY question, because
    # pending_questions has no emitter at all.)
    open_qs = pending_questions.get_open(tid, task_id=task_id, include_undelivered=True)
    assert len(open_qs) == 1
    assert open_qs[0]["question_text"] == "which cohort?" or "which cohort" in open_qs[0]["question_text"]


def test_manager_review_clarify_without_question_text_redirects_to_revise_not_waiting(pool):
    """VT-606 round-3 MINOR fix: decide_next_action reaches CLARIFY whenever the legacy
    action_taken is empty — reachable NOT only via status='needs_owner_input' (which
    PlanSpecialistReturn's OWN validator already requires owner_question for) but ALSO via
    status='completed' with an empty action_summary (no such requirement there) — a real
    specialist could plausibly report "nothing to show" without ever setting owner_question. That
    combination must NEVER park the step/task waiting (nothing would ever answer a question that
    was never asked) — redirected to revise_step instead (one more cycle), never a stuck
    waiting_owner with no path to resume."""
    from orchestrator.manager import pending_questions, task_store
    from orchestrator.manager.review import manager_review

    tid = _seed_tenant(pool)
    task_id, step_id = _create_and_claim(pool, tid)

    result = manager_review(
        tid, task_id, step_id,
        situation="s", desired_outcome="original framing", acceptance_criteria=["done"],
        raw_output="the specialist reported nothing actionable, no question either",
        has_next_step=True,
        text_call=_FakeClient(
            {"status": "completed", "action_summary": "", "outcome_summary": "nothing to report"}
        ),
    )

    assert result.outcome == "revise_step"
    assert result.decision.revised_outcome is not None
    assert result.decision.revised_outcome != "original framing"
    task = task_store.get_task(tid, task_id)
    assert task["status"] == "running"  # NEVER 'waiting_owner'
    steps = {s["step_seq"]: s for s in task_store.get_steps(tid, task_id)}
    assert steps[1]["status"] == "pending"  # NEVER 'waiting'
    # No pending question was ever opened — nothing to correlate against, so none should exist.
    assert pending_questions.get_open(tid, task_id=task_id) == []


def test_manager_review_escalate_blocks_task_and_creates_incident(pool):
    from orchestrator.manager import task_store
    from orchestrator.manager.review import manager_review
    from orchestrator.observability.incident_store import get_incident

    tid = _seed_tenant(pool)
    task_id, step_id = _create_and_claim(pool, tid)

    result = manager_review(
        tid, task_id, step_id,
        situation="s", desired_outcome="d", acceptance_criteria=["done"],
        raw_output="no path forward",
        has_next_step=True,
        text_call=_FakeClient(
            {"status": "blocked", "reason_code": "no_consent", "outcome_summary": "cannot proceed"}
        ),
    )
    assert result.outcome == "escalate"
    assert task_store.get_task(tid, task_id)["status"] == "blocked"
    assert result.incident_id is not None
    incident = get_incident(tid, result.incident_id)
    assert incident is not None
    assert incident["escalation_tier"] >= 2


def test_manager_review_extraction_failure_fails_closed_to_escalate(pool):
    """A garbled/non-JSON specialist-extraction response must NEVER be silently guessed — it
    fails closed to blocked+escalate, never a crash and never a fabricated 'completed'."""
    from orchestrator.manager import task_store
    from orchestrator.manager.review import manager_review

    tid = _seed_tenant(pool)
    task_id, step_id = _create_and_claim(pool, tid)

    def _broken_call(tier: str, **kwargs):  # noqa: ANN003, ANN202 — test double
        return "not json"

    result = manager_review(
        tid, task_id, step_id,
        situation="s", desired_outcome="d", acceptance_criteria=["done"],
        raw_output="whatever",
        has_next_step=True,
        text_call=_broken_call,
    )
    assert result.outcome == "escalate"
    assert task_store.get_task(tid, task_id)["status"] == "blocked"


def test_manager_review_ask_owner_ESCALATES_when_the_question_cannot_be_delivered(pool, monkeypatch):
    """VT-755 scope 0, the other half. An undelivered question must NOT park the task.

    A task parked on a question the owner never received is the immortal wedge scope 3 had to build a
    detector for — `waiting_owner` is in TASK_ACTIVE, so the stall reaper skips it, and nothing can
    ever wake it. Refusing the park removes the CONDITION rather than alerting on it.

    The escalate must be the FULL escalate, not two status writes: a blocked task nobody was paged
    about is the same silence VT-746 closed.
    """
    from orchestrator.manager import task_store
    from orchestrator.manager.review import manager_review

    tid = _seed_tenant(pool)  # no owner_phone on the row -> undeliverable
    task_id, step_id = _create_and_claim(pool, tid)

    incidents: list[dict] = []
    monkeypatch.setattr(
        "orchestrator.manager.review.create_incident",
        lambda tenant_id, **kw: incidents.append(kw) or None,
    )

    result = manager_review(
        tid, task_id, step_id,
        situation="s", desired_outcome="d", acceptance_criteria=["done"],
        raw_output="needs input",
        has_next_step=True,
        text_call=_FakeClient({"status": "needs_owner_input", "owner_question": "which cohort?"}),
    )
    assert result.outcome == "escalate", "an unasked question parked the task anyway"
    assert task_store.get_task(tid, task_id)["status"] == "blocked"
    assert incidents and incidents[0]["detail"]["reason"] == "ask_owner_undelivered", (
        "the undelivered path blocked the task without raising an incident — a wedge nobody is paged "
        "about"
    )
