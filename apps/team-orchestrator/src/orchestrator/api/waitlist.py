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
import logging
import os
import re
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from orchestrator.graph import get_pool

router = APIRouter()
logger = logging.getLogger(__name__)

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
    body: WaitlistBody, x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret")
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
    email: str | None = None,
    whatsapp_e164: str | None = None,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    """VT-97 #2 / VT-354 NIT-3 — ops erasure: HARD-delete a waitlist entry by email OR
    whatsapp_e164 (a principal exercising erasure may know only their number). At least one
    identifier is required. Returns the deleted count (0 if not present — ops-only, no
    enumeration concern)."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="invalid internal secret")
    email_n = email.strip().lower() if email else None
    phone_n = whatsapp_e164.strip() if whatsapp_e164 else None
    if not email_n and not phone_n:
        raise HTTPException(status_code=400, detail="email or whatsapp_e164 required")
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM waitlist_signups "
            "WHERE (%s::text IS NOT NULL AND email = %s) "
            "OR (%s::text IS NOT NULL AND whatsapp_e164 = %s)",
            (email_n, email_n, phone_n, phone_n),
        )
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


def run_waitlist_retention_purge(*, months: int = 6) -> int:
    """VT-354: the SCHEDULED retention enforcer (wired to a daily DBOS job). Hard-deletes
    un-notified waitlist rows older than ``months`` (the DPDP 6-month bound) so pre-launch PII
    never sits unbounded — the bound is now ENFORCED, not runbook-manual. Idempotent + safe on an
    empty/disabled waitlist (0 rows → 0 deleted). Returns + logs the deleted count."""
    deleted = purge_stale_unnotified(months=months)
    logger.info("VT-354 waitlist retention purge: deleted=%d (months=%d)", deleted, months)
    return deleted
