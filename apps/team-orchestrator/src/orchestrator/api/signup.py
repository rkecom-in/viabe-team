"""VT-82 — POST /api/signup (owner signup, the sole owner-acquisition surface).

Thin route over ``onboarding.signup.run_signup`` — validate the 6 fields + the two
consents, atomically create the tenant + consent proof + trial, coarsen the city →
city_tier (closes VT-317), merge owner_name into business_profile, and queue the
welcome (injectable, non-terminal). Tenant-creation is PRE-tenant-context
(service_role); no auth/GUC needed here — it's the bootstrap surface.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

# SignupError.code → HTTP status. Everything but a duplicate is a 400 (bad input).
_DUPLICATE_STATUS = 409
_BAD_REQUEST_STATUS = 400


class SignupBody(BaseModel):
    business_name: str = Field(..., min_length=1, max_length=200)
    owner_name: str = Field(..., min_length=1, max_length=120)
    whatsapp_number: str = Field(..., min_length=1, max_length=20)
    preferred_language: str = Field(..., min_length=2, max_length=2)
    city: str = Field(..., min_length=1, max_length=120)
    business_type: str = Field(..., min_length=1, max_length=40)
    consent_dpdpa: bool
    consent_residency: bool


@router.get("/api/signup/business-types")
def business_types() -> dict[str, object]:
    """VT-96: the signup form's business_type options (key + en/hi labels) — the
    config taxonomy as the single source of truth. Static, public, no PII, no auth."""
    from orchestrator.onboarding.signup import business_type_options

    return {"business_types": business_type_options()}


@router.post("/api/signup", status_code=201)
def signup(body: SignupBody) -> dict[str, object]:
    # NEEDS-FAZAL: this is the SOLE, intentionally pre-auth owner-acquisition front
    # door. It has NO rate-limiting and NO proof-of-control of the whatsapp_number
    # (the regex is structural only). Two real gaps the review flagged: (1) unbounded
    # tenant/consent-row creation on the BYPASSRLS pool (flooding); (2) number
    # SQUATTING — first-writer-wins on the unique whatsapp_number, so an attacker can
    # permanently 409-brick a number they don't own. The proper fix is OTP /
    # proof-of-control BEFORE create (wire signup BEHIND the VT-250 owner-verify OTP)
    # + a per-IP throttle. That's a launch-blocking security decision for Fazal/VT-250,
    # NOT built in this PR. Do not expose /api/signup publicly until it lands.
    from orchestrator.onboarding.signup import SignupError, SignupInput, run_signup

    try:
        out = run_signup(SignupInput(**body.model_dump()))
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
