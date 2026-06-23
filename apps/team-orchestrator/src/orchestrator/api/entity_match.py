"""VT-406 — entity-match endpoints (candidate lookup + verify-confirm). Internal-secret gated
(team-web proxies; the Sandbox/Apify vendor calls all happen orchestrator-side, never in team-web).

- POST /api/orchestrator/onboard/entity-candidates {business_name, city} → UNVERIFIED candidates for
  the owner to pick (never shown as verified).
- POST /api/orchestrator/onboard/entity-confirm {tenant_id, gstin} → round-trips the chosen GSTIN
  through Sandbox (verification.run_lookup); ACTIVE => gstin_verified + anchor + seeds async discovery.

The HARD reject (no gstin_verified => no account) is VT-408; this surface returns the verify status,
it does not block account creation.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

router = APIRouter()


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


class EntityCandidatesBody(BaseModel):
    business_name: str
    city: str = ""


class EntityConfirmBody(BaseModel):
    tenant_id: str
    gstin: str


@router.post("/api/orchestrator/onboard/entity-candidates")
def entity_candidates(
    body: EntityCandidatesBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    """Surface UNVERIFIED entity candidates (web-search GSTIN hints + GBP). Graceful-degrade to an
    empty list — never stalls signup. Candidates are NOT facts; the owner picks one to verify."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
    if not body.business_name.strip():
        raise HTTPException(status_code=422, detail={"code": "business_name_required"})

    from orchestrator.onboarding import entity_match

    candidates = entity_match.fetch_candidates(body.business_name, body.city)
    return {"candidates": [asdict(c) for c in candidates]}


@router.post("/api/orchestrator/onboard/entity-confirm")
def entity_confirm(
    body: EntityConfirmBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    """Verify the owner-confirmed GSTIN (Sandbox round-trip) → gstin_verified + anchor + async
    discovery seed. Fail-closed (a vendor failure never fakes verified; vendor_down is retryable,
    invalid_gstin is bad input)."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
    if not body.gstin.strip():
        raise HTTPException(status_code=422, detail={"code": "gstin_required"})

    from orchestrator.onboarding import entity_match

    return entity_match.confirm_and_verify(body.tenant_id, body.gstin)
