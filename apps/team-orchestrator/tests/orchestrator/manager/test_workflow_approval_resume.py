"""VT-606 round-3 CRITICAL fix (adversarial review) — the approval-resume invariant, driven
end-to-end through the REAL ``manager.workflow._dispatch_specialist_step`` (not message_ids.py in
isolation): ``request_owner_approval_node`` persists ``state['run_id']`` into
``pending_approvals``, and ``approval_resume.resume_run`` resumes with
``thread_id=str(run_id)`` read back out of that row. Before the fix, ``_dispatch_specialist_step``
used ``step_thread_id(...)`` (a formatted string) as the checkpoint thread_id but ``UUID(task_id)``
as ``state['run_id']`` — two DIFFERENT values, so any approval interrupt raised through the loop
orphaned forever (the resume would target a thread that was never checkpointed).

Mirrors ``tests/agent/test_approval_pause_resume_integration.py``'s own pattern (a REAL
``request_owner_approval_node`` + a REAL LangGraph interrupt/resume, dry_run=True so no live
Twilio send) but drives it through ``_dispatch_specialist_step`` itself — proving THIS module's own
run_id/thread_id wiring, not just the approval node in isolation. ``build_supervisor_graph`` is
substituted with a MINIMAL graph (an orchestrator stub that attaches ``pending_approval_request`` +
the REAL ``request_owner_approval_node``) shared via a SINGLE InMemorySaver instance across both
the pause (inside ``_dispatch_specialist_step``) and the resume (inside
``approval_resume.resume_run``) — proving the SAME checkpoint thread is found on both sides. No
live Anthropic call (the graph never reaches an LLM-calling node) and no live Twilio call
(dry_run=True).
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("psycopg")
pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-606 approval-resume integration test skipped",
)


@pytest.fixture(scope="module")
def substrate():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "test-salt")
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")

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
            "INSERT INTO tenants (id, business_name, plan_tier, phase, owner_phone) "
            "VALUES (%s, %s, 'standard', 'trial', %s)",
            (tid, f"apr-{tid[:8]}", f"+9198{uuid4().int % 10**8:08d}"),
        )
    return tid


def _seed_pipeline_run(pool, tid: str, run_id) -> None:
    """``pending_approvals.run_id`` has a FOREIGN KEY to ``pipeline_runs.id`` — a SEPARATE finding
    from the thread_id/state['run_id'] mismatch this test suite's main fix addresses (flagged in
    the VT-606 round-3 completion report): NEITHER the old broken code (state['run_id']=task_id,
    also not a pipeline_runs row) nor this fix's loop_run_id value satisfies that FK on its own —
    manager_task_workflow never creates a pipeline_runs row for its own dispatches. Seeded here so
    THIS test can cleanly prove the specific invariant under test (thread_id == state['run_id']),
    isolated from that separate, pre-existing architectural gap."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running')",
            (str(run_id), tid),
        )


def _create_and_claim_step(tid: str):
    from orchestrator.manager import plan_store
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    plan = ManagerPlan(
        objective="test approval-resume wiring",
        steps=[PlanStep(step_seq=1, kind="verification")],
    )
    task_id = plan_store.create_plan(tid, plan, source_message_sid=f"SM{uuid4().hex}")
    step = plan_store.claim_next_step(tid, task_id)
    return str(task_id), step


def _orchestrator_stub_that_requests_approval(state):
    """Stands in for a real orchestrator/specialist/collapse decision chain — attaches a
    pending_approval_request, exactly what the collapse path would do before routing to
    request_owner_approval in production. No LLM call — a plain Python stub."""
    return {
        "pending_approval_request": {
            "approval_type": "campaign_send",
            "summary": "Approve this test send?",
            "details": {"cohort_size": 3},
            "template_params": {},
            "dry_run": True,  # no live Twilio call
            "timeout_hours": 48,
        }
    }


