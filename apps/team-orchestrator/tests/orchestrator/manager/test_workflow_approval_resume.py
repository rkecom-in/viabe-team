"""VT-606 round-3 CRITICAL fix (adversarial review) — the approval-resume invariant.

Team-lead ruling (2026-07-05) on the recon's unresolved dependency: the primary proof test must be
DB-BACKED, not a minimal-graph/InMemorySaver stand-in — the critical finding is precisely about
resume across the PERSISTED thread, so the REAL PostgresSaver via ``get_checkpointer()`` IS the
subject under test. This drives the loop through the REAL production seams:

  - the REAL ``create_agent`` orchestrator (a ``ToolBindableFake`` model emits a real
    ``spawn_sales_recovery`` tool call — the orchestrator's own routing runs unfaked);
  - ``_sales_recovery_node`` monkeypatched to return a real ``CampaignPlanProposed`` (no live
    Anthropic dispatch for the specialist itself — everything downstream of it is real);
  - the REAL ``collapse_node`` (a real ``campaigns`` + ``campaign_recipients`` INSERT against a
    real seeded cohort);
  - the REAL ``manager_review`` node — ``manager_task_id``/``manager_step_id`` are seeded (via
    ``_dispatch_specialist_step`` itself, which always populates them) so it executes for real
    rather than the no-op fallback. VT-607 (Loop Package 6): a produced ``campaign_plan`` routes
    manager_review through the DETERMINISTIC typed CampaignPlan->PlanSpecialistReturn adapter
    (``adapt_campaign_plan_to_specialist_return``) — no sonnet-5 call at all for this step (the
    adapter's own grounding check runs a REAL, read-only DB query confirming the cohort's
    customer_ids resolve to real, tenant-scoped customers seeded by ``_e2e_seed.seed``);
  - the REAL ``request_owner_approval_node`` (dry_run via the package's autouse Twilio stub — no
    live Twilio call; a real ``pending_approvals`` INSERT);
  - all driven through the REAL, unmodified ``manager.workflow._dispatch_specialist_step`` in
    ``mode="enforce"``, checkpointed against the REAL module-level ``PostgresSaver``
    (``orchestrator.graph.get_checkpointer()``).

Then ``pending_approvals.run_id`` is read back from the DB and ``approval_resume.resume_run(run_id,
"approved")`` is called VERBATIM — no monkeypatching of its own checkpointer, and its own model is
faked ONLY to avoid a live LLM call at graph-compile time (the resumed node never calls it).

Mirrors ``tests/orchestrator/test_e2e_sr_agent.py``'s substrate + seed/teardown pattern exactly
(``_e2e_seed.seed`` / ``_e2e_plan.build_proposed_plan``, both siblings in ``tests/orchestrator/``).

VT-607 update: the FK gap this suite originally worked around (``pending_approvals.run_id`` /
``campaigns.run_id`` both reference ``pipeline_runs.id``, and ``manager_task_workflow`` never
created that row for its own dispatches) is now FIXED at the source —
``_dispatch_specialist_step`` mints its own ``pipeline_runs`` row before ``graph.invoke``. The
positive test below no longer seeds one manually; the control test still does, since it drives a
raw stand-in ``graph.invoke`` directly (bypassing ``_dispatch_specialist_step`` by design, to
isolate the OLD thread_id/run_id mismatch) and so never goes through the fix.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

# _e2e_seed / _e2e_plan live in tests/orchestrator/ (this file is one level down, in
# tests/orchestrator/manager/) — the tests tree is not a package, so load them by path exactly as
# test_e2e_sr_agent.py itself does.
_E2E_HELPERS_DIR = Path(__file__).resolve().parents[1]
if str(_E2E_HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(_E2E_HELPERS_DIR))

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("dbos")
pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-606 approval-resume integration test skipped",
)


@pytest.fixture()
def substrate() -> Any:
    """Apply migrations, init the module-level pool + PostgresSaver, tear down. Mirrors
    test_e2e_sr_agent.py's own fixture exactly (function-scoped, reset first so a prior module's
    stale pool can't leak in)."""
    import apply_migrations

    from orchestrator import graph as graphmod

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "test-salt")

    graphmod.reset_substrate()
    graphmod.init_substrate(dsn)
    try:
        yield dsn
    finally:
        graphmod.reset_substrate()


