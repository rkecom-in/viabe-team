"""VT-408 — the hard GSTIN gate at SIGNUP (verify-then-create).

Fazal 2026-06-24 (CL-442, verbatim): *"We will be gating it hard — a no-GST business
doesn't get anything, neither paid nor trial."* The VT-361 activation gate (subscribe →
paid_active, transitions.py) is RETAINED as defense-in-depth; the PRIMARY gate now lives
here, at account-creation.

**Invariant:** no tenant reaches trial OR active without an ACTIVE GSTIN. Enforcement is
**verify-then-create** — the GSTIN is checked server-side BEFORE the ``tenants`` row is
created, so no unverified tenant is ever persisted (cleanest DPDP posture: nothing held for
a business we reject; CL-390/422).

This module is the PRE-tenant verify seam (the post-create owner-surface lookup stays in
``verification.py:run_lookup``). It has NO tenant_id — it gates the door, before a tenant
exists — so it cannot write the tenant-scoped ``kyc_verification_log``. It is STATELESS:
abuse throttling is the existing team-web per-IP + OTP-before-create backstop (route.ts),
NOT a new per-create ledger table (Cowork ruling 5/8, 2026-06-24 — no new schema; the
per-IP throttle is the abuse backstop and ``vendor_down`` is cap-exempt). PII-free by
construction: the raw GSTIN / business name never persist here; we return an outcome tag,
the verified result is consumed by ``create_signup_tenant`` only on the green path.

Three outcomes (do NOT conflate them — verification.py already separates them):
- ``gstin_verified`` — Sandbox returned an ACTIVE GSTIN with an authoritative name. PROCEED
  to create.
- ``vendor_down`` — Sandbox outage (``ok=False``). NOT a reject: an outage must never turn a
  legit GST business away. HOLD with a retry path; create NOTHING. Cap-exempt.
- ``invalid_gstin`` — the GSTIN is not an active registration (not-found OR inactive — the
  same generic copy for both; NO enumeration oracle). REJECT terminus; create NOTHING.

Fail-closed: any verify result that is not a confirmed ACTIVE GSTIN → no tenant created.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Outcome tags — mirror verification.py:run_lookup so ops sees one vocabulary.
GSTIN_VERIFIED = "gstin_verified"
VENDOR_DOWN = "vendor_down"
INVALID_GSTIN = "invalid_gstin"


@dataclass(frozen=True)
class SignupVerifyResult:
    """Outcome of the pre-create GSTIN verify. ``ok`` is True ONLY for an ACTIVE GSTIN with
    an authoritative name — the ONLY value that earns a tenant. ``retryable`` distinguishes a
    HOLD (vendor_down, try again) from a terminal REJECT (invalid_gstin)."""

    ok: bool
    outcome: str  # gstin_verified | vendor_down | invalid_gstin
    retryable: bool  # True ⇒ vendor_down HOLD (retry); False ⇒ reject / verified
    verified_name: str | None = None  # authoritative name — set only on gstin_verified
    gstin: str | None = None  # echoed only on gstin_verified (consumed by create)


# An injectable GSTIN search fn for tests: (gstin) -> a GstinLookup-shaped object exposing
# ``ok`` / ``is_active()`` / ``authoritative_name()``. Production passes None → the real
# ``sandbox_kyc.search_gstin``. This is the seam the unit tests drive WITHOUT live creds.
SearchFn = Callable[[str], Any]


def verify_gstin_for_signup(gstin: str, *, search_fn: SearchFn | None = None) -> SignupVerifyResult:
    """Pre-create GSTIN verify — the PRIMARY signup gate (VT-408). Fail-closed; tenant-less.

    Branches the Sandbox result into the three signup outcomes. Returns a
    ``SignupVerifyResult`` — it NEVER raises (a vendor exception is fail-closed to
    ``vendor_down`` upstream in ``search_gstin``, which already returns ``ok=False`` rather
    than raising). The caller (``run_signup`` / the reject-UX seam) decides create-vs-hold-vs-
    reject from ``result.ok`` / ``result.retryable``.
    """
    if not gstin or not gstin.strip():
        # No GSTIN supplied is a reject, not a hold — there is nothing to verify, and the
        # ruling is explicit: no GST ⇒ nothing. Terminal, generic copy (same as invalid).
        _emit_verify_event(
            failure_type="validation",
            operation="empty_gstin",
            error="No GSTIN supplied — terminal reject (no GST → no tenant)",
            impact="blocked_signup",
        )
        return SignupVerifyResult(ok=False, outcome=INVALID_GSTIN, retryable=False)

    if search_fn is None:
        # Provider seam (Fazal 2026-06-27): resolve the GST verifier via the adapter module, so the
        # planned Sandbox→reliable-provider swap is a gst_verifier change, not a signup-path edit.
        from orchestrator.onboarding.gst_verifier import default_gst_verifier

        search_fn = default_gst_verifier()

    result = search_fn(gstin)

    # vendor_down: the Sandbox call failed (ok=False). HOLD, do not reject — an outage must
    # not permanently turn away a legit GST business. Cap-exempt (Cowork ruling 5).
    if not getattr(result, "ok", False):
        logger.info("VT-408 signup gate: vendor_down — HOLD (no tenant created)")
        _emit_verify_event(
            failure_type="vendor_error",
            operation="vendor_down",
            error="Sandbox GST verify call failed (ok=False) — retryable HOLD",
            severity="error",
            impact=None,
            vendor="sandbox",
        )
        return SignupVerifyResult(ok=False, outcome=VENDOR_DOWN, retryable=True)

    # invalid_gstin: the call succeeded but the GSTIN is not an active registration (inactive
    # OR not-found — indistinguishable to the caller on purpose; no enumeration oracle).
    if not result.is_active() or not result.authoritative_name():
        logger.info("VT-408 signup gate: invalid_gstin — REJECT (no tenant created)")
        _emit_verify_event(
            failure_type="validation",
            operation="invalid_gstin",
            error="GSTIN not active or no authoritative name — terminal reject",
            impact="blocked_signup",
        )
        return SignupVerifyResult(ok=False, outcome=INVALID_GSTIN, retryable=False)

    # gstin_verified: ACTIVE + authoritative name. The ONLY path that earns a tenant.
    name = result.authoritative_name()
    logger.info("VT-408 signup gate: gstin_verified — PROCEED to create")
    return SignupVerifyResult(
        ok=True, outcome=GSTIN_VERIFIED, retryable=False, verified_name=name, gstin=gstin
    )


# --------------------------------------------------------------------------- #
# Reject / hold copy (bilingual, EN/HI — the freeform_acks ACK_COPY pattern).
# Rendered on the web signup form (the primary surface) AND as a WhatsApp reply to an unknown
# inbound number (tenant_provision closure, §3 #6). NO enumeration oracle: invalid_gstin uses
# ONE generic message for both not-found and inactive.
# --------------------------------------------------------------------------- #

# §5-approved copy (Cowork 2026-06-24). Latin tokens (Viabe / GST / GSTIN) kept in both langs.
SIGNUP_GATE_COPY: dict[str, dict[str, str]] = {
    # invalid_gstin → terminal reject. Generic: never leaks inactive-vs-not-found OR name-mismatch
    # vs inactive (no enumeration oracle). VT-510: changed "find" → "verify" so the copy is accurate
    # when the GSTIN is active but belongs to a genuinely different business (name-mismatch terminal).
    "reject": {
        "en": (
            "Viabe Team is for GST-registered businesses. We couldn't verify an active GST "
            "registration for this business. Viabe Team runs real business tasks on "
            "your behalf, so we verify every business's GST before starting — there's no trial "
            "or paid plan without it. If you have a GSTIN, double-check it and try again. If "
            "your business isn't GST-registered yet, we can't onboard you right now."
        ),
        "hi": (
            "Viabe Team GST-पंजीकृत व्यवसायों के लिए है। इस व्यवसाय के लिए हम कोई सक्रिय "
            "GST पंजीकरण सत्यापित नहीं कर सके। Viabe Team आपकी ओर से असली व्यावसायिक काम करता है, इसलिए शुरू "
            "करने से पहले हम हर व्यवसाय का GST सत्यापित करते हैं — इसके बिना कोई ट्रायल या पेड प्लान "
            "नहीं है। अगर आपके पास GSTIN है, तो उसे दोबारा जाँचें और फिर से प्रयास करें। अगर आपका "
            "व्यवसाय अभी GST-पंजीकृत नहीं है, तो हम अभी आपको ऑनबोर्ड नहीं कर सकते।"
        ),
    },
    # vendor_down → retryable HOLD. "On our side, try again" — distinct from the reject.
    "vendor_down": {
        "en": (
            "We couldn't verify your GST right now — this is on our side, not yours. Please try "
            "again in a moment."
        ),
        "hi": (
            "हम अभी आपका GST सत्यापित नहीं कर सके — यह हमारी ओर से समस्या है, आपकी नहीं। कृपया कुछ "
            "देर में फिर से प्रयास करें।"
        ),
    },
    # Unknown inbound WhatsApp number → direct them to the verified web signup (tenant_provision
    # closure). No tenant is created for an inbound stranger; signup is verify-first.
    "inbound_directive": {
        "en": (
            "Thanks for reaching out! To start with Viabe Team, please verify your GST-registered "
            "business at our signup — we onboard GST-registered businesses only."
        ),
        "hi": (
            "संपर्क करने के लिए धन्यवाद! Viabe Team शुरू करने के लिए, कृपया हमारे साइनअप पर अपना "
            "GST-पंजीकृत व्यवसाय सत्यापित करें — हम केवल GST-पंजीकृत व्यवसायों को ऑनबोर्ड करते हैं।"
        ),
    },
}

_SUPPORTED_LANGS = frozenset({"en", "hi"})


def gate_copy(kind: str, language: str) -> str:
    """Resolve a bilingual gate message (reject / vendor_down / inbound_directive). Unknown /
    unsupported language falls back to 'en' (the message must still render)."""
    variants = SIGNUP_GATE_COPY[kind]
    lang = language if language in _SUPPORTED_LANGS else "en"
    return variants.get(lang) or variants["en"]


# ---------------------------------------------------------------------------
# VT-515: debug event helper (inline — no tenant context at this gate)
# ---------------------------------------------------------------------------

def _emit_verify_event(
    *,
    failure_type: str,
    operation: str,
    error: str,
    severity: str = "error",
    impact: str | None = None,
    vendor: str | None = None,
) -> None:
    """Emit a debug_event for a signup-gate verify failure. Fail-soft — never raises."""
    try:
        from orchestrator.observability.debug_log import emit_debug_event

        emit_debug_event(
            failure_type=failure_type,
            component="verify",
            operation=operation,
            error=error,
            severity=severity,
            impact=impact,
            vendor=vendor,
            # No tenant_id or trace_id at this stage — the gate runs before a tenant exists
            # and without a discovery_id in scope. The viewer correlates via created_at window.
        )
    except Exception:  # noqa: BLE001 — never raise into the gate
        pass


__all__ = [
    "GSTIN_VERIFIED",
    "INVALID_GSTIN",
    "SIGNUP_GATE_COPY",
    "SignupVerifyResult",
    "VENDOR_DOWN",
    "gate_copy",
    "verify_gstin_for_signup",
]