def test_approval_interrupt_through_dispatch_resumes_the_correct_thread(
    substrate, monkeypatch: pytest.MonkeyPatch
):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from orchestrator.agent.tools.request_owner_approval import request_owner_approval_node
    from orchestrator.state.agent_graph_state import AgentGraphState

    tid = _seed_tenant(substrate)
    task_id, step = _create_and_claim_step(tid)
    step_id = str(step["step_id"])
    attempt = 1

    from orchestrator.manager import message_ids as _mid_precheck

    _seed_pipeline_run(substrate, tid, _mid_precheck.loop_run_id(task_id, step_id, attempt))

    shared_saver = InMemorySaver()

    def _minimal_graph(model, checkpointer=None, *, mode=None):
        # Ignore whatever checkpointer is passed — both the pause (inside
        # _dispatch_specialist_step) and the resume (inside approval_resume.resume_run) MUST use
        # the SAME saver instance for the interrupted thread to actually be found.
        g = StateGraph(AgentGraphState)
        g.add_node("orchestrator_stub", _orchestrator_stub_that_requests_approval)
        g.add_node("request_owner_approval", request_owner_approval_node)
        g.add_edge(START, "orchestrator_stub")
        g.add_edge("orchestrator_stub", "request_owner_approval")
        g.add_edge("request_owner_approval", END)
        return g.compile(checkpointer=shared_saver)

    monkeypatch.setattr("orchestrator.supervisor.build_supervisor_graph", _minimal_graph)

    from orchestrator.manager import message_ids
    from orchestrator.manager.workflow import _dispatch_specialist_step

    expected_run_id = message_ids.loop_run_id(task_id, step_id, attempt)

    # --- pause: drive the REAL _dispatch_specialist_step ---
    outcome, _revised = _dispatch_specialist_step(
        tid, task_id, step_id, attempt,
        "test situation", "test desired outcome", ["done"], None, False,
    )
    # manager_review never ran (the graph paused at the interrupt before reaching it) — the
    # str(...or "escalate") fallback is expected and not itself under test here.
    assert outcome == "escalate"

    with substrate.connection() as conn:
        row = conn.execute(
            "SELECT run_id, decision, status FROM pending_approvals WHERE tenant_id = %s",
            (tid,),
        ).fetchone()
    assert row is not None, "arm_pause_request did not persist a pending_approvals row"
    persisted_run_id = row["run_id"] if isinstance(row, dict) else row[0]
    assert row["status" if isinstance(row, dict) else 2] == "pending"

    # THE invariant: the run_id persisted in pending_approvals is EXACTLY the same value used as
    # the graph's checkpoint thread_id (message_ids.loop_run_id) — never task_id, never anything
    # else.
    assert str(persisted_run_id) == str(expected_run_id)

    # --- resume: approval_resume.resume_run reads the PERSISTED run_id and must find the SAME
    # checkpointed thread (not orphan it) ---
    from orchestrator.agent import approval_resume

    resumed_state = approval_resume.resume_run(persisted_run_id, "approved")
    assert resumed_state.get("owner_decision") == "approved"

    # Idempotency (mirrors the existing VT-47 test): resume re-execution did not duplicate the row.
    with substrate.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM pending_approvals WHERE tenant_id = %s", (tid,)
        ).fetchone()
    assert (n["count"] if isinstance(n, dict) else n[0]) == 1


def test_approval_interrupt_orphans_without_the_fix(substrate, monkeypatch: pytest.MonkeyPatch):
    """The control case — proves the harness exercises the real defect: reintroducing the OLD
    mismatch (thread_id from a formatted string, state['run_id'] = task_id) makes resume_run
    unable to find the checkpointed thread at all (a fresh, un-checkpointed thread resumes as if
    nothing was ever paused — no interrupt to resume, silently wrong)."""
    from uuid import UUID

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from orchestrator.agent.tools.request_owner_approval import request_owner_approval_node
    from orchestrator.state.agent_graph_state import AgentGraphState

    tid = _seed_tenant(substrate)
    task_id, step = _create_and_claim_step(tid)
    step_id = str(step["step_id"])
    _seed_pipeline_run(substrate, tid, task_id)  # the OLD bug persisted run_id=task_id

    shared_saver = InMemorySaver()

    def _minimal_graph(model, checkpointer=None, *, mode=None):
        g = StateGraph(AgentGraphState)
        g.add_node("orchestrator_stub", _orchestrator_stub_that_requests_approval)
        g.add_node("request_owner_approval", request_owner_approval_node)
        g.add_edge(START, "orchestrator_stub")
        g.add_edge("orchestrator_stub", "request_owner_approval")
        g.add_edge("request_owner_approval", END)
        return g.compile(checkpointer=shared_saver)

    monkeypatch.setattr("orchestrator.supervisor.build_supervisor_graph", _minimal_graph)

    # Reproduce the OLD (broken) shape directly: thread_id is a formatted string, run_id is the
    # task_id — deliberately NOT loop_run_id, to prove the mismatch is what breaks the resume.
    from orchestrator.agent.dispatch import _BRAIN_MODEL_OPUS, _resolve_model

    broken_thread_id = f"manager_task:{task_id}:{step_id}:1"
    initial_state = {
        "messages": [],
        "tenant_id": UUID(tid),
        "run_id": UUID(task_id),  # the OLD bug: run_id != thread_id
    }
    graph = _minimal_graph(model=_resolve_model(_BRAIN_MODEL_OPUS))
    graph.invoke(initial_state, config={"configurable": {"thread_id": broken_thread_id}})

    with substrate.connection() as conn:
        row = conn.execute(
            "SELECT run_id FROM pending_approvals WHERE tenant_id = %s", (tid,)
        ).fetchone()
    assert row is not None
    persisted_run_id = row["run_id"] if isinstance(row, dict) else row[0]
    assert str(persisted_run_id) == task_id  # persisted as the task_id, per the OLD bug

    from orchestrator.agent import approval_resume

    # The resume targets thread_id=str(persisted_run_id)=task_id — NOT broken_thread_id, the
    # thread that was ACTUALLY checkpointed. LangGraph finds NO checkpoint at all under that
    # thread_id, so the graph runs the WHOLE thing from START on a brand-new, empty thread instead
    # of resuming the interrupt — request_owner_approval_node re-executes with NO
    # pending_approval_request in state (orchestrator_stub never ran on this fresh thread) and
    # raises. This is the orphaning made concrete: not a soft wrong answer, a hard crash — the
    # approval is unreachable, permanently, via the persisted run_id.
    with pytest.raises(RuntimeError, match="tenant_id / run_id missing from state"):
        approval_resume.resume_run(persisted_run_id, "approved")