def _seed_pipeline_run(dsn: str, tenant_id: str, run_id: Any) -> None:
    """Both ``pending_approvals.run_id`` and ``campaigns.run_id`` carry a FOREIGN KEY to
    ``pipeline_runs.id`` (migrations 005 / 016). VT-607 fixed this at the source for the REAL
    dispatch path (``_dispatch_specialist_step`` now mints its own row) — this helper is used ONLY
    by the control test below, which drives a raw stand-in graph directly and so never goes
    through that fix."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running')",
            (str(run_id), tenant_id),
        )


def _create_and_claim_step(tenant_id: str) -> tuple[str, dict]:
    from orchestrator.manager import plan_store
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    plan = ManagerPlan(
        objective="recover dormant customers",
        steps=[
            PlanStep(step_seq=1, kind="specialist_dispatch", specialist="sales_recovery_agent")
        ],
    )
    task_id = plan_store.create_plan(tenant_id, plan, source_message_sid=f"SM{uuid4().hex}")
    step = plan_store.claim_next_step(tenant_id, task_id)
    return str(task_id), step


def test_approval_interrupt_through_real_loop_resumes_the_persisted_thread(
    substrate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CRITICAL finding's load-bearing proof, fully real seams except the two LLM call sites
    and Twilio: the interrupt is raised through the REAL, PERSISTED PostgresSaver thread, and
    approval_resume.resume_run — reading pending_approvals.run_id back from the DB, called
    VERBATIM — finds and resumes that SAME thread. Asserts the resume lands on the loop-minted
    uuid5 thread (owner_decision present) and the workflow proceeds past the resolved step
    (campaign_execute runs; campaigns.status advances to 'sent')."""
    monkeypatch.setenv("TEAM_TWILIO_MOCK_MODE", "1")
    # resume_run's own build_supervisor_graph(mode=None) call reads get_loop_mode() — must resolve
    # to the SAME shape (enforce) the pause was checkpointed under, or the resumed graph's node
    # topology would not match the persisted checkpoint.
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "enforce")

    from _e2e_plan import build_proposed_plan
    from _e2e_seed import seed, teardown

    dsn = substrate
    result = seed(dsn)
    t1 = result.t1

    try:
        cohort = t1.subscribed_ids + t1.opted_out_ids

        # --- the ONE specialist fake: a real CampaignPlanProposed, no live Anthropic dispatch ---
        import orchestrator.supervisor as supervisor_mod

        def _fake_sr_node(state: dict[str, Any]) -> dict[str, Any]:
            plan = build_proposed_plan(tenant_id=t1.tenant_id, run_id=t1.run_id, cohort_ids=cohort)
            return {"campaign_plan": plan}

        monkeypatch.setattr(supervisor_mod, "_sales_recovery_node", _fake_sr_node)

        # VT-607 (Loop Package 6): manager_review no longer needs a faked Anthropic client here —
        # a produced campaign_plan routes it through the deterministic typed adapter
        # (adapt_campaign_plan_to_specialist_return), never the sonnet-5 extraction call at all.

        # --- the orchestrator's own model: a real create_agent, driven by a canned tool call ---
        from langchain_core.language_models import LanguageModelInput
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.messages.base import BaseMessage
        from langchain_core.runnables import Runnable

        class _ToolBindableFake(GenericFakeChatModel):
            """GenericFakeChatModel survives create_agent's tool binding — mirrors
            test_supervisor.py's ToolBindableFake exactly (bind_tools must not raise)."""

            def bind_tools(
                self, tools: Any, *, tool_choice: Any = None, **kwargs: Any
            ) -> "Runnable[LanguageModelInput, AIMessage]":
                return self

        spawn_messages: list[BaseMessage] = [
            AIMessage(
                content="",
                tool_calls=[{"name": "spawn_sales_recovery", "args": {}, "id": "1"}],
            ),
        ]

        import orchestrator.agent.dispatch as dispatch_mod

        def _fake_resolve_model(*_a: Any, **_kw: Any) -> Any:
            return _ToolBindableFake(messages=iter(spawn_messages))

        monkeypatch.setattr(dispatch_mod, "_resolve_model", _fake_resolve_model)

        # --- create + claim the step, dispatch for real ---
        # VT-607: no manual pipeline_runs seeding here anymore — _dispatch_specialist_step mints
        # its own pipeline_runs row (id=loop_run_id) before graph.invoke, so the pending_approvals/
        # campaigns FK is satisfied by the REAL code path, not a test-side workaround.
        task_id, step = _create_and_claim_step(str(t1.tenant_id))
        step_id = str(step["step_id"])
        attempt = 1

        from orchestrator.manager import message_ids

        expected_run_id = message_ids.loop_run_id(task_id, step_id, attempt)

        from orchestrator.manager.workflow import _dispatch_specialist_step

        outcome, _revised = _dispatch_specialist_step(
            str(t1.tenant_id), task_id, step_id, attempt,
            "Owner asked to win back dormant customers", "Recover dormant customers",
            ["campaign proposed"], "sales_recovery_agent", False,
        )
        # manager_review DID run for real (status='completed' -> ACCEPT -> outcome='complete'),
        # but the graph continued past it to collapse -> the approval gate, which PAUSED before
        # this graph.invoke returned. VT-607: _dispatch_specialist_step reports this distinct,
        # workflow-loop-only signal (never the old "escalate" fallback, which would have made the
        # outer loop wrongly treat a live, healthy pause as an already-blocked/incident task).
        assert outcome == "paused_approval"

        # --- VT-607 adversarial proof: NO manual pipeline_runs seeding above, yet collapse's
        # campaigns INSERT and request_owner_approval's pending_approvals INSERT BOTH satisfied
        # their FK to pipeline_runs.id — because _dispatch_specialist_step minted that row itself.
        # A dangling/missing row here would have raised psycopg.errors.ForeignKeyViolation before
        # this line was ever reached; these assertions additionally confirm the row's own
        # columns/status lifecycle (mirrors close_webhook_run_paused's 'paused' convention). ---
        with psycopg.connect(dsn, autocommit=True) as conn:
            run_row = conn.execute(
                "SELECT tenant_id, run_type, status, ended_at FROM pipeline_runs WHERE id = %s",
                (str(expected_run_id),),
            ).fetchone()
        assert run_row is not None, (
            "_dispatch_specialist_step did not mint its own pipeline_runs row for loop_run_id"
        )
        run_tenant_id, run_type, run_status, ended_at = run_row
        assert str(run_tenant_id) == str(t1.tenant_id)
        assert run_type == "manager_dispatch"
        assert run_status == "paused", (
            f"a graph paused mid-invoke (the approval gate) must close pipeline_runs as 'paused' "
            f"(close_webhook_run_paused's own convention), not silently left 'running'; got {run_status!r}"
        )
        assert ended_at is not None

        # --- THE invariant: pending_approvals.run_id == the loop-minted uuid5 thread id ---
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(
                "SELECT run_id, status FROM pending_approvals WHERE tenant_id = %s",
                (str(t1.tenant_id),),
            ).fetchone()
        assert row is not None, (
            "collapse -> request_owner_approval did not persist a pending_approvals row"
        )
        persisted_run_id, status = row
        assert status == "pending"
        assert str(persisted_run_id) == str(expected_run_id)

        # --- resume: approval_resume.resume_run reads the PERSISTED run_id, called VERBATIM ---
        from orchestrator.agent import approval_resume

        resumed_state = approval_resume.resume_run(persisted_run_id, "approved")
        assert resumed_state.get("owner_decision") == "approved"

        # --- the workflow proceeds from the resolved step: campaign_execute ran for real ---
        exec_error = resumed_state.get("campaign_execution_error")
        assert exec_error is None, f"campaign_execute seam errored post-resume: {exec_error!r}"

        with psycopg.connect(dsn, autocommit=True) as conn:
            crow = conn.execute(
                "SELECT status FROM campaigns WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 1",
                (str(t1.tenant_id),),
            ).fetchone()
        assert crow is not None, "collapse must have persisted a campaigns row"
        assert crow[0] == "sent", (
            f"campaigns.status must advance to 'sent' after the approved resume; got {crow[0]!r}"
        )

        # Idempotency (mirrors the existing VT-47 test): resume did not duplicate the approval row.
        with psycopg.connect(dsn, autocommit=True) as conn:
            n = conn.execute(
                "SELECT count(*) FROM pending_approvals WHERE tenant_id = %s", (str(t1.tenant_id),)
            ).fetchone()
        assert n[0] == 1
    finally:
        teardown(dsn, result)


