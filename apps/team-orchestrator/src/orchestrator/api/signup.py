"""VT-82 — POST /api/signup (owner signup, the sole owner-acquisition surface).

Thin route over ``onboarding.signup.run_signup`` — validate the 6 fields + the two
consents, atomically create the tenant + consent proof + trial, coarsen the city →
city_tier (closes VT-317), merge owner_name into business_profile, and queue the
welcome (injectable, non-terminal). Tenant-creation is PRE-tenant-context
(service_role); no auth/GUC needed here — it's the bootstrap surface.
"""

from __future__ import annotations

import hmac
import os
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


def _verify_internal_secret(provided: str | None) -> bool:
    """VT-326 A2: only team-web (which holds INTERNAL_API_SECRET) may reach this
    BYPASSRLS create surface — a constant-time match (CL-72 pattern)."""
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)

# SignupError.code → HTTP status. Everything but a duplicate is a 400 (bad input).
_DUPLICATE_STATUS = 409
_BAD_REQUEST_STATUS = 400
# VT-408 gate statuses: a terminal GSTIN reject is a 422 (the input is well-formed but the
# business is not GST-registered — Unprocessable); a vendor_down HOLD is a 503 (transient,
# retry — Service Unavailable on the verification vendor). Distinct from a 400 field error.
_GATE_REJECT_STATUS = 422
_VENDOR_DOWN_STATUS = 503

# VT-94: in-process cache for the public founding-status endpoint (Cowork — a public
# unauth endpoint must not be a per-request DB-load / DoS vector; the count changes only
# on a signup, so a short stale window is fine).
_FOUNDING_CACHE_TTL_SEC = 60.0
_founding_cache: dict[str, Any] = {"value": None, "expiry": 0.0}


class SignupBody(BaseModel):
    business_name: str = Field(..., min_length=1, max_length=200)
    owner_name: str = Field(..., min_length=1, max_length=120)
    whatsapp_number: str = Field(..., min_length=1, max_length=20)
    preferred_language: str = Field(..., min_length=2, max_length=2)
    city: str = Field(..., min_length=1, max_length=120)
    business_type: str = Field(..., min_length=1, max_length=40)
    consent_dpdpa: bool
    consent_residency: bool
    # VT-408: the GSTIN to verify before the tenant is created (verify-then-create). The web
    # form (VT-406) collects it as a gating sub-step. Optional at the schema boundary; an
    # empty/unverified value is a hard reject in run_signup's gate (no GST => nothing).
    gstin: str = Field(default="", max_length=20)
    # VT-449: the MCA CIN the owner picked/confirmed (registry leg). When present, run_signup fetches MCA
    # Company Master Data → uses the AUTHORITATIVE canonical name for the GST name-match + persists the
    # (encrypted) tenant_mca_data. Optional; absent → the name-match anchors on the typed business_name.
    cin: str = Field(default="", max_length=21)


@router.get("/api/signup/business-types")
def business_types() -> dict[str, object]:
    """VT-96: the signup form's business_type options (key + en/hi labels) — the
    config taxonomy as the single source of truth. Static, public, no PII, no auth."""
    from orchestrator.onboarding.signup import business_type_options

    return {"business_types": business_type_options()}


@router.get("/api/team/founding-status")
def founding_status() -> dict[str, object]:
    """VT-94: public founding-tier counter for the landing-site widget (VT-99 consumes
    it). No auth — the count is non-sensitive. CACHED ~60s (Cowork): a public unauth
    endpoint must not be a per-request DB-load / DoS vector; the count changes only on a
    signup, so a short stale window is fine."""
    from orchestrator.billing.founding_counter import get_founding_status
    from orchestrator.graph import get_pool

    now_m = time.monotonic()
    cached = _founding_cache["value"]
    if cached is not None and now_m < _founding_cache["expiry"]:
        return cached  # type: ignore[no-any-return]
    with get_pool().connection() as conn:
        status = get_founding_status(conn)
    result: dict[str, object] = {
        "remaining": status.remaining,
        "cap": status.cap,
        "public_count": status.public_count,
        "all_claimed": status.all_claimed,
    }
    _founding_cache["value"] = result
    _founding_cache["expiry"] = now_m + _FOUNDING_CACHE_TTL_SEC
    return result


@router.post("/api/signup", status_code=201)
def signup(
    body: SignupBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, object]:
    # VT-326: the BYPASSRLS create surface is now defended at BOTH boundaries — team-web
    # gates it (OTP-before-create proof + per-IP throttle) AND requires this
    # X-Internal-Secret, so a topology/SSRF leak can't reach create unauthenticated
    # (closes the flooding gap at the source, not just the edge). Number-squatting is
    # closed by the team-web OTP proof-of-control before this is ever called.
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})

    from orchestrator.onboarding.signup import (
        SignupError,
        SignupGateError,
        SignupInput,
        run_signup,
    )
    from orchestrator.onboarding.signup_gate import gate_copy

    try:
        out = run_signup(SignupInput(**body.model_dump()))
    except SignupGateError as exc:
        # VT-408 GSTIN hard-gate refused to create a tenant. A retryable HOLD (vendor_down,
        # "on our side") → 503 so the form shows a Retry affordance; a terminal REJECT
        # (invalid/missing GSTIN, generic copy — NO enumeration oracle) → 422. NO tenant was
        # created either way. The owner-facing bilingual copy is resolved server-side.
        copy_kind = "vendor_down" if exc.retryable else "reject"
        status = _VENDOR_DOWN_STATUS if exc.retryable else _GATE_REJECT_STATUS
        raise HTTPException(
            status_code=status,
            detail={
                "code": exc.outcome,
                "retryable": exc.retryable,
                "message": gate_copy(copy_kind, exc.language),
            },
        ) from exc
    except SignupError as exc:
        status = _DUPLICATE_STATUS if exc.code == "duplicate" else _BAD_REQUEST_STATUS
        raise HTTPException(
            status_code=status, detail={"code": exc.code, "message": str(exc)}
        ) from exc

    return {
        "tenant_id": str(out.tenant_id),
        "plan_tier": out.plan_tier,
        "city_tier": out.city_tier,
        "welcome_sent": out.welcome_sent,
    }


__all__ = ["router"]
