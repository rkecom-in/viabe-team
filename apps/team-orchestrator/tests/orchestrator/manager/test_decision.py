"""VT-526 (B3) — the manager decision loop: reading SpecialistReturn + driving the B2 spine.

The pure decision logic runs everywhere (dep-less safe). The record_decision transitions run on
live Postgres; the situation-authoring + real-type checks importorskip the roster (agent deps).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator.manager import decision as d

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set — record_decision DB tests skipped"
)


# ── Pure decision logic (deterministic; no DB, no LLM) ───────────────────────
def _ret(**kw) -> SimpleNamespace:
    base = dict(pushback=False, action_taken="", outcome="", proposed_outcome="", reason="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_pushback_with_proposal_revises():
    dec = d.decide_next_action(
        _ret(pushback=True, proposed_outcome="target lapsed >90d", reason="cohort too broad"),
        has_next_step=False,
    )
    assert dec.kind is d.ManagerDecisionKind.REVISE
    assert dec.revised_outcome == "target lapsed >90d"


def test_pushback_without_proposal_escalates():
    dec = d.decide_next_action(
        _ret(pushback=True, reason="needs a channel we don't have"), has_next_step=True
    )
    assert dec.kind is d.ManagerDecisionKind.ESCALATE


def test_no_action_no_pushback_clarifies():
    dec = d.decide_next_action(_ret(action_taken=""), has_next_step=True)
    assert dec.kind is d.ManagerDecisionKind.CLARIFY


def test_action_with_next_step_advances():
    dec = d.decide_next_action(_ret(action_taken="drafted 12 messages"), has_next_step=True)
    assert dec.kind is d.ManagerDecisionKind.NEXT_SPECIALIST


def test_action_plan_exhausted_accepts():
    dec = d.decide_next_action(_ret(action_taken="drafted 12 messages"), has_next_step=False)
    assert dec.kind is d.ManagerDecisionKind.ACCEPT


def test_reads_the_real_specialist_return_type():
    """Prove the consumer reads the ACTUAL roster SpecialistReturn (defined-but-never-read before),
    not just a duck-typed stand-in."""
    roster = pytest.importorskip("orchestrator.agent.roster")
    ret = roster.SpecialistReturn(pushback=True, proposed_outcome="reframe as a 2-step nudge")
    dec = d.decide_next_action(ret, has_next_step=False)
    assert dec.kind is d.ManagerDecisionKind.REVISE
    assert dec.revised_outcome == "reframe as a 2-step nudge"


# ── Manager-authored situation (the "" gap) ──────────────────────────────────
def test_situation_is_manager_authorable():
    roster = pytest.importorskip("orchestrator.agent.roster")
    spec = SimpleNamespace(name="test_lane", default_outcome="static default", update_builder=None)
    upd = roster.build_handoff_update(
        spec=spec, state={"tenant_id": None},
        situation="cohort went quiet in Q2", desired_outcome="recover 10% within 30d",
    )
    env = upd[roster.HANDOFF_STATE_KEY]
    assert env.situation == "cohort went quiet in Q2"
    assert env.desired_outcome == "recover 10% within 30d"


def test_situation_defaults_preserve_prior_behaviour():
    roster = pytest.importorskip("orchestrator.agent.roster")
    spec = SimpleNamespace(name="test_lane", default_outcome="static default", update_builder=None)
    env = roster.build_handoff_update(spec=spec, state={"tenant_id": None})[roster.HANDOFF_STATE_KEY]
    assert env.situation == ""
    assert env.desired_outcome == "static default"


# ── record_decision → B2 CAS transitions (live Postgres) ─────────────────────
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


def _running_task_with_step(pool):
    from orchestrator.manager import task_store as ts

    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"dec-{tid[:8]}"),
        )
    task = ts.create_task(tid, {"goal": "x"})
    ts.set_task_status(tid, task, "running", expected_from=("clarifying",))
    step = ts.add_step(tid, task, 1, "specialist_dispatch", status="running")
    return tid, task, step


@_DB
def test_record_accept_moves_task_to_verifying(pool):
    from orchestrator.manager import task_store as ts

    tid, task, step = _running_task_with_step(pool)
    d.record_decision(tid, task, step, d.ManagerDecision(d.ManagerDecisionKind.ACCEPT, "done"))
    assert ts.get_task(tid, task)["status"] == "verifying"
    assert ts.get_steps(tid, task)[0]["status"] == "done"


@_DB
def test_record_next_specialist_advances_plan(pool):
    from orchestrator.manager import task_store as ts

    tid, task, step1 = _running_task_with_step(pool)
    step2 = ts.add_step(tid, task, 2, "specialist_dispatch")  # pending
    d.record_decision(
        tid, task, step1,
        d.ManagerDecision(d.ManagerDecisionKind.NEXT_SPECIALIST, "advance"),
        next_step_id=step2,
    )
    steps = {s["step_seq"]: s for s in ts.get_steps(tid, task)}
    assert steps[1]["status"] == "done"
    assert steps[2]["status"] == "running"
    t = ts.get_task(tid, task)
    assert t["status"] == "running"
    assert str(t["current_step_id"]) == str(step2)


@_DB
def test_record_revise_returns_step_to_pending(pool):
    from orchestrator.manager import task_store as ts

    tid, task, step = _running_task_with_step(pool)
    d.record_decision(
        tid, task, step,
        d.ManagerDecision(d.ManagerDecisionKind.REVISE, "reframe", revised_outcome="tighter cohort"),
    )
    assert ts.get_steps(tid, task)[0]["status"] == "pending"


@_DB
def test_record_clarify_parks_on_owner(pool):
    from orchestrator.manager import task_store as ts

    tid, task, step = _running_task_with_step(pool)
    d.record_decision(tid, task, step, d.ManagerDecision(d.ManagerDecisionKind.CLARIFY, "ask"))
    assert ts.get_task(tid, task)["status"] == "waiting_owner"
    assert ts.get_steps(tid, task)[0]["status"] == "waiting"


@_DB
def test_record_escalate_blocks_task(pool):
    from orchestrator.manager import task_store as ts

    tid, task, step = _running_task_with_step(pool)
    d.record_decision(tid, task, step, d.ManagerDecision(d.ManagerDecisionKind.ESCALATE, "no path"))
    assert ts.get_task(tid, task)["status"] == "blocked"
    assert ts.get_steps(tid, task)[0]["status"] == "failed"
