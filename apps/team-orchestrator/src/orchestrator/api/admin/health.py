"""VT-224 admin health — integration agent substrate summary."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from orchestrator.api.admin._auth import log_admin_call
from orchestrator.api.admin._rate_limit import RateLimitedAdmin
from orchestrator.graph import get_pool

router = APIRouter()


@router.get("/api/orchestrator/admin/health/integration_agent")
def admin_integration_health(
    request: Request,
    fp: RateLimitedAdmin,
) -> dict[str, Any]:
    """Returns active oauth tokens count + last-ingestion-at per tenant."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM tenant_oauth_tokens "
            "WHERE refresh_token_encrypted IS NOT NULL"
        )
        oauth_row = cur.fetchone()

        cur.execute(
            """
            SELECT tenant_id::text AS tenant_id,
                   connector_id,
                   last_sync_at,
                   last_status
            FROM tenant_connector_status
            WHERE last_sync_at IS NOT NULL
            ORDER BY last_sync_at DESC
            LIMIT 100
            """
        )
        ingestion_rows = cur.fetchall()

    active_oauth_tokens = (
        oauth_row["n"] if isinstance(oauth_row, dict) else oauth_row[0]
    ) if oauth_row else 0

    last_ingestion: list[dict[str, Any]] = []
    for r in ingestion_rows:
        if isinstance(r, dict):
            last_ingestion.append({
                "tenant_id": r["tenant_id"],
                "connector_id": r["connector_id"],
                "last_sync_at": r["last_sync_at"].isoformat() if r["last_sync_at"] else None,
                "last_status": r["last_status"],
            })
        else:
            last_ingestion.append({
                "tenant_id": r[0],
                "connector_id": r[1],
                "last_sync_at": r[2].isoformat() if r[2] else None,
                "last_status": r[3],
            })

    result = {
        "active_oauth_tokens": int(active_oauth_tokens),
        "active_drive_channels": 0,  # VT-222 plumbs real count
        "last_ingestion": last_ingestion,
    }
    log_admin_call(
        request=request,
        endpoint="GET /api/orchestrator/admin/health/integration_agent",
        response_status=200,
    )
    return result
