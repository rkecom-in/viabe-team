"""Twilio inbound ingress endpoint (VT-3.3a/b + VT-3.3a-fix-1).

Deterministic ingress ONLY (Pillar 1): verify the internal secret, reject a
malformed payload, resolve the tenant, rate-limit, then start the durable
webhook workflow. No reasoning, no classification.

Dedup detection and event construction live INSIDE webhook_pipeline_run (the
durable boundary) — see PR-fix-1 / CL-72. This handler only validates and
starts the workflow.
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
from orchestrator.utils.phone_token import hash_phone

logger = logging.getLogger(__name__)
router = APIRouter()

# Fixed-window rate limits (per minute).
_PER_TENANT_LIMIT = 30
_WORKSPACE_LIMIT = 500
# All-zeros sentinel tenant_id for the workspace-wide bucket (see migration 013).
_WORKSPACE_SENTINEL = "00000000-0000-0000-0000-000000000000"


class TwilioIngressBody(BaseModel):
    """Request body forwarded by team-web (VT-3.3b) — raw Twilio fields only."""

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


# DBOS workflow statuses for a prior workflow that is still progressing — the
# rest (ERROR / CANCELLED / MAX_RECOVERY_ATTEMPTS_EXCEEDED) are terminally dead.
# Verified live against dbos 2.x WorkflowStatusString (CL-96).
_RECOVERING_STATUSES = frozenset({"PENDING", "ENQUEUED", "DELAYED"})


def _ingress_reason(prior: Any) -> str:
    """Classify a (re)delivered MessageSid by the prior workflow's DBOS status.

    ``DBOS.start_workflow`` no-ops on a known workflow_id (idempotency) — this
    only *reports* the prior workflow's state, it never re-triggers it:

    - None    -> ``started``  — brand-new MessageSid.
    - SUCCESS -> ``dupe``     — true Twilio retry of an already-handled message.
    - PENDING / ENQUEUED / DELAYED -> ``recovering`` — workflow still in flight;
      DBOS recovery / the queue will carry it to completion.
    - ERROR / CANCELLED / MAX_RECOVERY_ATTEMPTS_EXCEEDED -> ``terminal_failure``
      — the prior workflow is dead and ``start_workflow`` no-ops, so nothing
      recovers it (Pillar 7 — do not report a dead run as ``recovering``).
    """
    if prior is None:
        return "started"
    if prior.status == "SUCCESS":
        return "dupe"
    if prior.status in _RECOVERING_STATUSES:
        return "recovering"
    return "terminal_failure"


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


@router.post("/api/orchestrator/twilio-ingress")
def twilio_ingress(
    body: TwilioIngressBody,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Validate, resolve the tenant, rate-limit, and start the webhook workflow.

    Returns ``{workflow_id, reason}`` — reason is one of: started, dupe,
    recovering, terminal_failure, unknown_sender, rate_limit_exceeded,
    error_logged. 403 on a bad secret;
    400 on a malformed payload (missing MessageSid). After validation, never
    5xx for an application error (Pillar 7).
    """
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="invalid internal secret")

    # C3 fix (CL-73): reject a malformed payload before any side-effects.
    # Twilio always sends a MessageSid; a missing one is a team-web forwarder
    # bug — surface it so team-web can log/alert rather than collapsing every
    # malformed request into one workflow_id.
    fields = body.twilio_fields
    message_sid = str(fields.get("MessageSid", ""))
    if not message_sid:
        raise HTTPException(status_code=400, detail="missing MessageSid")

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
        # Read-only pre-check (no side-effect): the prior workflow's DBOS status
        # for this MessageSid, if any. Dedup itself happens inside the workflow.
        prior = DBOS.get_workflow_status(workflow_id)
        with SetWorkflowID(workflow_id):
            DBOS.start_workflow(webhook_pipeline_run, tenant_id, run_id, fields)
        return {
            "workflow_id": workflow_id,
            "reason": _ingress_reason(prior),
        }
    except Exception:
        # Pillar 7: never 5xx for an application error.
        logger.exception("twilio-ingress: failed sid=%s", message_sid)
        return {"workflow_id": None, "reason": "error_logged"}
