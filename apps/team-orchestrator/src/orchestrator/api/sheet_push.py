"""VT-207 Sheet push router.

Endpoint: ``POST /api/orchestrator/integrations/sheet/push``.

Apps Script (rendered by ``apps_script_template.render_apps_script``)
POSTs row-edit events with HMAC-SHA256-signed body. The handler:

1. Reads ``X-Viabe-Tenant`` header → tenant_id
2. Loads ``push_secret`` from ``tenant_oauth_tokens`` for
   (tenant_id, 'google_sheet')
3. Verifies HMAC over raw body via
   ``verify_push_signature``
4. Hands canonical row to dedupe/mapping seam (VT-209)
5. Returns 204 on success; 403 on bad signature

Per CL-72: Pillar 7 — handlers MUST return 2xx so Apps Script's
``muteHttpExceptions: true`` upstream doesn't surface noise on
transient failures. Errors are logged + a 200-shaped envelope is
returned with ``reason`` instead.
"""

from __future__ import annotations

import logging
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request

from orchestrator.graph import get_pool
from orchestrator.integrations.connectors.apps_script_template import (
    verify_push_signature,
)
from orchestrator.integrations.dedupe import dedupe_customer_row

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/orchestrator/integrations/sheet/push")
async def sheet_push(
    request: Request,
    x_viabe_signature: str = Header(default="", alias="X-Viabe-Signature"),
    x_viabe_tenant: str = Header(default="", alias="X-Viabe-Tenant"),
) -> dict[str, Any]:
    if not x_viabe_signature or not x_viabe_tenant:
        raise HTTPException(
            status_code=400,
            detail="X-Viabe-Signature + X-Viabe-Tenant headers required",
        )
    try:
        tenant_uuid = UUID(x_viabe_tenant)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid tenant_id") from None

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT push_secret FROM tenant_oauth_tokens "
            "WHERE tenant_id = %s AND connector_id = 'google_sheet'",
            (str(tenant_uuid),),
        )
        raw = cur.fetchone()
    row = cast("dict[str, Any] | None", raw)
    if row is None or not row["push_secret"]:
        raise HTTPException(
            status_code=403, detail="no push_secret for tenant"
        )

    body = await request.body()
    if not verify_push_signature(
        body=body, signature=x_viabe_signature, push_secret=row["push_secret"]
    ):
        raise HTTPException(status_code=403, detail="invalid signature")

    payload = await request.json()
    row_data = payload.get("row_data", {})
    phone = (
        row_data.get("phone")
        or row_data.get("Phone")
        or row_data.get("Mobile")
        or ""
    )
    if not phone:
        logger.info(
            "VT-207 sheet push: no phone in row_data; skipping dedupe",
            extra={"tenant_id": str(tenant_uuid)},
        )
        return {"status": "ok", "reason": "no_phone_field"}

    decision = dedupe_customer_row(
        tenant_id=tenant_uuid,
        phone_e164=phone,
        connector_id="google_sheet",
        canonical_row=row_data,
    )
    return {
        "status": "ok",
        "decision": decision.kind,
        "phone_token": decision.phone_token,
    }
