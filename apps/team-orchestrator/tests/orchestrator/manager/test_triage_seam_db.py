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
    # Round-3 fix: a shadow plan persists status='shadow' — never 'planned'/'queued', so it can
    # never occupy the tenant's one-active-task admission slot.
    assert task["status"] == "shadow"
    assert task_store.has_active_task(tid) is False


def test_shadow_task_never_blocks_a_real_new_task_admission(pool, monkeypatch: pytest.MonkeyPatch):
    """The orphan-task bug this fix closes: a shadow plan must not occupy the admission slot, so a
    REAL (enforce-mode) new_task right behind it still lands 'planned', not 'queued'."""
    from orchestrator.manager import task_store
    from orchestrator.manager import triage_seam as ts

    tid = _seed_tenant(pool)
    _mock_triage(monkeypatch, "new_task")
    _mock_valid_plan(monkeypatch, valid=True)

    shadow_result = ts.triage_seam(tid, "shadow ask", "SM110", mode="shadow")
    assert task_store.get_task(tid, shadow_result.task_id)["status"] == "shadow"

    monkeypatch.setattr(
        "orchestrator.manager.workflow.start_manager_task_workflow", lambda *a, **k: None
    )
    real_result = ts.triage_seam(tid, "real ask", "SM111b", mode="enforce")

    assert task_store.get_task(tid, real_result.task_id)["status"] == "planned"


def test_template_draft_never_consults_the_llm_validator(pool, monkeypatch: pytest.MonkeyPatch):
    """VT-633 — the minimal TEMPLATE draft is validated by construction: the per-turn opus call is
    GONE for it (it was a coin flip that intermittently rejected the constant as 'unfalsifiable'
    and silently rerouted the turn to legacy). A raising validator proves it is never consulted;
    the plan is still created."""
    from orchestrator.manager import triage_seam as ts

    tid = _seed_tenant(pool)
    _mock_triage(monkeypatch, "new_task")
    monkeypatch.setattr(
        "orchestrator.manager.plan_validation.validate_plan_draft",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("validator must not be called")),
    )

    result = ts.triage_seam(tid, "some ask", "SM222", mode="shadow")

    assert result.outcome == "new_task"
    assert result.task_id is not None  # plan created deterministically — no LLM in the path
    assert result.skip_legacy_dispatch is False  # shadow still never owns the turn


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


def test_enforce_new_task_queued_behind_active_does_not_force_start(
    pool, monkeypatch: pytest.MonkeyPatch
):
    """Round-3 fix: when create_plan admits the new_task as 'queued' (another task is already
    active), the workflow must NOT be force-started — there's nothing pending to claim yet — and
    the turn falls through to the legacy path rather than being silently skipped."""
    from orchestrator.manager import plan_store, task_store
    from orchestrator.manager import triage_seam as ts
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    tid = _seed_tenant(pool)
    plan_store.create_plan(
        tid,
        ManagerPlan(objective="already active", steps=[PlanStep(step_seq=1, kind="verification")]),
        source_message_sid=f"SM{uuid4().hex}",
    )
    _mock_triage(monkeypatch, "new_task")
    _mock_valid_plan(monkeypatch, valid=True)

    started = {"called": False}
    monkeypatch.setattr(
        "orchestrator.manager.workflow.start_manager_task_workflow",
        lambda *a, **k: started.__setitem__("called", True),
    )

    result = ts.triage_seam(tid, "a second ask", "SM556", mode="enforce")

    assert result.outcome == "new_task"
    assert result.task_id is not None
    assert task_store.get_task(tid, result.task_id)["status"] == "queued"
    assert started["called"] is False
    assert result.skip_legacy_dispatch is False  # falls through — no dedicated queued-reply path


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


def test_enforce_answer_pending_binds_to_the_owning_task_not_tenant_latest(
    pool, monkeypatch: pytest.MonkeyPatch
):
    """Round-3 fix: with TWO open questions across two different tasks, correlate_reply must
    resolve against the SPECIFIC question found (oldest first), never an implicit
    tenant-latest fallback that could answer the wrong task's question."""
    from orchestrator.manager import pending_questions
    from orchestrator.manager import triage_seam as ts

    tid = _seed_tenant(pool)
    older_task = str(uuid4())
    newer_task = str(uuid4())
    with pool.connection() as conn:
        for t in (older_task, newer_task):
            conn.execute(
                "INSERT INTO manager_tasks (tenant_id, id, objective, acceptance_criteria, "
                "source_message_ref, idempotency_key, status) "
                "VALUES (%s, %s, '{}'::jsonb, '{}'::jsonb, %s, %s, 'waiting_owner')",
                (tid, t, f"ref-{t}", f"idem-{t}"),
            )
    older_qid = pending_questions.ask(tid, "older question?", task_id=older_task)
    pending_questions.ask(tid, "newer question?", task_id=newer_task)
    _mock_triage(monkeypatch, "answer_pending")

    result = ts.triage_seam(tid, "answering the FIRST one", "SM665", mode="enforce")

    assert result.task_id is not None
    assert str(result.task_id) == older_task
    remaining_open = pending_questions.get_open(tid)
    assert len(remaining_open) == 1
    assert str(remaining_open[0]["task_id"]) == newer_task
    older = pending_questions.get_open(tid, task_id=older_task)
    assert older == []
    # the older question is now answered, not open
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT status FROM pending_questions WHERE id = %s", (str(older_qid),)
        ).fetchone()
    assert (row["status"] if isinstance(row, dict) else row[0]) == "answered"


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


