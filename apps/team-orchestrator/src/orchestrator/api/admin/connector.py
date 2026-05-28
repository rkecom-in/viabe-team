"""VT-224 admin connector endpoints.

setup_push, pull_sample, token_shape, drive_channels (3 routes).

Privacy locks per CL-390 cluster + VT-224 review lock:
- pull_sample returns row_count + col_count + headers ONLY
  (NO row preview, even scrubbed; column names are schema, OK to return)
- token_shape returns SHAPE only — never raw token values
- audit log stores token fingerprint, not raw token
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from orchestrator.api.admin._auth import log_admin_call
from orchestrator.api.admin._rate_limit import RateLimitedAdmin
from orchestrator.graph import get_pool
from orchestrator.integrations.connectors.google_sheet import GoogleSheetConnector
from orchestrator.integrations.connectors.shopify import ShopifyConnector

router = APIRouter()


class SetupPushBody(BaseModel):
    tenant_id: str
    connector_id: str
    spreadsheet_id: str | None = None


@router.post("/api/orchestrator/admin/connector/setup_push")
def admin_setup_push(
    body: SetupPushBody,
    request: Request,
    fp: RateLimitedAdmin,
) -> dict[str, Any]:
    try:
        tenant_uuid = UUID(body.tenant_id)
    except ValueError:
        log_admin_call(
            request=request,
            endpoint="POST /api/orchestrator/admin/connector/setup_push",
            response_status=400,
            connector_id=body.connector_id,
            error_message="invalid tenant_id UUID",
        )
        raise HTTPException(400, "tenant_id must be a UUID") from None

    try:
        if body.connector_id == "google_sheet":
            if not body.spreadsheet_id:
                raise HTTPException(400, "spreadsheet_id required for google_sheet")
            result = GoogleSheetConnector().setup_push(
                tenant_uuid, body.spreadsheet_id
            )
        elif body.connector_id == "shopify":
            result = ShopifyConnector().setup_push(tenant_uuid)
        else:
            raise HTTPException(400, f"unknown connector_id: {body.connector_id}")
    except HTTPException:
        log_admin_call(
            request=request,
            endpoint="POST /api/orchestrator/admin/connector/setup_push",
            response_status=400,
            tenant_id=body.tenant_id,
            connector_id=body.connector_id,
        )
        raise
    except Exception as exc:  # noqa: BLE001
        log_admin_call(
            request=request,
            endpoint="POST /api/orchestrator/admin/connector/setup_push",
            response_status=500,
            tenant_id=body.tenant_id,
            connector_id=body.connector_id,
            error_message=repr(exc)[:200],
        )
        raise HTTPException(500, f"setup_push failed: {exc}") from exc

    log_admin_call(
        request=request,
        endpoint="POST /api/orchestrator/admin/connector/setup_push",
        response_status=200,
        tenant_id=body.tenant_id,
        connector_id=body.connector_id,
    )
    return result


class PullSampleBody(BaseModel):
    tenant_id: str
    connector_id: str
    spreadsheet_id: str | None = None
    range: str | None = None


@router.post("/api/orchestrator/admin/connector/pull_sample")
def admin_pull_sample(
    body: PullSampleBody,
    request: Request,
    fp: RateLimitedAdmin,
) -> dict[str, Any]:
    """Returns row_count + col_count + headers ONLY (no row data)."""
    try:
        tenant_uuid = UUID(body.tenant_id)
    except ValueError:
        log_admin_call(
            request=request,
            endpoint="POST /api/orchestrator/admin/connector/pull_sample",
            response_status=400,
            connector_id=body.connector_id,
        )
        raise HTTPException(400, "tenant_id must be a UUID") from None

    try:
        if body.connector_id == "google_sheet":
            if not body.spreadsheet_id:
                raise HTTPException(400, "spreadsheet_id required for google_sheet")
            rows = GoogleSheetConnector().pull_sample(
                tenant_uuid,
                body.spreadsheet_id,
                body.range or "A1:Z50",
            )
        elif body.connector_id == "shopify":
            rows = ShopifyConnector().pull_sample(tenant_uuid)
        else:
            raise HTTPException(400, f"unknown connector_id: {body.connector_id}")
    except HTTPException:
        log_admin_call(
            request=request,
            endpoint="POST /api/orchestrator/admin/connector/pull_sample",
            response_status=400,
            tenant_id=body.tenant_id,
            connector_id=body.connector_id,
        )
        raise
    except Exception as exc:  # noqa: BLE001
        log_admin_call(
            request=request,
            endpoint="POST /api/orchestrator/admin/connector/pull_sample",
            response_status=500,
            tenant_id=body.tenant_id,
            connector_id=body.connector_id,
            error_message=repr(exc)[:200],
        )
        raise HTTPException(500, f"pull_sample failed: {exc}") from exc

    headers = list(rows[0].keys()) if rows else []
    result = {
        "row_count": len(rows),
        "col_count": len(headers),
        "headers": headers,
    }
    log_admin_call(
        request=request,
        endpoint="POST /api/orchestrator/admin/connector/pull_sample",
        response_status=200,
        tenant_id=body.tenant_id,
        connector_id=body.connector_id,
    )
    return result


@router.get("/api/orchestrator/admin/connector/token_shape")
def admin_token_shape(
    tenant_id: str,
    connector_id: str,
    request: Request,
    fp: RateLimitedAdmin,
) -> dict[str, Any]:
    """Returns shape only. NEVER raw token values."""
    try:
        UUID(tenant_id)
    except ValueError:
        log_admin_call(
            request=request,
            endpoint="GET /api/orchestrator/admin/connector/token_shape",
            response_status=400,
            connector_id=connector_id,
        )
        raise HTTPException(400, "tenant_id must be a UUID") from None

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT scopes,
                   refresh_token_encrypted IS NOT NULL AS refresh_present,
                   push_secret IS NOT NULL AS push_secret_present,
                   last_refreshed_at,
                   updated_at,
                   created_at
            FROM tenant_oauth_tokens
            WHERE tenant_id = %s AND connector_id = %s
            """,
            (tenant_id, connector_id),
        )
        row = cur.fetchone()

    if row is None:
        log_admin_call(
            request=request,
            endpoint="GET /api/orchestrator/admin/connector/token_shape",
            response_status=404,
            tenant_id=tenant_id,
            connector_id=connector_id,
        )
        raise HTTPException(404, "no token row for (tenant_id, connector_id)")

    scopes = row["scopes"] if isinstance(row, dict) else row[0]
    result = {
        "scope_count": len(scopes) if scopes else 0,
        "scopes": list(scopes) if scopes else [],
        "refresh_present": bool(row["refresh_present"] if isinstance(row, dict) else row[1]),
        "push_secret_present": bool(row["push_secret_present"] if isinstance(row, dict) else row[2]),
        "last_refreshed_at": (row["last_refreshed_at"] if isinstance(row, dict) else row[3]).isoformat() if (row["last_refreshed_at"] if isinstance(row, dict) else row[3]) else None,
        "updated_at": (row["updated_at"] if isinstance(row, dict) else row[4]).isoformat() if (row["updated_at"] if isinstance(row, dict) else row[4]) else None,
        "created_at": (row["created_at"] if isinstance(row, dict) else row[5]).isoformat() if (row["created_at"] if isinstance(row, dict) else row[5]) else None,
    }
    log_admin_call(
        request=request,
        endpoint="GET /api/orchestrator/admin/connector/token_shape",
        response_status=200,
        tenant_id=tenant_id,
        connector_id=connector_id,
    )
    return result


