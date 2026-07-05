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


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResp:
    def __init__(self, content: list) -> None:
        self.content = content


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    @property
    def messages(self):
        payload = self._payload

        class _M:
            @staticmethod
            def create(**kwargs):  # noqa: ANN003, ANN201
                return _FakeResp([_FakeTextBlock(json.dumps(payload))])

        return _M()


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
        client=_FakeClient(
            {"status": "completed", "action_summary": "did it", "outcome_summary": "ok",
             "evidence_refs": [{"kind": "pipeline_run", "ref": str(uuid4())}]}
        ),
    )
    assert result.outcome == "continue"
    steps = {s["step_seq"]: s for s in task_store.get_steps(tid, task_id)}
    assert steps[1]["status"] == "done"
    assert steps[1]["evidence_kind"] == "pipeline_run"


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
        client=_FakeClient({"status": "completed", "action_summary": "finished", "outcome_summary": "done"}),
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
        client=_FakeClient(
            {"status": "blocked", "action_summary": "", "outcome_summary": "wrong framing",
             "reason_code": "wrong_framing", "proposed_outcome": "try a narrower cohort"}
        ),
    )
    assert result.outcome == "revise_step"
    steps = {s["step_seq"]: s for s in task_store.get_steps(tid, task_id)}
    assert steps[1]["status"] == "pending"


def test_manager_review_ask_owner_opens_pending_question(pool):
    from orchestrator.manager import pending_questions, task_store
    from orchestrator.manager.review import manager_review

    tid = _seed_tenant(pool)
    task_id, step_id = _create_and_claim(pool, tid)

    result = manager_review(
        tid, task_id, step_id,
        situation="s", desired_outcome="d", acceptance_criteria=["done"],
        raw_output="needs input",
        has_next_step=True,
        client=_FakeClient({"status": "needs_owner_input", "owner_question": "which cohort?"}),
    )
    assert result.outcome == "ask_owner"
    assert task_store.get_task(tid, task_id)["status"] == "waiting_owner"
    open_qs = pending_questions.get_open(tid, task_id=task_id)
    assert len(open_qs) == 1
    assert open_qs[0]["question_text"] == "which cohort?" or "which cohort" in open_qs[0]["question_text"]


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
        client=_FakeClient(
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

    class _BrokenClient:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kwargs):  # noqa: ANN003, ANN201
                return _FakeResp([_FakeTextBlock("not json")])

    result = manager_review(
        tid, task_id, step_id,
        situation="s", desired_outcome="d", acceptance_criteria=["done"],
        raw_output="whatever",
        has_next_step=True,
        client=_BrokenClient(),
    )
    assert result.outcome == "escalate"
    assert task_store.get_task(tid, task_id)["status"] == "blocked"
