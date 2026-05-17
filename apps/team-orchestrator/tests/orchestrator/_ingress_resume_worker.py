"""Subprocess helper for the VT-3.3a DBOS auto-resume test.

Run as: ``python _ingress_resume_worker.py <db_url> <workflow_id> <tenant_id>``

Runs a workflow that performs the ingress step (open_webhook_run), pauses, then
probes. The test SIGKILLs the process during the pause and respawns it — DBOS
recovery resumes the workflow; the completed ingress step is cached.

No ``test_`` prefix — pytest does not collect this file.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

_DB_URL = sys.argv[1] if len(sys.argv) > 3 else ""
_WORKFLOW_ID = sys.argv[2] if len(sys.argv) > 3 else ""
_TENANT_ID = sys.argv[3] if len(sys.argv) > 3 else ""
os.environ["TEAM_SUPABASE_DB_URL"] = _DB_URL
os.environ.setdefault("DATABASE_URL", _DB_URL)

import psycopg  # noqa: E402 — imported after sys.path / env setup
from dbos import DBOS, SetWorkflowID  # noqa: E402

from dbos_config import launch_dbos  # noqa: E402
from orchestrator.runner import open_webhook_run  # noqa: E402


@DBOS.step()
def _probe(workflow_id: str) -> None:
    with psycopg.connect(_DB_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO _resume_probe (workflow_id, step_label) "
            "VALUES (%s, 'ingress_done')",
            (workflow_id,),
        )


@DBOS.workflow()
def _ingress_resume_workflow(workflow_id: str, tenant_id: str) -> str:
    run_id = str(uuid5(NAMESPACE_URL, workflow_id))
    open_webhook_run(tenant_id, run_id, {"probe": True})  # @DBOS.step
    time.sleep(8)  # interruption window — the test SIGKILLs here
    _probe(workflow_id)
    return "done"


def main() -> None:
    launch_dbos()  # recovers any workflow left PENDING by an earlier crash
    with SetWorkflowID(_WORKFLOW_ID):
        DBOS.start_workflow(_ingress_resume_workflow, _WORKFLOW_ID, _TENANT_ID)
    time.sleep(40)


if __name__ == "__main__":
    main()
