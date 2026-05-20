"""VT-3.4 PR 2/3 — Landmine 2: @DBOS.step + langgraph node composition.

Empirical verification of the @DBOS.step / langgraph-node crash boundary: when
a DBOS step wrapping a langgraph graph invocation is SIGKILLed mid-node, does
DBOS replay compose cleanly with langgraph's checkpointer — or does the replay
double-execute an already-completed node?

UNRUN in the authoring session (CL-209): no live Postgres was available, so
this test has NOT been executed. It is committed write-only; the LANDMINE2_
observation is produced when it runs in a PG-equipped session.

Requires DATABASE_URL — gated like test_skeleton.py (the CI ``orchestrator``
job, which provisions Postgres). thread_id == DBOS workflow_id == run_id, the
single-identifier convention from runner.py (CL-209 refinement-2).

The DBOS internal-state query uses ``DBOSClient`` (Context7-confirmed surface:
``list_workflows(status=...)`` -> objects with ``.workflow_id``). Exact DBOS
2.x kwarg/attribute names are validated on first run; a mismatch is an
infrastructure failure (no commit), not a composition divergence.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import warnings
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("langgraph")

import psycopg  # noqa: E402 — imported after the dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — Landmine 2 DBOS+langgraph test skipped",
)

_WORKER = Path(__file__).parent / "_langgraph_step_resume_worker.py"
# node_b's crash window (kept in sync with the worker constant).
_CRASH_WINDOW_SECONDS = 6


def _probe_count(dsn: str, thread_id: str, node_label: str) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM _landgraph_node_probe "
            "WHERE thread_id = %s AND node_label = %s",
            (thread_id, node_label),
        ).fetchone()
    return int(row[0]) if row else 0


def _wait_for_probe(
    dsn: str, thread_id: str, node_label: str, min_count: int, timeout: float
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _probe_count(dsn, thread_id, node_label) >= min_count:
            return
        time.sleep(0.5)
    raise AssertionError(
        f"probe '{node_label}' did not reach count {min_count} within {timeout}s"
    )


def _checkpoint_ids(dsn: str, thread_id: str) -> set[str]:
    """langgraph PostgresSaver checkpoint ids for a thread (append-only)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s",
            (thread_id,),
        ).fetchall()
    return {str(r[0]) for r in rows}


def _dbos_pending_workflow_ids(dsn: str) -> set[str]:
    """DBOS workflow ids currently in PENDING status (DBOSClient API)."""
    from dbos import DBOSClient

    client = DBOSClient(dsn)
    return {str(wf.workflow_id) for wf in client.list_workflows(status="PENDING")}


def test_dbos_step_resume_after_simulated_crash() -> None:
    """Landmine 2 — a DBOS step wrapping a langgraph invocation, SIGKILLed
    mid-node, replays without double-executing the already-completed node.

    Three sharpened assertions (CL-209 refinement 1):
      - the DBOS step is observed PENDING between crash and resume;
      - the pre-crash checkpointer state is preserved across the replay;
      - node_a (outside the crash) runs exactly once; node_b (the crashed
        node) runs exactly twice — crash attempt + replay.
    """
    dsn = os.environ["DATABASE_URL"]
    run_id = str(uuid4())  # == DBOS workflow_id == langgraph thread_id

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _landgraph_node_probe ("
            "id serial PRIMARY KEY, thread_id text, node_label text, "
            "at timestamptz DEFAULT now())"
        )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        # Launch 1: run until node_b enters its crash window, then SIGKILL.
        proc1 = subprocess.Popen([sys.executable, str(_WORKER), dsn, run_id])
        try:
            _wait_for_probe(dsn, run_id, "node_b", min_count=1, timeout=60)
            pre_crash_ckpts = _checkpoint_ids(dsn, run_id)
        finally:
            proc1.kill()
        proc1.wait(timeout=15)

        # Between crash and resume — is the DBOS step's workflow PENDING?
        step_pending = run_id in _dbos_pending_workflow_ids(dsn)

        # Launch 2: DBOS recovery re-runs the PENDING step.
        proc2 = subprocess.Popen([sys.executable, str(_WORKER), dsn, run_id])
        try:
            _wait_for_probe(dsn, run_id, "node_b", min_count=2, timeout=120)
            # Grace: let node_b finish its crash-window sleep so langgraph
            # writes node_b's checkpoint before post-resume capture.
            time.sleep(_CRASH_WINDOW_SECONDS + 4)
            post_resume_ckpts = _checkpoint_ids(dsn, run_id)
        finally:
            proc2.kill()
            proc2.wait(timeout=15)

    node_a_count = _probe_count(dsn, run_id, "node_a")
    node_b_count = _probe_count(dsn, run_id, "node_b")
    duplicate_side_effects = node_a_count != 1

    print("LANDMINE2_DBOS_STEP_PENDING_OBSERVED:", step_pending)
    print(
        "LANDMINE2_CHECKPOINTER_STATE_AT_STEP_BOUNDARY:",
        "pre_crash=", sorted(pre_crash_ckpts),
        "post_resume=", sorted(post_resume_ckpts),
    )
    print(
        "LANDMINE2_NODE_EXECUTION_COUNT:",
        "node_a=", node_a_count, "node_b=", node_b_count,
    )
    print(
        "LANDMINE2_DUPLICATE_SIDE_EFFECTS:", duplicate_side_effects,
        "(node_a expected exactly 1 execution)",
    )
    print("LANDMINE2_WARNINGS:", [str(w.message) for w in caught])

    # --- three sharpened assertions (BORING = all pass) ----------------------
    assert step_pending, (
        "DBOS step's workflow was not observed PENDING between crash and "
        "resume — crash window misaligned with the step boundary"
    )
    assert pre_crash_ckpts <= post_resume_ckpts, (
        "pre-crash langgraph checkpoints were not preserved across the DBOS "
        "replay — checkpointer state drift"
    )
    assert node_a_count == 1, (
        f"node_a executed {node_a_count}x — a node OUTSIDE the killed step "
        "was double-executed: DBOS replay caused langgraph re-execution"
    )
    assert node_b_count == 2, (
        f"node_b executed {node_b_count}x — expected exactly 2 "
        "(crash attempt + replay)"
    )
