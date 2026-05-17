"""Subprocess helper for the VT-3.2 DBOS auto-resume test.

Run as: ``python _transition_resume_worker.py <db_url> <workflow_id> <tenant_id>``

Runs a workflow that applies a `signup` transition (a @DBOS.step), then pauses.
The test SIGKILLs the process during the pause, then spawns it again — the
second launch's DBOS recovery resumes the workflow; the completed
apply_transition step is cached, so the transition is applied exactly once.

No ``test_`` prefix — pytest does not collect this file.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

_DB_URL = sys.argv[1] if len(sys.argv) > 3 else ""
_WORKFLOW_ID = sys.argv[2] if len(sys.argv) > 3 else ""
_TENANT_ID = sys.argv[3] if len(sys.argv) > 3 else ""
os.environ["TEAM_SUPABASE_DB_URL"] = _DB_URL
os.environ.setdefault("DATABASE_URL", _DB_URL)

import psycopg  # noqa: E402 — imported after sys.path / env setup
from dbos import DBOS, SetWorkflowID  # noqa: E402

from dbos_config import launch_dbos  # noqa: E402
from orchestrator.state import new_subscriber_state  # noqa: E402
from orchestrator.transitions import apply_transition  # noqa: E402


@DBOS.step()
def _probe(workflow_id: str) -> None:
    with psycopg.connect(_DB_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO _resume_probe (workflow_id, step_label) "
            "VALUES (%s, 'after_transition')",
            (workflow_id,),
        )


@DBOS.workflow()
def _transition_workflow(workflow_id: str, tenant_id: str) -> str:
    state = new_subscriber_state(UUID(tenant_id))
    apply_transition(state, "signup", {})  # @DBOS.step — checkpointed
    time.sleep(8)  # interruption window — the test SIGKILLs the process here
    _probe(workflow_id)
    return "done"


def main() -> None:
    launch_dbos()  # recovers any workflow left PENDING by an earlier crash
    with SetWorkflowID(_WORKFLOW_ID):
        DBOS.start_workflow(_transition_workflow, _WORKFLOW_ID, _TENANT_ID)
    time.sleep(40)


if __name__ == "__main__":
    main()
