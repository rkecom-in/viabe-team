"""VT-228 — admin endpoints to grant/revoke Ops operators.

Writes the `operator_allowlist` table (migration 046) that team-web's
auth callsites read. Gated by `TEAM_ADMIN_API_TOKEN` (VT-224 pattern via
RateLimitedAdmin). Synchronous DB writes via the DBOS pool — these are
low-frequency admin actions, not hot-path.

NO PII: logs user_id (a Supabase Auth UUID) + action only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from orchestrator.api.admin._auth import log_admin_call
from orchestrator.api.admin._rate_limit import RateLimitedAdmin

router = APIRouter()


class GrantBody(BaseModel):
    user_id: str = Field(..., min_length=1)
    granted_by: str | None = None
    notes: str | None = None


class RevokeBody(BaseModel):
    user_id: str = Field(..., min_length=1)
    reason: str | None = None


def _pool() -> Any:
    from orchestrator.graph import get_pool

    return get_pool()


@router.post("/api/orchestrator/admin/operator/grant")
def admin_operator_grant(
    body: GrantBody,
    request: Request,
    fp: RateLimitedAdmin,
) -> dict[str, Any]:
    """Grant operator access (idempotent re-grant clears any prior revoke)."""
    try:
        with _pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO operator_allowlist (user_id, granted_by, notes)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                    SET revoked_at = NULL,
                        revoke_reason = NULL,
                        granted_by = EXCLUDED.granted_by,
                        granted_at = now(),
                        notes = EXCLUDED.notes
                """,
                (body.user_id, body.granted_by, body.notes),
            )
    except Exception as exc:  # noqa: BLE001
        log_admin_call(
            request=request,
            endpoint="POST /api/orchestrator/admin/operator/grant",
            response_status=500,
            error_message=repr(exc)[:200],
        )
        raise HTTPException(status_code=500, detail=type(exc).__name__) from exc
    log_admin_call(
        request=request,
        endpoint="POST /api/orchestrator/admin/operator/grant",
        response_status=200,
    )
    return {"status": "granted", "user_id": body.user_id}


@router.post("/api/orchestrator/admin/operator/revoke")
def admin_operator_revoke(
    body: RevokeBody,
    request: Request,
    fp: RateLimitedAdmin,
) -> dict[str, Any]:
    """Revoke operator access (sets revoked_at; row kept for audit)."""
    try:
        with _pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE operator_allowlist
                   SET revoked_at = now(), revoke_reason = %s
                 WHERE user_id = %s AND revoked_at IS NULL
                """,
                (body.reason, body.user_id),
            )
            affected = cur.rowcount
    except Exception as exc:  # noqa: BLE001
        log_admin_call(
            request=request,
            endpoint="POST /api/orchestrator/admin/operator/revoke",
            response_status=500,
            error_message=repr(exc)[:200],
        )
        raise HTTPException(status_code=500, detail=type(exc).__name__) from exc
    log_admin_call(
        request=request,
        endpoint="POST /api/orchestrator/admin/operator/revoke",
        response_status=200,
    )
    return {
        "status": "revoked" if affected else "not_active",
        "user_id": body.user_id,
    }


__all__ = ["router"]
