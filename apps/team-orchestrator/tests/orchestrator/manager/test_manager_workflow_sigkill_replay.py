"""VT-606 round-3 test-adequacy item (c) — ONE real DBOS crash/replay test for
``manager_task_workflow``. Mirrors ``tests/orchestrator/test_dbos_step_resume.py``'s Landmine-2
subprocess SIGKILL pattern exactly: a worker process starts the REAL, unmodified
``manager_task_workflow`` (a real ``@DBOS.step()``-decorated ``_dispatch_specialist_step`` inside
it, real ``PostgresSaver`` checkpointing) and is SIGKILLed mid-step; a second launch's DBOS
recovery re-enters the PENDING step. The probed graph node (substituted for
``build_supervisor_graph``'s real node topology — the ONE swappable dependency, never
``_dispatch_specialist_step``/``manager_task_workflow`` themselves) shows whether the crash
attempt's work is preserved and the replay does not double-execute in a way that breaks anything.

Quarantine convention (mirrors ``test_skeleton.py::test_dbos_auto_resumes_after_sigkill``): the
function name contains "sigkill" so the pre-push hook's ``-k 'not sigkill'`` filter excludes it
(these subprocess/timing tests are slow and occasionally flaky under CI resource contention); CI's
own full run (no `-k` filter) still executes it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("langgraph")

import psycopg  # noqa: E402 — imported after the dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-606 manager_task_workflow crash/replay test skipped",
)

_WORKER = Path(__file__).parent / "_manager_workflow_sigkill_worker.py"
_CRASH_WINDOW_SECONDS = 6


def _probe_count(dsn: str, workflow_id: str, step_label: str) -> int:
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(
                "SELECT count(*) FROM _manager_workflow_probe "
                "WHERE workflow_id = %s AND step_label = %s",
                (workflow_id, step_label),
            ).fetchone()
    except psycopg.errors.UndefinedTable:
        return 0  # the worker hasn't created the probe table yet — race at process startup
    return int(row[0]) if row else 0


def _wait_for_probe(dsn: str, workflow_id: str, step_label: str, min_count: int, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _probe_count(dsn, workflow_id, step_label) >= min_count:
            return
        time.sleep(0.5)
    raise AssertionError(f"probe '{step_label}' did not reach count {min_count} within {timeout}s")


def _seed_tenant_and_task(dsn: str) -> tuple[str, str]:
    import apply_migrations

    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "test-salt")

    # plan_store.create_plan needs tenant_connection -> get_pool() — a lightweight pool init in
    # THIS (test) process, mirroring test_plan_store.py's own convention (no full DBOS launch
    # needed here; the WORKER subprocess does its own separate DBOS launch).
    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_id = str(
            conn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase) "
                "VALUES (%s, 'founding', 'onboarding') RETURNING id",
                (f"VT606-sigkill-{uuid4().hex[:8]}",),
            ).fetchone()[0]
        )

    from orchestrator.manager import plan_store
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    plan = ManagerPlan(objective="sigkill/replay test", steps=[PlanStep(step_seq=1, kind="verification")])
    task_id = str(plan_store.create_plan(tenant_id, plan, source_message_sid=f"SM{uuid4().hex}"))
    return tenant_id, task_id


def test_manager_task_workflow_survives_sigkill_mid_dispatch_and_replays() -> None:
    """A worker SIGKILLed while _dispatch_specialist_step's graph.invoke is mid-sleep resumes on
    the second launch: the DBOS workflow is observed PENDING between crash and recovery, and the
    probed node runs EXACTLY twice (crash attempt + replay) — never more (no runaway re-dispatch
    loop) and never zero (the replay genuinely re-enters the step, it doesn't skip it)."""
    dsn = os.environ["DATABASE_URL"]
    tenant_id, task_id = _seed_tenant_and_task(dsn)
    workflow_id = f"vt606-sigkill-{uuid4().hex}"

    argv = [sys.executable, str(_WORKER), dsn, tenant_id, task_id, workflow_id]

    # Launch 1: run until the probed node enters its crash window, then SIGKILL.
    proc1 = subprocess.Popen(argv)
    try:
        _wait_for_probe(dsn, workflow_id, "dispatch", min_count=1, timeout=45)
    finally:
        proc1.kill()
    proc1.wait(timeout=15)

    # Between crash and resume: the DBOS workflow is observed PENDING (the checkpointed hold
    # inside the step is durable — DBOS never partially-committed the step's result).
    from dbos import DBOSClient

    pending = {str(w.workflow_id) for w in DBOSClient(dsn).list_workflows(status="PENDING")}
    step_pending = workflow_id in pending

    # Launch 2: DBOS recovery re-enters the PENDING step; the probed node runs again.
    proc2 = subprocess.Popen(argv)
    try:
        _wait_for_probe(dsn, workflow_id, "dispatch", min_count=2, timeout=90)
        # Grace: let the workflow finish settling (verify + terminal read) post-replay.
        time.sleep(_CRASH_WINDOW_SECONDS + 6)
    finally:
        proc2.kill()
        proc2.wait(timeout=15)

    dispatch_count = _probe_count(dsn, workflow_id, "dispatch")

    print("SIGKILL_STEP_PENDING_OBSERVED:", step_pending)
    print("SIGKILL_DISPATCH_PROBE_COUNT:", dispatch_count)

    assert step_pending, (
        "the DBOS workflow was not observed PENDING between crash and resume — the crash window "
        "landed outside the step boundary"
    )
    assert dispatch_count == 2, (
        f"the probed dispatch node ran {dispatch_count}x — expected exactly 2 "
        "(crash attempt + replay), never a runaway re-dispatch loop"
    )

    with psycopg.connect(dsn, autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM manager_tasks WHERE tenant_id = %s AND id = %s",
            (tenant_id, task_id),
        ).fetchone()
    assert status is not None
    # The probed graph node deliberately bypasses manager_review's REAL persistence (it stubs
    # manager_review_outcome directly rather than running the real node) — this test's object is
    # the DBOS+LangGraph crash/replay mechanics, not the full manager_review chain (proven
    # separately, exhaustively, in test_manager_review_db.py / test_workflow.py). The task
    # correctly stays 'running' here (claim_next_step's own transition; nothing else moved it) —
    # the ceiling assertion is just that the row exists in a KNOWN, valid state, never corrupted
    # (e.g. NULL, or some status outside TASK_STATUSES) by the crash+replay.
    from orchestrator.manager.task_store import TASK_STATUSES

    assert status[0] in TASK_STATUSES, f"corrupted/unknown post-replay status: {status[0]}"