@router.get("/api/orchestrator/admin/connector/drive_channels")
def admin_drive_channels(
    tenant_id: str,
    request: Request,
    fp: RateLimitedAdmin,
) -> list[dict[str, Any]]:
    """VT-222 substrate stub. Returns empty list pre-VT-222."""
    try:
        UUID(tenant_id)
    except ValueError:
        log_admin_call(
            request=request,
            endpoint="GET /api/orchestrator/admin/connector/drive_channels",
            response_status=400,
        )
        raise HTTPException(400, "tenant_id must be a UUID") from None
    log_admin_call(
        request=request,
        endpoint="GET /api/orchestrator/admin/connector/drive_channels",
        response_status=200,
        tenant_id=tenant_id,
    )
    return []


@router.post("/api/orchestrator/admin/connector/drive_channels/{channel_id}/renew")
def admin_drive_channels_renew(
    channel_id: str,
    request: Request,
    fp: RateLimitedAdmin,
) -> dict[str, Any]:
    """VT-222 substrate stub. Returns 501 pre-VT-222."""
    log_admin_call(
        request=request,
        endpoint="POST /api/orchestrator/admin/connector/drive_channels/{channel_id}/renew",
        response_status=501,
    )
    raise HTTPException(
        status_code=501,
        detail="drive_channels/renew is not implemented until VT-222 ships",
    )
