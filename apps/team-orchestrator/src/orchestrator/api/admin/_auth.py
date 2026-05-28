"""VT-224 admin endpoint auth + audit-log helpers.

X-Team-Admin-Token header verified against TEAM_ADMIN_API_TOKEN env via
constant-time compare. Returns a fingerprint (8-char sha256 prefix) for
audit-log persistence; the raw token never leaves this module.

Audit-log writes happen in ``log_admin_call``. Endpoints call this
explicitly after the handler returns (or in a finally for error paths).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)

ADMIN_TOKEN_ENV = "TEAM_ADMIN_API_TOKEN"


def _token_fingerprint(token: str) -> str:
    """First 8 chars of sha256(token). Stable; never reverses to raw."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


async def require_admin_token(
    request: Request,
    x_team_admin_token: Annotated[str | None, Header(alias="X-Team-Admin-Token")] = None,
) -> str:
    """FastAPI dependency. Returns the token's 8-char fingerprint on
    pass, raises 403 on missing/wrong, 503 on env unset.
    """
    expected = os.environ.get(ADMIN_TOKEN_ENV, "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=f"{ADMIN_TOKEN_ENV} not configured",
        )
    if not x_team_admin_token or not hmac.compare_digest(
        x_team_admin_token, expected
    ):
        raise HTTPException(status_code=403, detail="invalid admin token")
    fp = _token_fingerprint(x_team_admin_token)
    # Stash on request.state so audit-log helper can read without re-deriving.
    request.state.admin_token_fingerprint = fp
    return fp


AdminAuth = Annotated[str, Depends(require_admin_token)]


def log_admin_call(
    *,
    request: Request,
    endpoint: str,
    response_status: int,
    tenant_id: str | None = None,
    connector_id: str | None = None,
    error_message: str | None = None,
) -> None:
    """Write one row to admin_audit_log. Never raises; logs on failure."""
    fp = getattr(request.state, "admin_token_fingerprint", "unknown")
    source_ip = request.client.host if request.client else "unknown"
    try:
        pool = get_pool()
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO admin_audit_log
                    (endpoint, tenant_id, connector_id, source_ip,
                     response_status, token_fingerprint, error_message)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    endpoint,
                    tenant_id,
                    connector_id,
                    source_ip,
                    response_status,
                    fp,
                    error_message,
                ),
            )
    except Exception:  # noqa: BLE001 — audit log MUST NOT break the call
        logger.exception("admin audit log write failed (endpoint=%s)", endpoint)
