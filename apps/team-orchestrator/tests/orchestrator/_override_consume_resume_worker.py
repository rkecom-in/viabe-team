"""Subprocess helper for the VT-374 N2 override re-apply kill-and-recover test.

Run as: ``python _override_consume_resume_worker.py <db_url> <workflow_id> <tenant_id> <run_id>``

A DBOS workflow that, inside a ``@DBOS.step``, consumes the pre-registered
(agent_dispatch, compose_drafts) override for ``<run_id>`` via the real
``run_control.consume_override``, records the consumed override id into a probe table,
and then sleeps (the crash window). The parent test SIGKILLs the process DURING the
sleep — the consume txn has already committed, the workflow is left PENDING. A second
launch lets DBOS recovery re-enter the workflow body; the step re-executes and
re-consumes the SAME override (N2: the ``consumed_run_id = run_id`` arm re-applies the
pin for the recovering run, NOT a fresh consume).

The consume step is deliberately NOT memoised for its DB read-back: each execution runs
``consume_override`` again, so the probe table accumulates ONE row per execution and the
test asserts the SAME override id appears on both the crash attempt and the replay.

``run_id`` is the run identity ``consume_override`` matches on; the DBOS workflow_id is
the caller-supplied ``<workflow_id>`` so the parent can address the workflow across the
crash. No ``test_`` prefix — pytest does not collect this file.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

_DB_URL = sys.argv[1] if len(sys.argv) > 4 else ""
_WORKFLOW_ID = sys.argv[2] if len(sys.argv) > 4 else ""
_TENANT_ID = sys.argv[3] if len(sys.argv) > 4 else ""
_RUN_ID = sys.argv[4] if len(sys.argv) > 4 else ""

os.environ["TEAM_SUPABASE_DB_URL"] = _DB_URL
os.environ.setdefault("DATABASE_URL", _DB_URL)

import psycopg  # noqa: E402
from dbos import DBOS, SetWorkflowID  # noqa: E402

from dbos_config import launch_dbos  # noqa: E402

# The crash window: long enough for the parent to observe the consume + SIGKILL, short
# enough that the test stays brisk.
_CRASH_WINDOW_SECONDS = 8


def _consume_and_probe(tenant_id: str, run_id: str) -> str | None:
    """Consume the override for this run and record the result (one row per execution).

    Called DIRECTLY in the workflow body (NOT a ``@DBOS.step``) — mirroring the real
    coordinator's ``_consume_execute_override`` call site (coordinator.py:537), which is
    plain workflow-body code. Because it is NOT a memoised step, DBOS recovery RE-ENTERS
    the body and re-runs this consume (N2): the recovering run re-applies the SAME
    override via the A5 ``consumed_run_id = run_id`` arm. The probe row count is how the
    test detects the re-apply across the crash.
    """
    from orchestrator.graph import get_pool
    from orchestrator.run_control import consume_override

    with get_pool().connection() as conn:
        override = consume_override(
            conn,
            tenant_id=tenant_id,
            workflow_kind="agent_dispatch",
            step_name="compose_drafts",
            run_id=run_id,
        )
    consumed_id = str(override.id) if override is not None else None
    with psycopg.connect(_DB_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO _vt374_consume_probe (run_id, override_id) VALUES (%s, %s)",
            (run_id, consumed_id),
        )
    return consumed_id


@DBOS.step()
def _park_step() -> None:
    """A durable step whose checkpoint is the crash boundary — SIGKILL during its sleep
    leaves the workflow PENDING (the consume above already committed)."""
    time.sleep(_CRASH_WINDOW_SECONDS)


@DBOS.workflow()
def _override_consume_workflow(tenant_id: str, run_id: str) -> str:
    _consume_and_probe(tenant_id, run_id)  # plain body — re-runs on recovery (N2)
    _park_step()  # crash window: the test SIGKILLs here, leaving the workflow PENDING
    return "completed"


def main() -> None:
    launch_dbos()  # recovers any workflow left PENDING by an earlier crash
    with SetWorkflowID(_WORKFLOW_ID):
        DBOS.start_workflow(_override_consume_workflow, _TENANT_ID, _RUN_ID)
    time.sleep(60)  # stay alive while the run executes / recovery completes


if __name__ == "__main__":
    main()
