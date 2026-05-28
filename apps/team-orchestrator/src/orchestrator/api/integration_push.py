"""VT-210 — Generic push-webhook receiver.

Endpoint: ``POST /api/orchestrator/integrations/{connector_id}/push``.

Replaces VT-207's per-connector ``sheet_push`` route once the
ConnectorBase ``verify_push_signature`` + ``parse_push_payload``
contract is in place. The legacy route stays as a redirect for now
(Apps Scripts already in the wild post to ``/sheet/push``); future
work can deprecate.

Auth strategy is per-connector:
- google_sheet: HMAC-SHA256 over raw body via X-Viabe-Signature header
  (shared push_secret stored in tenant_oauth_tokens)
- shopify (VT-208): X-Shopify-Hmac-Sha256 base64 HMAC

Both flow through ``connector.verify_push_signature(body, headers, push_secret)``.

Per CL-72: 2xx always when at all possible; 403 on signature failure
is the one allowed non-2xx so vendors stop sending bad payloads.

Per VT-210 brief AC-2: synthetic POST round-trips within 30s.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request

from orchestrator.graph import get_pool
from orchestrator.integrations.dedupe import dedupe_customer_row

logger = logging.getLogger(__name__)
router = APIRouter()


def _connector_for(connector_id: str):  # noqa: ANN202 — late-bound on purpose
    from orchestrator.integrations.scheduler import _connector_class_for

    return _connector_class_for(connector_id)()


@router.post("/api/orchestrator/integrations/{connector_id}/push")
async def integration_push(
    connector_id: str,
    request: Request,
    x_viabe_tenant: str = Header(default="", alias="X-Viabe-Tenant"),
) -> dict[str, Any]:
    if not x_viabe_tenant:
        raise HTTPException(
            status_code=400, detail="X-Viabe-Tenant header required"
        )
    try:
        tenant_uuid = UUID(x_viabe_tenant)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid tenant_id") from None

    connector = _connector_for(connector_id)
    if not connector.spec.push_supported:
        raise HTTPException(
            status_code=400, detail=f"connector {connector_id} does not support push"
        )

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT push_secret FROM tenant_oauth_tokens "
            "WHERE tenant_id = %s AND connector_id = %s",
            (str(tenant_uuid), connector_id),
        )
        raw = cur.fetchone()
    row = cast("dict[str, Any] | None", raw)
    if row is None or not row["push_secret"]:
        raise HTTPException(
            status_code=403, detail="no push_secret for tenant"
        )

    body = await request.body()
    if not connector.verify_push_signature(
        body, dict(request.headers), row["push_secret"]
    ):
        raise HTTPException(status_code=403, detail="invalid signature")

    rows = connector.parse_push_payload(body)
    persisted = 0
    for canonical_row in rows:
        phone = canonical_row.get("phone") or canonical_row.get("Phone")
        if not phone:
            continue
        dedupe_customer_row(
            tenant_id=tenant_uuid,
            phone_e164=str(phone),
            connector_id=connector_id,
            canonical_row=canonical_row,
        )
        persisted += 1

    now = datetime.now(UTC)
    with pool.connection() as conn:
        conn.execute(
            """
            UPDATE tenant_connector_status SET
                last_sync_at = %s,
                last_status = 'ok',
                consecutive_fails = 0,
                rows_ingested_today = CASE
                    WHEN last_ingested_date = %s THEN rows_ingested_today + %s
                    ELSE %s
                END,
                last_ingested_date = %s,
                updated_at = now()
            WHERE tenant_id = %s AND connector_id = %s
            """,
            (
                now, now.date(), persisted, persisted, now.date(),
                str(tenant_uuid), connector_id,
            ),
        )

    return {"status": "ok", "rows_ingested": persisted}
