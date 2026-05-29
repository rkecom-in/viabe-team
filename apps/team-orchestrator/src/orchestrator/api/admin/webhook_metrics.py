"""VT-226 — admin endpoint to enqueue webhook_metrics writes.

Called by team-web's edge webhook routes (fire-and-forget). Gated by
`TEAM_ADMIN_API_TOKEN` per VT-224 pattern. Enqueues
`write_webhook_metric_workflow` via DBOS for retry safety.

Returns 202 immediately; the actual DB write happens asynchronously.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from orchestrator.api.admin._auth import log_admin_call
from orchestrator.api.admin._rate_limit import RateLimitedAdmin
from orchestrator.observability.webhook_metrics_writer import (
    write_webhook_metric_workflow,
)

router = APIRouter()


class RecordBody(BaseModel):
    source: str
    event: str
    message_sid: str | None = None
    source_ip: str
    response_status: int


@router.post("/api/orchestrator/admin/webhook_metrics/record")
def admin_record_webhook_metric(
    body: RecordBody,
    request: Request,
    fp: RateLimitedAdmin,
) -> dict[str, Any]:
    """Enqueue a webhook_metrics row write via DBOS workflow.

    Returns 202 + workflow handle id.
    """
    try:
        handle = DBOS.start_workflow(
            write_webhook_metric_workflow,
            source=body.source,
            event=body.event,
            message_sid=body.message_sid,
            source_ip=body.source_ip,
            response_status=body.response_status,
        )
    except Exception as exc:  # noqa: BLE001
        log_admin_call(
            request=request,
            endpoint="POST /api/orchestrator/admin/webhook_metrics/record",
            response_status=500,
            error_message=repr(exc)[:200],
        )
        raise HTTPException(500, f"enqueue failed: {exc}") from exc

    log_admin_call(
        request=request,
        endpoint="POST /api/orchestrator/admin/webhook_metrics/record",
        response_status=202,
    )
    return {"status": "queued", "workflow_id": getattr(handle, "workflow_id", "unknown")}
