"""Subprocess helper for the DBOS auto-resume test (VT-3.1).

Run as: ``python _resume_worker.py <db_url> <workflow_id>``

Defines a representative DBOS workflow with a probe step and an interruption
window. The test spawns this process, SIGKILLs it mid-workflow, then spawns it
again — the second launch's DBOS recovery resumes the workflow from the last
checkpointed step.

The filename has no ``test_`` prefix, so pytest does not collect it.
"""

from __future__ import annotations

import sys
import time

import psycopg
from dbos import DBOS, DBOSConfig, SetWorkflowID

_DB_URL = sys.argv[1] if len(sys.argv) > 2 else ""
_WORKFLOW_ID = sys.argv[2] if len(sys.argv) > 2 else ""


@DBOS.step()
def probe(workflow_id: str, label: str) -> None:
    """Record one step execution.

    A re-run would insert a second row. DBOS's step cache guarantees a
    completed step is not re-executed on recovery — so exactly one row per
    label proves single execution across a crash.
    """
    with psycopg.connect(_DB_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO _resume_probe (workflow_id, step_label) VALUES (%s, %s)",
            (workflow_id, label),
        )


@DBOS.workflow()
def resume_probe_workflow(workflow_id: str) -> str:
    probe(workflow_id, "step1")
    time.sleep(8)  # interruption window — the test SIGKILLs the process here
    probe(workflow_id, "step2")
    return "completed"


def main() -> None:
    config: DBOSConfig = {"name": "team-orchestrator", "database_url": _DB_URL}
    DBOS(config=config)
    DBOS.launch()  # recovers any workflow left PENDING by an earlier crash
    with SetWorkflowID(_WORKFLOW_ID):
        DBOS.start_workflow(resume_probe_workflow, _WORKFLOW_ID)
    time.sleep(40)  # stay alive while the workflow runs / recovery completes


if __name__ == "__main__":
    main()
