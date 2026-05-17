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

from dbos import DBOS, SetWorkflowID, SetWorkflowTimeout
from psycopg.types.json import Jsonb

from dbos_config import WORKFLOW_TIMEOUT_SECONDS
from orchestrator.direct_handlers import HANDLERS
from orchestrator.graph import OrchestratorState, get_compiled_graph, get_pool
from orchestrator.pre_filter_gate import pre_filter
from orchestrator.state import new_subscriber_state
from orchestrator.types import WebhookEvent
from orchestrator.utils.phone_token import hash_phone


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
    with SetWorkflowTimeout(WORKFLOW_TIMEOUT_SECONDS), SetWorkflowID(run_id):
        return pipeline_run(tenant_id, run_id, inbound)


# --- VT-3.3a: Twilio inbound webhook ingress pipeline ------------------------
#
# A separate workflow from pipeline_run (VT-3.1's LangGraph-substrate smoke
# path) — the ingress pipeline is ingress -> Pre-Filter Gate -> direct handler.
# pipeline_run is left untouched so VT-3.1's synthetic tests keep passing.


@DBOS.step()
def open_webhook_run(tenant_id: str, run_id: str, trigger_payload: dict) -> None:
    """Record the inbound run in pipeline_runs. Idempotent — a redelivered
    MessageSid maps to the same run_id. trigger_payload is phone-tokenised."""
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs "
            "(id, tenant_id, run_type, status, trigger_payload) "
            "VALUES (%s, %s, 'twilio_inbound', 'running', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (run_id, tenant_id, Jsonb(trigger_payload)),
        )


@DBOS.step()
def record_webhook_received(tenant_id: str, run_id: str, envelope: dict) -> None:
    """Write the webhook_received step_record to pipeline_steps. The envelope
    is phone-tokenised — no plaintext PII (Pillar 3 / Pillar 7)."""
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_steps "
            "(run_id, tenant_id, step_index, step_kind, input_envelope) "
            "VALUES (%s, %s, 0, 'webhook_received', %s)",
            (run_id, tenant_id, Jsonb(envelope)),
        )


@DBOS.step()
def close_webhook_run(run_id: str, status: str) -> None:
    """Mark the inbound run finished. Idempotent."""
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status = %s, ended_at = now() WHERE id = %s",
            (status, run_id),
        )


@DBOS.workflow()
def webhook_pipeline_run(
    tenant_id: str, run_id: str, event_dict: dict
) -> dict[str, Any]:
    """Durable inbound-webhook pipeline: ingress -> Pre-Filter Gate -> handler.

    Started by /api/orchestrator/twilio-ingress with a workflow_id derived from
    the Twilio MessageSid (DBOS exactly-once idempotency).
    """
    event = WebhookEvent(**event_dict)
    state = new_subscriber_state(UUID(tenant_id), UUID(run_id))

    # Phone-tokenise before anything is persisted (Pillar 3 / Pillar 7).
    tokenised = dict(event_dict)
    if event.sender_phone:
        tokenised["sender_phone"] = hash_phone(event.sender_phone)

    open_webhook_run(tenant_id, run_id, tokenised)
    record_webhook_received(tenant_id, run_id, tokenised)

    result = pre_filter(event, state)
    handler_name: str | None = None
    if result.kind == "direct_handler":
        handler_name = result.handler_name
        HANDLERS[handler_name](event, state)

    close_webhook_run(run_id, "completed")
    return {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "routed": result.kind,
        "handler": handler_name,
    }
