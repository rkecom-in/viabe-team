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
from datetime import timedelta
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


_MAX_PAGE_SIZE = 100


@router.get("/api/orchestrator/owner/dashboard-customers")
def dashboard_customers(
    tenant_id: str = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1),
    excluded_only: bool = Query(False),
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    """Paginated owner customer list. Read-only. Phones MASKED at source (last-4 only);
    raw phone_e164 NEVER crosses to team-web. tenant_id is the session-derived value the
    team-web proxy forwards (the IDOR boundary lives in team-web's session gate + the secret)."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="X-Internal-Secret mismatch")
    limit = min(page_size, _MAX_PAGE_SIZE)
    offset = (page - 1) * limit
    try:
        wrapper = CustomersWrapper()
        rows = wrapper.list_customers_page(
            tenant_id, limit=limit, offset=offset, excluded_only=excluded_only
        )
        total = wrapper.count_all(tenant_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="customers read failed") from exc

    return {
        "page": page,
        "page_size": limit,
        "total": total,
        "customers": [
            {
                "display_name": r.get("display_name"),
                "phone_last4": _mask_phone(r.get("phone_e164")),  # MASKED at source
                "opt_out_status": r.get("opt_out_status"),
                "spend_rupees": int(r.get("spend_paise", 0)) // 100,
            }
            for r in rows
        ],
    }


@router.get("/api/orchestrator/owner/dashboard-campaigns")
def dashboard_campaigns(
    tenant_id: str = Query(...),
    days_back: int = Query(365, ge=1, le=365),
    limit: int = Query(50, ge=1),
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    """Owner campaign history. Read-only. Campaigns carry NO customer PII (tenant-level
    rollups) — no masking needed. Tenant-scoped via the wrapper (RLS + explicit WHERE)."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="X-Internal-Secret mismatch")
    try:
        rows = CampaignsWrapper().list_recent_with_responses(
            tenant_id, days_back=days_back, limit=min(limit, _MAX_PAGE_SIZE)
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="campaigns read failed") from exc

    return {
        "campaigns": [
            {
                "campaign_id": str(r.get("campaign_id") or ""),
                "status": r.get("status"),
                "template_id": r.get("template_id"),
                "responses": int(r.get("response_count", 0)),
                "sent_at": str(r["sent_at"]) if r.get("sent_at") else None,
            }
            for r in rows
        ],
    }


@router.get("/api/orchestrator/owner/dashboard-settings")
def dashboard_settings(
    tenant_id: str = Query(...),
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    """Owner settings — read-only business profile + plan/trial status. The owner's OWN
    business data (owner_name is the owner, not a customer) — no customer PII here. The
    DSR-init buttons live in team-web (links to /api/dsr/*); this endpoint is read-only."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="X-Internal-Secret mismatch")
    try:
        from orchestrator.agent.tools.get_business_profile import (
            GetBusinessProfileInput,
            get_business_profile,
        )
        from orchestrator.db.tenant_connection import tenant_connection

        profile = get_business_profile(GetBusinessProfileInput(tenant_id=tenant_id))
        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT plan_tier, phase, trial_started_at, trial_extension_count, "
                "       preferred_language FROM tenants WHERE id = %s",
                (tenant_id,),
            ).fetchone()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="settings read failed") from exc

    plan = dict(row) if row else {}
    trial_started = plan.get("trial_started_at")
    trial_ends = (trial_started + timedelta(days=14)) if trial_started else None
    return {
        "business": (
            {
                "business_name": profile.business_name,
                "business_archetype": profile.business_archetype,
                "owner_name": profile.owner_name,
                "locale": profile.locale,
                "working_hours": profile.working_hours,
            }
            if profile
            else None
        ),
        "plan": {
            "plan_tier": plan.get("plan_tier"),
            "phase": plan.get("phase"),
            "trial_started_at": str(trial_started) if trial_started else None,
            "trial_ends_at": str(trial_ends) if trial_ends else None,
            "trial_extension_count": plan.get("trial_extension_count"),
            "preferred_language": plan.get("preferred_language"),
        },
    }


@router.get("/api/orchestrator/owner/dashboard-reports")
def dashboard_reports(
    tenant_id: str = Query(...),
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    """Owner monthly-reports list. Read-only, tenant-scoped (monthly_reports RLS). No PII
    (tenant-level rollups). ``has_pdf`` indicates a stored PDF is available to download via
    the team-web download route (which mints a short-lived signed Storage URL)."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="X-Internal-Secret mismatch")
    try:
        from orchestrator.db.tenant_connection import tenant_connection

        with tenant_connection(tenant_id) as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT year_month, generated_at, pdf_storage_path FROM monthly_reports "
                    "WHERE tenant_id = %s ORDER BY year_month DESC",
                    (tenant_id,),
                ).fetchall()
            ]
    except Exception as exc:
        raise HTTPException(status_code=502, detail="reports read failed") from exc

    return {
        "reports": [
            {
                "year_month": r["year_month"],
                "generated_at": str(r["generated_at"]) if r.get("generated_at") else None,
                "has_pdf": bool(r.get("pdf_storage_path")),
            }
            for r in rows
        ],
    }