def test_enforce_new_task_routing_is_deterministic_no_validator_coin_flip(
    pool, monkeypatch: pytest.MonkeyPatch
):
    """VT-633 — enforce mode: a new_task ALWAYS creates the plan and starts the durable loop, even
    with the LLM validator raising (it is not in the path for the template draft). The old
    behavior — validator rejection silently falling through to legacy dispatch — was the root of
    the delegation-lane variance (same ask: enforce on one run, legacy diseases on the next)."""
    from orchestrator.manager import triage_seam as ts

    tid = _seed_tenant(pool)
    _mock_triage(monkeypatch, "new_task")
    monkeypatch.setattr(
        "orchestrator.manager.plan_validation.validate_plan_draft",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("validator must not be called")),
    )

    started = {"called": False}
    monkeypatch.setattr(
        "orchestrator.manager.workflow.start_manager_task_workflow",
        lambda *a, **k: started.__setitem__("called", True),
    )

    result = ts.triage_seam(tid, "some ask", "SM999", mode="enforce")

    assert result.outcome == "new_task"
    assert result.task_id is not None
    assert result.skip_legacy_dispatch is True  # the loop owns the turn — deterministically
    assert started["called"] is True


def test_triage_turn_receives_the_real_has_active_task_and_has_open_question_kwargs(
    pool, monkeypatch: pytest.MonkeyPatch
):
    """The seam must pass the ACTUAL, freshly-read DB state into triage_turn's classification
    inputs — not a stale/hardcoded value. Seeds a real active task + a real open question and
    asserts triage_turn was called with has_active_task=True, has_open_question=True."""
    from orchestrator.manager import pending_questions, plan_store, triage_seam as ts
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    tid = _seed_tenant(pool)
    task_id = plan_store.create_plan(
        tid,
        ManagerPlan(objective="active", steps=[PlanStep(step_seq=1, kind="verification")]),
        source_message_sid=f"SM{uuid4().hex}",
    )
    pending_questions.ask(tid, "which cohort?", task_id=task_id)

    captured_kwargs = {}

    def _capturing_triage_turn(**kwargs):
        captured_kwargs.update(kwargs)
        from orchestrator.manager.triage import TriageResult

        return TriageResult(outcome="task_status", reasoning="test")

    monkeypatch.setattr("orchestrator.manager.triage.triage_turn", _capturing_triage_turn)

    ts.triage_seam(tid, "how's it going?", "SM1000", mode="shadow")

    assert captured_kwargs["has_active_task"] is True
    assert captured_kwargs["has_open_question"] is True
    assert captured_kwargs["message_text"] == "how's it going?"


def test_triage_turn_receives_false_kwargs_for_a_clean_tenant(pool, monkeypatch: pytest.MonkeyPatch):
    """The OTHER half: a tenant with NO active task and NO open question must pass both flags as
    False — never a stale True carried over from some other tenant/test."""
    from orchestrator.manager import triage_seam as ts

    tid = _seed_tenant(pool)

    captured_kwargs = {}

    def _capturing_triage_turn(**kwargs):
        captured_kwargs.update(kwargs)
        from orchestrator.manager.triage import TriageResult

        return TriageResult(outcome="direct_reply", reasoning="test")

    monkeypatch.setattr("orchestrator.manager.triage.triage_turn", _capturing_triage_turn)

    ts.triage_seam(tid, "hi", "SM1001", mode="shadow")

    assert captured_kwargs["has_active_task"] is False
    assert captured_kwargs["has_open_question"] is False


def test_triage_decision_audit_row_carries_the_classifiers_reasoning(
    pool, monkeypatch: pytest.MonkeyPatch
):
    """§7D — the ``triage_decision`` audit row must carry the classifier's own stated WHY
    (``TriageResult.reasoning``), not just the outcome it produced."""
    from orchestrator.manager import triage_seam as ts
    from orchestrator.manager.triage import TriageResult

    tid = _seed_tenant(pool)
    monkeypatch.setattr(
        "orchestrator.manager.triage.triage_turn",
        lambda **k: TriageResult(outcome="direct_reply", reasoning="owner asked a simple FAQ"),
    )

    ts.triage_seam(tid, "hi", "SM1100", mode="shadow")

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT decision FROM tm_audit_log WHERE tenant_id = %s "
            "AND event_kind = 'triage_decision' ORDER BY created_at DESC LIMIT 1",
            (tid,),
        ).fetchone()
    assert row is not None
    decision = row["decision"] if isinstance(row, dict) else row[0]
    assert decision["reasoning"] == "owner asked a simple FAQ"


def test_template_draft_criteria_are_structurally_falsifiable():
    """VT-633 — the template's acceptance criteria must be CHECKABLE facts (a log row, a DB
    record), never a subjective judgment ('owner confirms the ask was addressed'), so the
    verification cycle has something real to verify and no LLM can call them unfalsifiable."""
    from orchestrator.manager.triage_seam import _build_draft_plan

    draft = _build_draft_plan("win back my lapsed customers")
    joined = " | ".join(draft.acceptance_criteria).lower()
    assert "conversation log" in joined
    assert "db record" in joined
    assert "owner confirms" not in joined  # the old unfalsifiable criterion is gone
