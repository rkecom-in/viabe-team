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
from orchestrator.db import tenant_connection
from orchestrator.direct_handlers import HANDLERS
from orchestrator.graph import OrchestratorState, get_compiled_graph
from orchestrator.owner_inputs import run_extraction_for_event
from orchestrator.pre_filter_gate import pre_filter
from orchestrator.state import new_subscriber_state
from orchestrator.types import WebhookEvent
from orchestrator.utils.phone_token import hash_phone


@DBOS.step()
def open_run(tenant_id: str, run_id: str) -> None:
    """Record the run as started. Idempotent (ON CONFLICT) so recovery is safe."""
    with tenant_connection(tenant_id) as conn:
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
def close_run(tenant_id: str, run_id: str) -> None:
    """Mark the run completed. Idempotent.

    tenant_id is required so the UPDATE runs under tenant_connection — under
    RLS the WHERE id = %s is scoped by the USING clause, so without the GUC
    set the UPDATE is a silent no-op (CL-71).
    """
    with tenant_connection(tenant_id) as conn:
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
    close_run(tenant_id, run_id)
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
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs "
            "(id, tenant_id, run_type, status, trigger_payload) "
            "VALUES (%s, %s, 'twilio_inbound', 'running', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (run_id, tenant_id, Jsonb(trigger_payload)),
        )


@DBOS.step()
def record_webhook_received(tenant_id: str, run_id: str, envelope: dict) -> None:
    """Write the webhook_received step_record (step_index=0) to pipeline_steps.

    The envelope is phone-tokenised — no plaintext PII (Pillar 3 / Pillar 7).

    Idempotency is provided by the DBOS workflow-id boundary for COMPLETED
    steps. A crash between the SQL commit and DBOS recording the step causes
    re-execution on workflow resume — hence the ON CONFLICT (run_id, step_index)
    DO NOTHING clause. Migration 014's UNIQUE (run_id, step_index) constraint
    makes ON CONFLICT well-defined.
    """
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "INSERT INTO pipeline_steps "
            "(run_id, tenant_id, step_index, step_kind, input_envelope) "
            "VALUES (%s, %s, 0, 'webhook_received', %s) "
            "ON CONFLICT (run_id, step_index) DO NOTHING",
            (run_id, tenant_id, Jsonb(envelope)),
        )


@DBOS.step()
def close_webhook_run(tenant_id: str, run_id: str, status: str) -> None:
    """Mark the inbound run finished. Idempotent.

    tenant_id is required so the UPDATE runs under tenant_connection — the
    WHERE id = %s is scoped by the RLS USING clause, so without the GUC set
    the UPDATE silently affects 0 rows (CL-71).
    """
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status = %s, ended_at = now() WHERE id = %s",
            (status, run_id),
        )


# Twilio status-callback states (vs a plain inbound message).
_CALLBACK_STATES = {"delivered", "read", "failed", "undelivered"}


def build_webhook_event(fields: dict[str, Any], dupe_status: bool) -> WebhookEvent:
    """Construct a WebhookEvent from raw Twilio fields. Plain helper (no LLM)."""
    callback_state = fields.get("MessageStatus")
    is_callback = callback_state in _CALLBACK_STATES
    return WebhookEvent(
        body=str(fields.get("Body", "")),
        sender_phone=str(fields.get("From", "")),
        message_type="status_callback" if is_callback else "inbound_message",
        twilio_message_sid=fields.get("MessageSid"),
        status_callback_state=callback_state if is_callback else None,
        dupe_status=dupe_status,
        num_media=int(fields.get("NumMedia", 0) or 0),
        media_url_0=fields.get("MediaUrl0"),
    )


@DBOS.step()
def record_inbound_message_sid(tenant_id: str, message_sid: str) -> bool:
    """Record the MessageSid in the idempotency ledger — the FIRST workflow step.

    Returns True if newly inserted, False if already seen. C2 fix (CL-72): this
    runs inside the durable workflow boundary, so a half-completed ingress can
    never leave a row that makes the next attempt look like a duplicate.
    """
    with tenant_connection(tenant_id) as conn:
        cur = conn.execute(
            "INSERT INTO twilio_inbound_events (message_sid, tenant_id) "
            "VALUES (%s, %s) ON CONFLICT (message_sid) DO NOTHING",
            (message_sid, tenant_id),
        )
        return cur.rowcount == 1


