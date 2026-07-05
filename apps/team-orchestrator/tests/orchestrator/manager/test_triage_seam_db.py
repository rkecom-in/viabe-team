"""VT-606 (team-lead ruling round 2) — the triage seam's shadow/enforce behavior (live Postgres for
tenant/task/pending_questions reads; triage_turn + validate_plan_draft + start_manager_task_workflow
are mocked — no real Anthropic/DBOS call)."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("anthropic")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — shadow/enforce triage_seam tests skipped",
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
            (tid, f"ts-{tid[:8]}"),
        )
    return tid


def _mock_triage(monkeypatch, outcome: str):
    from orchestrator.manager.triage import TriageResult

    def _fake(**kwargs):
        return TriageResult(outcome=outcome, reasoning="test")

    monkeypatch.setattr("orchestrator.manager.triage.triage_turn", _fake)


def _mock_valid_plan(monkeypatch, *, valid: bool = True):
    from orchestrator.manager.plan_validation import PlanValidationResult

    monkeypatch.setattr(
        "orchestrator.manager.plan_validation.validate_plan_draft",
        lambda plan, **k: PlanValidationResult(valid=valid, reason="test"),
    )


def test_shadow_new_task_creates_plan_and_never_skips_legacy(pool, monkeypatch: pytest.MonkeyPatch):
    from orchestrator.manager import triage_seam as ts
    from orchestrator.manager import task_store

    tid = _seed_tenant(pool)
    _mock_triage(monkeypatch, "new_task")
    _mock_valid_plan(monkeypatch, valid=True)

    result = ts.triage_seam(tid, "please win back my lapsed customers", "SM111", mode="shadow")

    assert result.outcome == "new_task"
    assert result.task_id is not None
    assert result.skip_legacy_dispatch is False  # shadow NEVER owns the reply/effect
    task = task_store.get_task(tid, result.task_id)
    assert task is not None
    assert task["status"] in ("planned", "queued")  # an inert, unstarted plan row


def test_shadow_plan_validation_failure_creates_no_plan(pool, monkeypatch: pytest.MonkeyPatch):
    from orchestrator.manager import triage_seam as ts

    tid = _seed_tenant(pool)
    _mock_triage(monkeypatch, "new_task")
    _mock_valid_plan(monkeypatch, valid=False)

    result = ts.triage_seam(tid, "some ask", "SM222", mode="shadow")

    assert result.outcome == "new_task"
    assert result.task_id is None
    assert result.skip_legacy_dispatch is False


def test_shadow_direct_reply_records_decision_without_a_plan(pool, monkeypatch: pytest.MonkeyPatch):
    from orchestrator.manager import triage_seam as ts

    tid = _seed_tenant(pool)
    _mock_triage(monkeypatch, "direct_reply")

    result = ts.triage_seam(tid, "hi", "SM333", mode="shadow")

    assert result.outcome == "direct_reply"
    assert result.task_id is None
    assert result.skip_legacy_dispatch is False


def test_triage_classify_failure_falls_back_to_legacy_behavior(pool, monkeypatch: pytest.MonkeyPatch):
    """triage.py's own fail-soft contract (garbled/errored classify -> None) must make the seam
    behave EXACTLY like legacy for this turn — no plan, no skip."""
    from orchestrator.manager import triage_seam as ts

    tid = _seed_tenant(pool)
    monkeypatch.setattr("orchestrator.manager.triage.triage_turn", lambda **k: None)

    result = ts.triage_seam(tid, "whatever", "SM444", mode="shadow")

    assert result.outcome is None
    assert result.task_id is None
    assert result.skip_legacy_dispatch is False


def test_enforce_new_task_starts_workflow_and_skips_legacy(pool, monkeypatch: pytest.MonkeyPatch):
    from orchestrator.manager import triage_seam as ts

    tid = _seed_tenant(pool)
    _mock_triage(monkeypatch, "new_task")
    _mock_valid_plan(monkeypatch, valid=True)

    started = {}
    monkeypatch.setattr(
        "orchestrator.manager.workflow.start_manager_task_workflow",
        lambda tenant_id, task_id: started.update(tenant_id=tenant_id, task_id=task_id),
    )

    result = ts.triage_seam(tid, "win back lapsed customers", "SM555", mode="enforce")

    assert result.outcome == "new_task"
    assert result.skip_legacy_dispatch is True
    assert started["task_id"] == result.task_id


def test_enforce_answer_pending_correlates_and_skips_legacy(pool, monkeypatch: pytest.MonkeyPatch):
    from orchestrator.manager import pending_questions
    from orchestrator.manager import triage_seam as ts

    tid = _seed_tenant(pool)
    pending_questions.ask(tid, "which cohort?")
    _mock_triage(monkeypatch, "answer_pending")

    result = ts.triage_seam(tid, "the VIP cohort", "SM666", mode="enforce")

    assert result.outcome == "answer_pending"
    assert result.skip_legacy_dispatch is True
    assert pending_questions.get_open(tid) == []


def test_enforce_cancel_task_falls_through_to_legacy(pool, monkeypatch: pytest.MonkeyPatch):
    from orchestrator.manager import triage_seam as ts

    tid = _seed_tenant(pool)
    _mock_triage(monkeypatch, "cancel_task")

    result = ts.triage_seam(tid, "stop that", "SM777", mode="enforce")

    assert result.outcome == "cancel_task"
    assert result.skip_legacy_dispatch is False  # documented gap — no cancellation pipeline yet


def test_enforce_task_status_falls_through_to_legacy(pool, monkeypatch: pytest.MonkeyPatch):
    from orchestrator.manager import triage_seam as ts

    tid = _seed_tenant(pool)
    _mock_triage(monkeypatch, "task_status")

    result = ts.triage_seam(tid, "how's it going?", "SM888", mode="enforce")

    assert result.outcome == "task_status"
    assert result.skip_legacy_dispatch is False


def test_enforce_new_task_plan_validation_failure_falls_through_to_legacy(
    pool, monkeypatch: pytest.MonkeyPatch
):
    from orchestrator.manager import triage_seam as ts

    tid = _seed_tenant(pool)
    _mock_triage(monkeypatch, "new_task")
    _mock_valid_plan(monkeypatch, valid=False)

    started = {"called": False}
    monkeypatch.setattr(
        "orchestrator.manager.workflow.start_manager_task_workflow",
        lambda *a, **k: started.__setitem__("called", True),
    )

    result = ts.triage_seam(tid, "some ask", "SM999", mode="enforce")

    assert result.outcome == "new_task"
    assert result.task_id is None
    assert result.skip_legacy_dispatch is False
    assert started["called"] is False