def test_approval_interrupt_orphans_without_the_fix(
    substrate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control case — proves the harness exercises the real defect: reintroducing the OLD
    mismatch (thread_id from a formatted string, state['run_id'] = task_id) makes resume_run
    unable to find the checkpointed thread at all. Narrower scope than the test above (just the
    thread_id/state['run_id'] wiring, not the full collapse/approval pipeline) — a minimal
    stand-in graph + InMemorySaver is the right tool here (mirrors the original VT-47 pattern);
    the DB-backed fully-real proof above is the one carrying the CRITICAL finding's actual weight."""
    from uuid import UUID

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from orchestrator.agent.tools.request_owner_approval import request_owner_approval_node
    from orchestrator.state.agent_graph_state import AgentGraphState

    dsn = substrate
    tenant_id = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, owner_phone) "
            "VALUES (%s, %s, 'standard', 'trial', %s)",
            (tenant_id, f"apr-{tenant_id[:8]}", f"+9198{uuid4().int % 10**8:08d}"),
        )

    task_id, step = _create_and_claim_step(tenant_id)
    step_id = str(step["step_id"])
    _seed_pipeline_run(dsn, tenant_id, task_id)  # the OLD bug persisted run_id=task_id

    def _orchestrator_stub_that_requests_approval(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "pending_approval_request": {
                "approval_type": "campaign_send",
                "summary": "Approve this test send?",
                "details": {"cohort_size": 3},
                "template_params": {},
                "dry_run": True,
                "timeout_hours": 48,
            }
        }

    shared_saver = InMemorySaver()

    def _minimal_graph(model: Any, checkpointer: Any = None, *, mode: Any = None) -> Any:
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
        "tenant_id": UUID(tenant_id),
        "run_id": UUID(task_id),  # the OLD bug: run_id != thread_id
    }
    graph = _minimal_graph(model=_resolve_model(_BRAIN_MODEL_OPUS))
    graph.invoke(initial_state, config={"configurable": {"thread_id": broken_thread_id}})

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT run_id FROM pending_approvals WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()
    assert row is not None
    persisted_run_id = row[0]
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
