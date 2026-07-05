"""VT-607 (Loop Package 6) — "the first specialist through the loop": ONE full DB-backed
end-to-end acceptance test driving the REAL, unmodified ``manager_task_workflow`` (not just
``_dispatch_specialist_step`` in isolation — ``test_workflow_approval_resume.py`` already proves
that half) through Package 6's exact acceptance chain: seeded cohort -> loop task -> SR step ->
grounded plan -> manager review accepts -> approval pause (real ``pending_approvals`` via the REAL
FK path, VT-607 (a)) -> resume approved -> campaign advances -> verification -> terminal
'completed' with terminal_outcome='completed_with_effect' -> owner_notification_status='pending'.

This test is ALSO the empirical proof for a gap discovered while building it: the outer workflow
loop, before this fix, could not correctly resume after ANY approval-gate interrupt (it treated a
live, healthy pause as manager_review having already escalated/blocked the task, permanently
stranding it at 'verifying'). manager.workflow.py's new 'paused_approval' branch is what THIS test
exercises end-to-end — the interrupt is raised by ``manager_task_workflow`` (via
``DBOS.start_workflow``, a background execution) while THIS test resolves the approval from a
SEPARATE call (``approval_resume.resume_run``), exactly mirroring how the real webhook path
resolves it independently of the loop that raised it.

Fakes: the two LLM call sites (the orchestrator's spawn-tool-calling model, and the completion-
verification opus checkpoint's Anthropic client) and Twilio (package autouse stub). Real: SR's
CampaignPlan->PlanSpecialistReturn grounding+adapter (a REAL DB existence check against the real
seeded cohort), collapse, the approval gate, campaign_execute, and the full outer loop's
approval-pause handling.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

_E2E_HELPERS_DIR = Path(__file__).resolve().parents[1]
if str(_E2E_HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(_E2E_HELPERS_DIR))

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("dbos")
pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-607 SR-loop end-to-end test skipped",
)


@pytest.fixture()
def substrate() -> Any:
    dsn = os.environ["DATABASE_URL"]
    import apply_migrations

    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "test-salt")

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


class _FakeVerifyTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeVerifyResp:
    def __init__(self, content: list) -> None:
        self.content = content


class _FakeVerifyClient:
    """Fakes verification.verify_completion's Anthropic().messages.create call — a canned
    'verified' verdict. deterministic_floor_ok (the pure pre-check) runs for real."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    @property
    def messages(self) -> Any:
        payload = self._payload

        class _M:
            @staticmethod
            def create(**kwargs: Any) -> _FakeVerifyResp:
                return _FakeVerifyResp([_FakeVerifyTextBlock(json.dumps(payload))])

        return _M()


