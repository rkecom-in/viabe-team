"""VT-77 — self-serve DSR admin endpoints.

``POST /api/orchestrator/admin/dsr/export`` → ZIP of the tenant's per-table data
(PII-scrubbed, Phase-1 (a) posture). ``POST /api/orchestrator/admin/dsr/delete``
→ create/load a dsr_tickets ticket then run ``purge_tenant_data`` (ONE fulfilment
path, Pillar 8).

Auth (Cowork 20260603T154500Z answer 3): operator-confirmed + ``INTERNAL_API_SECRET``
now (same as the other admin paths); owner-self-serve via the portal activates with
the portal (gate-live VT-231). CL-422: dev = synthetic only until Mumbai.
"""

from __future__ import annotations

import hmac
import logging
import os
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field

from orchestrator.dsr_export import build_export_zip, export_tenant_data
from orchestrator.dsr_purge import purge_tenant_data
from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)
router = APIRouter()


class DsrTenantBody(BaseModel):
    tenant_id: str = Field(..., min_length=1)


def _verify_internal_secret(provided: str | None) -> None:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="X-Internal-Secret invalid")


def _parse_tenant(tenant_id: str) -> str:
    try:
        return str(UUID(tenant_id))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="tenant_id not a UUID") from exc


@router.post("/api/orchestrator/admin/dsr/export")
def dsr_export(
    body: DsrTenantBody,
    x_internal_secret: str | None = Header(default=None),
) -> Response:
    """Return a ZIP (manifest.json + <table>.json) of the tenant's data."""
    _verify_internal_secret(x_internal_secret)
    tid = _parse_tenant(body.tenant_id)
    export = export_tenant_data(tid)
    zip_bytes = build_export_zip(export)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="dsr-export-{tid}.zip"'
        },
    )


@router.post("/api/orchestrator/admin/dsr/delete")
def dsr_delete(
    body: DsrTenantBody,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, object]:
    """Create/load a deletion ticket, then run the purge (one fulfilment path)."""
    _verify_internal_secret(x_internal_secret)
    tid = _parse_tenant(body.tenant_id)

    # One fulfilment path (Pillar 8): an open/acknowledged deletion ticket for the
    # tenant is reused; else a new one is opened. Service-role insert (BYPASSRLS)
    # with an explicit tenant_id — mirrors dsr_purge's privileged path.
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT id::text AS id FROM dsr_tickets "
            "WHERE tenant_id = %s AND request_type = 'deletion' "
            "  AND status <> 'completed' "
            "ORDER BY acknowledged_at DESC NULLS LAST LIMIT 1",
            (tid,),
        ).fetchone()
        if row:
            ticket_id = row["id"] if isinstance(row, dict) else row[0]
        else:
            created = conn.execute(
                "INSERT INTO dsr_tickets (tenant_id, request_type, status, "
                "acknowledged_at) VALUES (%s, 'deletion', 'acknowledged', now()) "
                "RETURNING id::text AS id",
                (tid,),
            ).fetchone()
            ticket_id = created["id"] if isinstance(created, dict) else created[0]

    result = purge_tenant_data(UUID(ticket_id))
    return {
        "tenant_id": tid,
        "ticket_id": ticket_id,
        "deleted_counts": result.deleted_counts,
        "tenant_anonymized": result.tenant_anonymized,
        "already_completed": result.already_completed,
    }


__all__ = ["router"]
