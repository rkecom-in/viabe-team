"""VT-361 — business-verification endpoint (two-tier; lookup). Internal-secret (team-web proxies).

GSTIN lookup → gstin_verified. All vendor calls happen here (orchestrator-side), fail-closed,
RLS-scoped via verification.py. team-web never calls Sandbox. The VTR "green" override lives on the
ops surface (api/ops_resolve.py — operator-JWT gated).
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

router = APIRouter()


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


class BusinessVerificationBody(BaseModel):
    tenant_id: str
    gstin: str


@router.post("/api/business-verification")
def business_verification(
    body: BusinessVerificationBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    """VT-361 two-tier: GSTIN lookup → gstin_verified on an ACTIVE GSTIN. Fail-closed (a vendor
    failure never fakes verified; vendor_down is retryable, invalid_gstin is bad input)."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
    if not body.gstin:
        raise HTTPException(status_code=422, detail={"code": "gstin_required"})

    from orchestrator.onboarding import verification

    return verification.run_lookup(body.tenant_id, body.gstin)
