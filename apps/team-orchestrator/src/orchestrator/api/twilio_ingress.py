"""Twilio inbound ingress endpoint (VT-3.3a).

Deterministic ingress ONLY (Pillar 1): verify the internal secret, derive a
DBOS workflow_id from the Twilio MessageSid (exactly-once), start the webhook
workflow. No reasoning, no classification, no LLM.

Path 2: team-web (VT-3.3b) verifies the Twilio signature and resolves the
tenant, then calls this endpoint with INTERNAL_API_SECRET + tenant_id + the
raw Twilio fields. This endpoint trusts only INTERNAL_API_SECRET.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from dbos import DBOS, SetWorkflowID
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from orchestrator.graph import get_pool
from orchestrator.runner import webhook_pipeline_run
from orchestrator.types import WebhookEvent

logger = logging.getLogger(__name__)
router = APIRouter()

_CALLBACK_STATES = {"delivered", "read", "failed", "undelivered"}


class TwilioIngressBody(BaseModel):
    """Request body forwarded by team-web (VT-3.3b)."""

    tenant_id: UUID
    twilio_fields: dict[str, Any]


def _verify_internal_secret(provided: str | None) -> bool:
    """Constant-time compare against INTERNAL_API_SECRET (Pillar 8 — no bespoke crypto)."""
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _record_inbound(message_sid: str, tenant_id: UUID) -> bool:
    """Insert the MessageSid; return True if newly inserted, False if duplicate."""
    with get_pool().connection() as conn:
        cur = conn.execute(
            "INSERT INTO twilio_inbound_events (message_sid, tenant_id) "
            "VALUES (%s, %s) ON CONFLICT (message_sid) DO NOTHING",
            (message_sid, str(tenant_id)),
        )
        return cur.rowcount == 1


def _build_event(fields: dict[str, Any], dupe: bool) -> WebhookEvent:
    callback_state = fields.get("MessageStatus")
    is_callback = callback_state in _CALLBACK_STATES
    return WebhookEvent(
        body=str(fields.get("Body", "")),
        sender_phone=str(fields.get("From", "")),
        message_type="status_callback" if is_callback else "inbound_message",
        twilio_message_sid=fields.get("MessageSid"),
        status_callback_state=callback_state if is_callback else None,
        dupe_status=dupe,
        num_media=int(fields.get("NumMedia", 0) or 0),
        media_url_0=fields.get("MediaUrl0"),
    )


@router.post("/api/orchestrator/twilio-ingress")
def twilio_ingress(
    body: TwilioIngressBody,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Start a durable DBOS webhook workflow for an inbound Twilio message."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="invalid internal secret")

    message_sid = str(body.twilio_fields.get("MessageSid", ""))
    workflow_id = f"twilio_inbound_{message_sid}"
    # Deterministic run_id (pipeline_runs PK): same MessageSid -> same run_id.
    run_id = str(uuid5(NAMESPACE_URL, message_sid))

    try:
        dupe = not _record_inbound(message_sid, body.tenant_id)
        event = _build_event(body.twilio_fields, dupe)
        with SetWorkflowID(workflow_id):
            DBOS.start_workflow(
                webhook_pipeline_run,
                str(body.tenant_id),
                run_id,
                event.model_dump(),
            )
    except Exception:
        # Pillar 7: never return 5xx for an application error — a 5xx would
        # trigger a Twilio retry and duplicate processing. Log and 200.
        logger.exception("twilio-ingress: workflow start failed sid=%s", message_sid)
        return {"workflow_id": workflow_id, "run_id": run_id, "status": "error_logged"}

    return {"workflow_id": workflow_id, "run_id": run_id, "status": "accepted"}
