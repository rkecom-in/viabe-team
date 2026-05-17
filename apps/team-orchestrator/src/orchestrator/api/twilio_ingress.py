"""Twilio inbound ingress endpoint (VT-3.3a + VT-3.3b).

Deterministic ingress ONLY (Pillar 1): verify the internal secret, resolve the
tenant, rate-limit, derive a DBOS workflow_id from the MessageSid
(exactly-once), start the webhook workflow. No reasoning, no classification.

Path 2 (VT-3.3b): team-web verifies the Twilio signature and forwards the raw
Twilio fields with INTERNAL_API_SECRET. Tenant lookup + rate limiting live
here (Pillar 8 — a single DB-access path), not in team-web.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from dbos import DBOS, SetWorkflowID
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from orchestrator.graph import get_pool
from orchestrator.runner import webhook_pipeline_run
from orchestrator.types import WebhookEvent
from orchestrator.utils.phone_token import hash_phone

logger = logging.getLogger(__name__)
router = APIRouter()

_CALLBACK_STATES = {"delivered", "read", "failed", "undelivered"}

# Fixed-window rate limits (per minute).
_PER_TENANT_LIMIT = 30
_WORKSPACE_LIMIT = 500
# All-zeros sentinel tenant_id for the workspace-wide bucket (see migration 013).
_WORKSPACE_SENTINEL = "00000000-0000-0000-0000-000000000000"


class TwilioIngressBody(BaseModel):
    """Request body forwarded by team-web (VT-3.3b) — raw Twilio fields only.

    The tenant is resolved here, not by team-web.
    """

    twilio_fields: dict[str, Any]


def _verify_internal_secret(provided: str | None) -> bool:
    """Constant-time compare against INTERNAL_API_SECRET (Pillar 8 — no bespoke crypto)."""
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _lookup_tenant(from_phone: str) -> str | None:
    """Resolve a tenant by WhatsApp number. Most recent wins; None if unknown."""
    if not from_phone:
        return None
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT id FROM tenants WHERE whatsapp_number = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (from_phone,),
        ).fetchone()
    return str(row["id"]) if row else None


def _bump_bucket(conn: Any, tenant_id: str, limit: int) -> bool:
    """Atomically increment the current minute bucket. Return True if within limit."""
    row = conn.execute(
        "INSERT INTO rate_limit_buckets (tenant_id, window_start, count) "
        "VALUES (%s, date_trunc('minute', now()), 1) "
        "ON CONFLICT (tenant_id, window_start) "
        "DO UPDATE SET count = rate_limit_buckets.count + 1 "
        "RETURNING count",
        (tenant_id,),
    ).fetchone()
    return row["count"] <= limit


def _within_rate_limits(tenant_id: str) -> bool:
    """Check per-tenant (30/min) and workspace (500/min) inbound rate limits."""
    with get_pool().connection() as conn:
        if not _bump_bucket(conn, tenant_id, _PER_TENANT_LIMIT):
            return False
        return _bump_bucket(conn, _WORKSPACE_SENTINEL, _WORKSPACE_LIMIT)


def _record_inbound(message_sid: str, tenant_id: str) -> bool:
    """Insert the MessageSid; return True if newly inserted, False if duplicate."""
    with get_pool().connection() as conn:
        cur = conn.execute(
            "INSERT INTO twilio_inbound_events (message_sid, tenant_id) "
            "VALUES (%s, %s) ON CONFLICT (message_sid) DO NOTHING",
            (message_sid, tenant_id),
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
    """Resolve the tenant, rate-limit, and start a durable webhook workflow.

    Returns ``{workflow_id, reason}`` — reason is one of: started, dupe,
    unknown_sender, rate_limit_exceeded, error_logged. Never 5xx for an
    application error (Pillar 7).
    """
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="invalid internal secret")

    fields = body.twilio_fields
    message_sid = str(fields.get("MessageSid", ""))
    from_phone = str(fields.get("From", ""))

    try:
        tenant_id = _lookup_tenant(from_phone)
        if tenant_id is None:
            logger.info(
                "twilio-ingress: unknown_sender from=%s sid=%s",
                hash_phone(from_phone) if from_phone else "<empty>",
                message_sid,
            )
            return {"workflow_id": None, "reason": "unknown_sender"}

        if not _within_rate_limits(tenant_id):
            logger.warning(
                "twilio-ingress: rate_limit_exceeded tenant=%s sid=%s",
                tenant_id,
                message_sid,
            )
            return {"workflow_id": None, "reason": "rate_limit_exceeded"}

        workflow_id = f"twilio_inbound_{message_sid}"
        run_id = str(uuid5(NAMESPACE_URL, message_sid))
        dupe = not _record_inbound(message_sid, tenant_id)
        event = _build_event(fields, dupe)
        with SetWorkflowID(workflow_id):
            DBOS.start_workflow(
                webhook_pipeline_run, tenant_id, run_id, event.model_dump()
            )
        return {"workflow_id": workflow_id, "reason": "dupe" if dupe else "started"}
    except Exception:
        # Pillar 7: never 5xx for an application error.
        logger.exception("twilio-ingress: failed sid=%s", message_sid)
        return {"workflow_id": None, "reason": "error_logged"}
