"""VT-222 Drive Push notification webhook (per CL-421).

POST /api/orchestrator/integrations/sheet/drive_push

Google Drive sends:
  X-Goog-Channel-ID:      our registered channel_id
  X-Goog-Channel-Token:   our verify-only secret (set on register)
  X-Goog-Resource-State:  'sync' (initial ping) | 'update' (file change)
  X-Goog-Resource-ID:     opaque Drive resource id

Privacy: no body data is logged. Channel_token is constant-time compared
against tenant_drive_channels. Token mismatch returns 401 BEFORE any DB
write.
"""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime
from typing import Annotated

from dbos import DBOS
from fastapi import APIRouter, Header, Request, Response

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/orchestrator/integrations/sheet/drive_push")
async def drive_push(
    request: Request,
    x_goog_channel_id: Annotated[
        str | None, Header(alias="X-Goog-Channel-ID")
    ] = None,
    x_goog_channel_token: Annotated[
        str | None, Header(alias="X-Goog-Channel-Token")
    ] = None,
    x_goog_resource_state: Annotated[
        str | None, Header(alias="X-Goog-Resource-State")
    ] = None,
    x_goog_resource_id: Annotated[
        str | None, Header(alias="X-Goog-Resource-ID")
    ] = None,
) -> Response:
    """Drive notification handler.

    On `sync` (Drive's initial post-register handshake): 200 OK.
    On `update`: verify channel_token; enqueue delta pull workflow.
    """
    if not x_goog_channel_id or not x_goog_channel_token:
        return Response(status_code=400, content="missing channel headers")

    # Look up the channel BEFORE any state branching so token mismatch
    # is rejected in every case (sync + update).
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, connector_id, resource_id, channel_token "
            "FROM tenant_drive_channels WHERE channel_id = %s",
            (x_goog_channel_id,),
        )
        row = cur.fetchone()

    if row is None:
        # Unknown channel; do not leak which side is wrong.
        return Response(status_code=401, content="invalid channel")

    stored_token = (
        row["channel_token"] if isinstance(row, dict) else row[3]
    )
    if not hmac.compare_digest(stored_token, x_goog_channel_token):
        return Response(status_code=401, content="invalid channel token")

    tenant_id = str(row["tenant_id"] if isinstance(row, dict) else row[0])
    connector_id = str(
        row["connector_id"] if isinstance(row, dict) else row[1]
    )
    resource_id = str(row["resource_id"] if isinstance(row, dict) else row[2])

    if x_goog_resource_state == "sync":
        # Initial handshake — Drive sends this once after register.
        return Response(status_code=200)

    # On any change event, mark last_notification_at + enqueue delta pull.
    now = datetime.now(UTC)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE tenant_drive_channels SET last_notification_at = %s "
            "WHERE channel_id = %s",
            (now, x_goog_channel_id),
        )

    from orchestrator.integrations.drive_push import pull_sheet_delta_workflow

    try:
        DBOS.start_workflow(
            pull_sheet_delta_workflow,
            tenant_id,
            connector_id,
            resource_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "drive_push: enqueue pull_sheet_delta_workflow failed "
            "(tenant=%s, channel=%s)",
            tenant_id,
            x_goog_channel_id,
        )

    return Response(status_code=200)
