"""DBOS workflow entry point for an orchestrator pipeline run (VT-3.1).

Pillar 1: no reasoning here — the steps only persist run state and drive the
LangGraph substrate. Pillar 8: one workflow, one substrate.

Each ``@DBOS.step`` is a durable checkpoint. DBOS auto-resumes the workflow
from the last completed step after a crash. Steps are written idempotently so
recovery is safe.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from dbos import DBOS

from dbos_config import WORKFLOW_TIMEOUT_SECONDS
from orchestrator.graph import OrchestratorState, get_compiled_graph, get_pool


@DBOS.step()
def open_run(tenant_id: str, run_id: str) -> None:
    """Record the run as started. Idempotent (ON CONFLICT) so recovery is safe."""
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running') "
            "ON CONFLICT (id) DO NOTHING",
            (run_id, tenant_id),
        )


@DBOS.step()
def invoke_graph(tenant_id: str, run_id: str, inbound: str) -> list[str]:
    """Run the LangGraph substrate for this run. thread_id == run_id."""
    state: OrchestratorState = {
        "tenant_id": UUID(tenant_id),
        "run_id": UUID(run_id),
        "history": [inbound],
    }
    result = get_compiled_graph().invoke(
        state, config={"configurable": {"thread_id": run_id}}
    )
    return list(result["history"])


@DBOS.step()
def close_run(run_id: str) -> None:
    """Mark the run completed. Idempotent."""
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status = 'completed', ended_at = now() "
            "WHERE id = %s",
            (run_id,),
        )


@DBOS.workflow()
def pipeline_run(tenant_id: str, run_id: str, inbound: str) -> dict[str, Any]:
    """Durable orchestrator pipeline run — three checkpointed steps."""
    open_run(tenant_id, run_id)
    history = invoke_graph(tenant_id, run_id, inbound)
    close_run(run_id)
    return {"tenant_id": tenant_id, "run_id": run_id, "history": history}


def run_pipeline(tenant_id: str, run_id: str, inbound: str) -> dict[str, Any]:
    """Run ``pipeline_run`` durably, keyed on ``run_id`` for idempotency.

    The 6-minute timeout and run_id-as-workflow-id are applied here: invoking
    twice with the same run_id returns the first run's result without
    re-executing (DBOS idempotency).
    """
    with DBOS.SetWorkflowTimeout(WORKFLOW_TIMEOUT_SECONDS), DBOS.SetWorkflowID(run_id):
        return pipeline_run(tenant_id, run_id, inbound)
