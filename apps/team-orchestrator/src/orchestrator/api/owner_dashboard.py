"""VT-87 — owner-portal read endpoint (the data-spine for the dashboard index).

Auth: ``X-Internal-Secret`` (CL-72) — same boundary as owner_verify. team-web derives the
tenant_id from the owner SESSION (requireOwnerSession) and forwards it here; this endpoint
is only reachable with the shared secret, and team-web never trusts a client-supplied
tenant (the IDOR boundary). Read-only (GET).

PII (CL-390): phones are MASKED HERE (last-4 only). The raw ``phone_e164`` from the wrapper
NEVER leaves this function — the HTTP response toward team-web carries last-4 only.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from orchestrator.db.wrappers import CampaignsWrapper, CustomersWrapper

router = APIRouter()


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _mask_phone(phone: str | None) -> str | None:
    """Last-4 only (e.g. '••••3210'). The raw phone never crosses the boundary (CL-390)."""
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    return ("••••" + digits[-4:]) if len(digits) >= 4 else "••••"


@router.get("/api/orchestrator/owner/dashboard-summary")
def dashboard_summary(
    tenant_id: str = Query(...),
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="X-Internal-Secret mismatch")
    try:
        customers = CustomersWrapper()
        campaigns = CampaignsWrapper()
        customer_count = customers.count_all(tenant_id)
        top = customers.top_customers_by_spend(tenant_id, limit=5)
        recent = campaigns.list_recent_with_responses(tenant_id, days_back=30, limit=5)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="dashboard read failed") from exc

    return {
        "customer_count": customer_count,
        "top_customers": [
            {
                "display_name": r.get("display_name"),
                "phone_last4": _mask_phone(r.get("phone_e164")),  # MASKED at source
                "spend_rupees": int(r.get("spend_paise", 0)) // 100,
            }
            for r in top
        ],
        "recent_campaigns": [
            {
                "campaign_id": str(r.get("campaign_id") or ""),
                "status": r.get("status"),
                "template_id": r.get("template_id"),
                "responses": int(r.get("response_count", 0)),
                "sent_at": str(r["sent_at"]) if r.get("sent_at") else None,
            }
            for r in recent
        ],
    }
