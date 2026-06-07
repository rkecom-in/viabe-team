"""VT-361 — business-verification endpoint (Option F). Internal-secret only (team-web proxies).

ONE endpoint, dispatched by ``action`` (lookup | initiate | bind). All vendor calls happen here
(orchestrator-side), fail-closed, RLS-scoped via verification.py. team-web never calls Sandbox.
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
    action: str  # 'lookup' | 'initiate' | 'bind'
    gstin: str | None = None
    reference: str | None = None


@router.post("/api/business-verification")
def business_verification(
    body: BusinessVerificationBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    """VT-361 Option F. lookup → GSTIN name; initiate → reverse-penny-drop UPI handle; bind → match
    payer name + set the tier. Fail-closed throughout (a vendor failure never fakes verified)."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})

    from orchestrator.onboarding import verification

    if body.action == "lookup":
        if not body.gstin:
            raise HTTPException(status_code=422, detail={"code": "gstin_required"})
        return verification.run_lookup(body.tenant_id, body.gstin)
    if body.action == "initiate":
        return verification.run_initiate(body.tenant_id)
    if body.action == "bind":
        if not body.reference:
            raise HTTPException(status_code=422, detail={"code": "reference_required"})
        return verification.run_bind(body.tenant_id, body.reference)
    raise HTTPException(status_code=422, detail={"code": "unknown_action"})
