"""VT-97 — /api/waitlist (pre-launch waitlist capture + its OWN erasure path).

Pre-tenant, purpose-limited PII (email + WhatsApp): the sole use is the launch
announcement (CL-390). Collected only behind the `waitlist` launch mode + the
X-Internal-Secret (team-web) — the same BYPASSRLS bootstrap-surface pattern as /api/signup.

waitlist_signups is NOT in the tenant DSR `_PURGE_ORDER` (no tenant_id). Its erasure path is
its OWN: the DELETE here (ops erasure) + the post-notify / retention purge fns below. See
docs/policy/waitlist-data.md.

CL-422: real waitlist PII is gated on VT-231 (Mumbai prod) + Fazal, exactly like
ENABLE_PUBLIC_SIGNUP — the surface stays dark on dev (Seoul).
"""

from __future__ import annotations

import hmac
import os
import re
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from orchestrator.graph import get_pool

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+91[6-9]\d{9}$")


def _verify_internal_secret(provided: str | None) -> bool:
    """Only team-web (which holds INTERNAL_API_SECRET) may reach this BYPASSRLS surface —
    a constant-time match (CL-72 / the /api/signup pattern)."""
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


class WaitlistBody(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    whatsapp_e164: str = Field(..., min_length=1, max_length=20)
    referral_source: str | None = Field(default=None, max_length=120)
    # VT-97 #1: DPDP consent is mandatory AT COLLECTION (the form gates it; re-checked here).
    consent: bool


@router.post("/api/waitlist")
def join_waitlist(
    body: WaitlistBody, x_internal_secret: str | None = Header(default=None)
) -> dict[str, Any]:
    """Capture a waitlist entry. Idempotent: a re-submit (same email OR number) is a no-op
    and ALWAYS returns ``queued`` — never leaks whether the entry already existed (no
    enumeration). consent_at is stamped server-side."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="invalid internal secret")
    if not body.consent:
        raise HTTPException(status_code=400, detail="consent required")
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="invalid email")
    if not _PHONE_RE.match(body.whatsapp_e164):
        raise HTTPException(status_code=400, detail="invalid whatsapp number")
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO waitlist_signups (email, whatsapp_e164, referral_source, consent_at) "
            "VALUES (%s, %s, %s, now()) "
            "ON CONFLICT DO NOTHING",  # any unique conflict (email or number) → idempotent no-op
            (email, body.whatsapp_e164, body.referral_source),
        )
    return {"status": "queued"}


@router.delete("/api/waitlist")
def erase_waitlist(
    email: str, x_internal_secret: str | None = Header(default=None)
) -> dict[str, Any]:
    """VT-97 #2 — ops erasure: HARD-delete a waitlist entry by email (an explicit erasure
    request). Returns the deleted count (0 if not present — no enumeration concern, ops-only)."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="invalid internal secret")
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM waitlist_signups WHERE email = %s", (email.strip().lower(),))
        deleted = cur.rowcount
    return {"deleted": deleted}


def purge_notified_waitlist() -> int:
    """Ops sweep: hard-delete rows already notified at launch (notified_at set). Run after the
    launch announcement sweep — purpose fulfilled, PII no longer needed (CL-390)."""
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM waitlist_signups WHERE notified_at IS NOT NULL")
        return cur.rowcount


def purge_stale_unnotified(months: int = 6) -> int:
    """VT-97 #2 retention bound: hard-delete UN-notified rows older than ``months`` (launch
    slipped) so pre-launch PII never sits unbounded."""
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM waitlist_signups WHERE notified_at IS NULL "
            "AND created_at < now() - make_interval(months => %s)",
            (months,),
        )
        return cur.rowcount
