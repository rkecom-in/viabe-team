"""VT-195 Phase 3 — read-only admin surface for a tenant's L1 business_profile.

GET the tenant's single 'business_profile' l1_entities attributes (the durable
identity the agent pre-injects). Read-only; the dashboard/Ops Console consumes
this. Write surfaces are the L1 writer (seed/onboarding) + VT-198 owner-feedback.

Gated by the admin token (RateLimitedAdmin), like the other VT-224 admin routes.
RLS-scoped read via search_entities (tenant_connection -> app_role + GUC).
CL-390: business identity, not customer PII.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from orchestrator.api.admin._auth import log_admin_call
from orchestrator.api.admin._rate_limit import RateLimitedAdmin

router = APIRouter()


@router.get("/api/orchestrator/admin/l1_profile/{tenant_id}")
def admin_get_l1_profile(
    tenant_id: str,
    request: Request,
    fp: RateLimitedAdmin,
) -> dict[str, Any]:
    """Return the tenant's L1 business_profile attributes, or 404 if none."""
    endpoint = "GET /api/orchestrator/admin/l1_profile/{tenant_id}"
    try:
        from orchestrator.knowledge import BUSINESS_PROFILE_ENTITY_TYPE, search_entities

        entities = search_entities(
            tenant_id, entity_type=BUSINESS_PROFILE_ENTITY_TYPE, limit=1
        )
    except Exception as exc:  # noqa: BLE001
        log_admin_call(
            request=request, endpoint=endpoint, response_status=500,
            error_message=repr(exc)[:200],
        )
        raise HTTPException(status_code=500, detail="l1_profile read failed") from exc

    if not entities:
        log_admin_call(request=request, endpoint=endpoint, response_status=404)
        raise HTTPException(status_code=404, detail="no business_profile for tenant")

    log_admin_call(request=request, endpoint=endpoint, response_status=200)
    return {"tenant_id": tenant_id, "attributes": entities[0].attributes}