def _wait_for(predicate, timeout: float, interval: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result is not None:
            return result
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


def test_sr_through_the_loop_full_acceptance(substrate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_TWILIO_MOCK_MODE", "1")
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "enforce")

    # The workflow's real poll interval (5 min) would make this test take up to 5 minutes to
    # notice the resolution this test drives concurrently — shorten it (a test-only knob, never a
    # production behavior change) so the wait is seconds, not minutes.
    import orchestrator.manager.workflow as workflow_mod

    monkeypatch.setattr(workflow_mod, "_OWNER_WAIT_POLL_S", 0.3)
    monkeypatch.setattr(workflow_mod, "_OWNER_WAIT_MAX_POLLS", 100)  # ~30s ceiling

    from _e2e_plan import build_proposed_plan
    from _e2e_seed import seed, teardown

    dsn = substrate
    result = seed(dsn)
    t1 = result.t1

    try:
        cohort = t1.subscribed_ids + t1.opted_out_ids

        import orchestrator.supervisor as supervisor_mod

        def _fake_sr_node(state: dict[str, Any]) -> dict[str, Any]:
            plan = build_proposed_plan(tenant_id=t1.tenant_id, run_id=t1.run_id, cohort_ids=cohort)
            return {"campaign_plan": plan}

        monkeypatch.setattr(supervisor_mod, "_sales_recovery_node", _fake_sr_node)

        from langchain_core.language_models import LanguageModelInput
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.messages.base import BaseMessage
        from langchain_core.runnables import Runnable

        class _ToolBindableFake(GenericFakeChatModel):
            def bind_tools(
                self, tools: Any, *, tool_choice: Any = None, **kwargs: Any
            ) -> "Runnable[LanguageModelInput, AIMessage]":
                return self

        spawn_messages: list[BaseMessage] = [
            AIMessage(content="", tool_calls=[{"name": "spawn_sales_recovery", "args": {}, "id": "1"}]),
        ]

        import orchestrator.agent.dispatch as dispatch_mod

        def _fake_resolve_model(*_a: Any, **_kw: Any) -> Any:
            return _ToolBindableFake(messages=iter(spawn_messages))

        monkeypatch.setattr(dispatch_mod, "_resolve_model", _fake_resolve_model)

        # The completion-verification checkpoint's own Anthropic call — a canned 'verified'.
        import orchestrator.manager.verification as verification_mod

        monkeypatch.setattr(
            verification_mod,
            "Anthropic",
            lambda: _FakeVerifyClient({"verdict": "verified", "reason": "test: objective met"}),
        )

        # --- create the durable plan task (NOT claimed here — the workflow itself claims it) ---
        from orchestrator.manager import plan_store
        from orchestrator.manager.plan_models import ManagerPlan, PlanStep

        plan = ManagerPlan(
            objective="recover dormant customers",
            steps=[
                PlanStep(
                    step_seq=1, kind="specialist_dispatch", specialist="sales_recovery_agent",
                    situation="Owner asked to win back dormant customers",
                    desired_outcome="Recover dormant customers",
                )
            ],
        )
        task_id = plan_store.create_plan(
            str(t1.tenant_id), plan, source_message_sid=f"SM{uuid4().hex}"
        )

        # --- start the loop as a background DBOS workflow (fire-and-forget; get_result blocks) ---
        import dbos as _dbos

        workflow_id = f"vt607-e2e-{uuid4().hex}"
        with _dbos.SetWorkflowID(workflow_id):
            handle = _dbos.DBOS.start_workflow(
                workflow_mod.manager_task_workflow, str(t1.tenant_id), str(task_id)
            )

        # --- concurrently: wait for the approval gate to raise + persist pending_approvals, then
        # resolve it from a SEPARATE call, exactly like the real webhook path does (runner.py's
        # correlate_reply: mark_approval_resolved FIRST — the single resolution choke point that
        # actually flips pending_approvals.status away from 'pending' — THEN resume_run) ---
        def _find_pending_approval():
            with psycopg.connect(dsn, autocommit=True) as conn:
                row = conn.execute(
                    "SELECT id, run_id FROM pending_approvals WHERE tenant_id = %s AND status = 'pending'",
                    (str(t1.tenant_id),),
                ).fetchone()
            return row if row else None

        approval_id, persisted_run_id = _wait_for(_find_pending_approval, timeout=30.0)

        from orchestrator.agent import approval_resume
        from orchestrator.db import tenant_connection

        with tenant_connection(str(t1.tenant_id)) as conn, conn.transaction():
            resolved = approval_resume.mark_approval_resolved(
                conn, str(t1.tenant_id), approval_id, "approved",
            )
        assert resolved is True

        resumed_state = approval_resume.resume_run(persisted_run_id, "approved")
        assert resumed_state.get("owner_decision") == "approved"
        assert resumed_state.get("campaign_execution_error") is None

        # --- the workflow's own poll loop notices the resolution, verifies, settles ---
        final_status = handle.get_result()
        assert final_status == "completed"

        from orchestrator.manager import task_store

        task_row = task_store.get_task(str(t1.tenant_id), task_id)
        assert task_row is not None
        assert task_row["status"] == "completed"
        assert task_row["terminal_outcome"] == "completed_with_effect"
        assert task_row["owner_notification_status"] == "pending"

        with psycopg.connect(dsn, autocommit=True) as conn:
            campaign_status = conn.execute(
                "SELECT status FROM campaigns WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 1",
                (str(t1.tenant_id),),
            ).fetchone()
        assert campaign_status is not None
        assert campaign_status[0] == "sent"
    finally:
        teardown(dsn, result)
