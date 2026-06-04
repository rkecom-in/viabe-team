"""VT-47 — pause/resume integration test (live local Postgres + checkpointer).

DB-gated (DATABASE_URL). Exercises the REAL request_owner_approval_node over a
minimal graph compiled with the REAL PostgresSaver, against a SEEDED SYNTHETIC
tenant (CL-422). Asserts:

  - pause: interrupt() halts the graph (__interrupt__ surfaced), a
    pending_approvals row exists with decision NULL.
  - resume(approved): the node returns owner_decision='approved'; exactly ONE
    approval row (resume re-execution did not duplicate — idempotency guard).
  - Pillar-7 cannot-bypass: with NO resume decision (still paused) the graph
    has NOT produced an owner_decision='approved' — the gate is authoritative,
    the send cannot proceed without an explicit approval.
  - resume(rejected): owner_decision='rejected' (a non-approval terminal).
  - resolve path: resolve_decision_from_reply + mark_approval_resolved drive
    the durable row to decision='approved', status='approved', resolved.

No live Twilio (dry_run send) and no live Anthropic (classify_fn stubbed).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("langgraph")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-47 pause/resume integration test skipped",
)


@pytest.fixture()
def substrate():
    """Init the module-level substrate (pool + PostgresSaver) against the test
    DB, and tear it down after.

    reset_substrate() FIRST so a prior test module that left the module-level
    pool/compiled-graph open (init_substrate is idempotent and returns early
    when _compiled is set) cannot hand us a stale/closed pool — this fixture
    owns a clean substrate for the pause/resume cycle."""
    from orchestrator import graph as graphmod

    dsn = os.environ["DATABASE_URL"]
    graphmod.reset_substrate()
    graphmod.init_substrate(dsn)
    yield {"dsn": dsn, "graphmod": graphmod}
    graphmod.reset_substrate()


def _build_gate_graph(checkpointer):
    from langgraph.graph import END, START, StateGraph

    from orchestrator.agent.tools.request_owner_approval import (
        request_owner_approval_node,
    )
    from orchestrator.state.agent_graph_state import AgentGraphState

    g = StateGraph(AgentGraphState)
    g.add_node("gate", request_owner_approval_node)
    g.add_edge(START, "gate")
    g.add_edge("gate", END)
    return g.compile(checkpointer=checkpointer)


def _seed(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        tid = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, owner_phone) "
            "VALUES ('VT47 IT', 'founding', 'onboarding', %s) RETURNING id",
            (f"+9198{uuid4().int % 10**8:08d}",),
        ).fetchone()[0]
        rid = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
            "VALUES (%s, 'orchestrator', 'running') RETURNING id",
            (tid,),
        ).fetchone()[0]
    return str(tid), str(rid)


def _request(tid, rid):
    return {
        "tenant_id": __import__("uuid").UUID(tid),
        "run_id": __import__("uuid").UUID(rid),
        "pending_approval_request": {
            "approval_type": "campaign_send",
            "summary": "Approve send to 3 customers?",
            "details": {"cohort_size": 3},
            "template_params": {},
            "dry_run": True,
            "timeout_hours": 48,
        },
    }


def test_pause_then_resume_approved(substrate):
    from langgraph.types import Command

    dsn = substrate["dsn"]
    saver = substrate["graphmod"].get_checkpointer()
    graph = _build_gate_graph(saver)
    tid, rid = _seed(dsn)
    cfg = {"configurable": {"thread_id": rid}}

    # --- pause ---
    paused = graph.invoke(_request(tid, rid), config=cfg)
    assert "__interrupt__" in paused, "interrupt() must halt the graph"

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT decision, status, resolved_at FROM pending_approvals "
            "WHERE run_id = %s",
            (rid,),
        ).fetchone()
    assert row is not None
    assert row[0] is None and row[1] == "pending" and row[2] is None

    # --- Pillar-7 cannot-bypass: still paused, no approved decision exists ---
    # The state at this point carries NO owner_decision (the run is suspended);
    # nothing downstream can have read an approval — the gate is authoritative.
    assert "owner_decision" not in paused

    # --- resume(approved) ---
    resumed = graph.invoke(Command(resume={"decision": "approved"}), config=cfg)
    assert resumed.get("owner_decision") == "approved"

    # Idempotency: resume re-executed the node but did NOT duplicate the row.
    with psycopg.connect(dsn, autocommit=True) as conn:
        n = conn.execute(
            "SELECT count(*) FROM pending_approvals WHERE run_id = %s", (rid,)
        ).fetchone()[0]
    assert n == 1, "resume re-execution must not create a second approval row"


def test_resume_rejected_is_non_approval_terminal(substrate):
    from langgraph.types import Command

    dsn = substrate["dsn"]
    saver = substrate["graphmod"].get_checkpointer()
    graph = _build_gate_graph(saver)
    tid, rid = _seed(dsn)
    cfg = {"configurable": {"thread_id": rid}}

    paused = graph.invoke(_request(tid, rid), config=cfg)
    assert "__interrupt__" in paused

    resumed = graph.invoke(Command(resume={"decision": "rejected"}), config=cfg)
    # Pillar 7: a rejection is a clean non-approval terminal — NOT 'approved'.
    assert resumed.get("owner_decision") == "rejected"


def test_resolve_path_drives_durable_row(substrate):
    """The resume-path helpers (classify -> resolve) drive the durable row to
    decision='approved', status='approved', resolved — the substrate the
    Pillar-7 send path keys on."""
    from orchestrator.agent.approval_resume import (
        find_open_approval_for_tenant,
        mark_approval_resolved,
        resolve_decision_from_reply,
    )
    from orchestrator.db import tenant_connection

    dsn = substrate["dsn"]
    saver = substrate["graphmod"].get_checkpointer()
    graph = _build_gate_graph(saver)
    tid, rid = _seed(dsn)
    cfg = {"configurable": {"thread_id": rid}}

    graph.invoke(_request(tid, rid), config=cfg)  # pause

    # Classify a synthetic owner "haan" (stub classifier — no live Anthropic).
    def stub(_text):
        return SimpleNamespace(classification="approval", confidence=0.95)

    decision = resolve_decision_from_reply("haan bhejo", tenant_id="t-vt270", classify_fn=stub)
    assert decision == "approved"

    with tenant_connection(tid) as conn:
        approval = find_open_approval_for_tenant(conn, tid)
        assert approval is not None and approval["run_id"] == rid
        mark_approval_resolved(conn, tid, approval["id"], decision, owner_message_sid="SMxyz")

    with psycopg.connect(dsn, autocommit=True) as conn:
        d, s, resolved, sid = conn.execute(
            "SELECT decision, status, resolved_at, owner_message_sid "
            "FROM pending_approvals WHERE run_id = %s",
            (rid,),
        ).fetchone()
    assert d == "approved"
    assert s == "approved"
    assert resolved is not None
    assert sid == "SMxyz"


def test_send_failure_writes_no_orphan_and_no_pause(substrate):
    """Pillar 7: when the template send fails, NO pending_approvals row is
    written and the gate does NOT pause (owner_decision='send_failed') — the
    campaign cannot proceed to send, and there is no stuck/orphan pause."""
    dsn = substrate["dsn"]
    tid, rid = _seed(dsn)

    # Inject a failing sender via the request (dry_run False so the send path
    # runs) using arm_pause_request's seam through a monkeypatched send_fn is
    # awkward at the node; instead drive arm_pause_request directly to assert
    # the no-orphan guarantee on the real DB.
    from orchestrator.agent.tools.request_owner_approval import (
        RequestOwnerApprovalInput,
        arm_pause_request,
    )

    def failing_send(tenant_id, template_name, params, *, recipient_phone=None):
        return SimpleNamespace(success=False, error_code="boom", error_message="x")

    res = arm_pause_request(
        RequestOwnerApprovalInput(
            tenant_id=__import__("uuid").UUID(tid),
            run_id=__import__("uuid").UUID(rid),
            approval_type="campaign_send",
            summary="x",
            timeout_hours=48,
        ),
        send_fn=failing_send,
    )
    assert res.status == "error"
    with psycopg.connect(dsn, autocommit=True) as conn:
        n = conn.execute(
            "SELECT count(*) FROM pending_approvals WHERE run_id = %s", (rid,)
        ).fetchone()[0]
    assert n == 0, "send failure must not leave an orphan pending_approvals row"