@DBOS.step()
def record_brain_pending(tenant_id: str, run_id: str, reason: str) -> None:
    """Record that this run is awaiting the brain (step_index=1, VT-3.4 unwired).

    Idempotency is provided by the DBOS workflow-id boundary for COMPLETED
    steps. A crash between the SQL commit and DBOS recording the step causes
    re-execution on workflow resume — hence the ON CONFLICT (run_id, step_index)
    DO NOTHING clause. Migration 014's UNIQUE (run_id, step_index) constraint
    makes ON CONFLICT well-defined.
    """
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "INSERT INTO pipeline_steps "
            "(run_id, tenant_id, step_index, step_kind, output_envelope) "
            "VALUES (%s, %s, 1, 'awaiting_brain', %s) "
            "ON CONFLICT (run_id, step_index) DO NOTHING",
            (run_id, tenant_id, Jsonb({"reason": reason})),
        )


@DBOS.workflow()
def webhook_pipeline_run(
    tenant_id: str, run_id: str, twilio_fields: dict
) -> dict[str, Any]:
    """Durable inbound-webhook pipeline: dedup -> ingress -> Pre-Filter -> handler.

    Started by /api/orchestrator/twilio-ingress with a workflow_id derived from
    the Twilio MessageSid (DBOS exactly-once idempotency). Dedup detection and
    event construction happen inside this durable boundary (C2 fix, CL-72).
    """
    message_sid = str(twilio_fields.get("MessageSid", ""))
    newly_inserted = record_inbound_message_sid(tenant_id, message_sid)
    event = build_webhook_event(twilio_fields, dupe_status=not newly_inserted)
    state = new_subscriber_state(UUID(tenant_id), UUID(run_id))

    # Redact + phone-tokenise before anything is persisted (Pillar 3 / Pillar 7).
    # Body redaction (Component 0): drop the raw message body from the
    # persisted envelope. Provenance via MessageSid + hashed sender_phone
    # is preserved; content (what the owner said) is not retained. The
    # in-memory ``event`` keeps body intact for the request's lifetime —
    # request-scoped readers (pre_filter, the future owner_inputs
    # extraction writer) consume body from ``event``, NOT from
    # trigger_payload / input_envelope.
    tokenised = event.model_dump()
    tokenised.pop("body", None)
    if event.sender_phone:
        tokenised["sender_phone"] = hash_phone(event.sender_phone)

    open_webhook_run(tenant_id, run_id, tokenised)
    record_webhook_received(tenant_id, run_id, tokenised)

    # VT-146 — owner-input extraction seam. Reads body from the
    # request-scoped ``event`` (NOT from any persisted column; VT-144
    # stripped raw body from trigger_payload / input_envelope), routes
    # it to the classifier in ``orchestrator.owner_inputs`` (which owns
    # the LLM seam — Pillar 1 keeps runner.py deterministic; the LLM
    # call lives behind the writer's boundary), persists only the
    # derived intent / segment / occasion row to ``owner_inputs``.
    # ``run_extraction_for_event`` is best-effort internally —
    # classifier or write failure logs and returns None; the inbound
    # pipeline never breaks. No body leaves this function via
    # persistence; the body text crosses the wire to the classifier
    # only.
    run_extraction_for_event(UUID(tenant_id), UUID(run_id), event)

    result = pre_filter(event, state)
    handler_name: str | None = None
    final_status = "completed"
    if result.kind == "direct_handler":
        handler_name = result.handler_name
        HANDLERS[handler_name](event, state)
    elif result.kind == "brain":
        # VT-3.4 brain not yet wired — record a brain-pending step and mark the
        # run 'escalated' so it is not silently reported as completed (Pillar 7).
        record_brain_pending(tenant_id, run_id, result.reason)
        final_status = "escalated"
    # result.kind == "reject" → observability-only; the run ends clean (completed).

    close_webhook_run(tenant_id, run_id, final_status)
    return {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "routed": result.kind,
        "handler": handler_name,
    }
