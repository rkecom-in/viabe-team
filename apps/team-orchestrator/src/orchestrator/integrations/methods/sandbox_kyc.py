"""VT-361 — Sandbox by Quicko business-verification client (Option F).

Two vendor calls, both ORCHESTRATOR-side (never team-web), both fail-closed + result-only:

1. ``search_gstin(gstin)`` — Sandbox public GSTIN search (no taxpayer auth). Returns the authoritative
   legal/trade name + status + constitution. Validates EXISTENCE; on its own it is NOT proof of
   ownership (anyone can type a public GSTIN — knowledge, not control).

2. The REVERSE penny-drop (Fazal 2026-06-08): the owner pays ₹1 via UPI; the vendor returns the
   PAYER's bank-registered name. We match that name against the GSTIN/claimed name. Zero financial
   data collected from the owner (no account number / IFSC) — strictly better DPDP posture than a
   forward penny-drop. ``initiate_reverse_penny_drop()`` returns a reference + the UPI payment
   handle; ``poll_reverse_penny_drop(reference)`` returns the payer name once paid.

GST-OTP bind: EVALUATED-AND-DEAD (no accessible API — Fazal 2026-06-08). Not built.

Result-only: we parse ONLY the fields needed for the name-match (name + status) and DISCARD the rest
of the vendor response — it never reaches storage or logs. Graceful-degrade: any vendor failure
(absent creds, network, 4xx/5xx, parse) → return a sentinel result, NEVER raise, NEVER fake-verified.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("SANDBOX_BASE_URL", "https://api.sandbox.co.in")
_KEY_ENV = "SANDBOX_API_KEY"
_SECRET_ENV = "SANDBOX_API_SECRET"
_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class GstinLookup:
    """Result-only GSTIN lookup. ok=False on any vendor failure (fail-closed)."""

    ok: bool
    legal_name: str | None = None
    trade_name: str | None = None
    status: str | None = None  # 'Active' etc.
    constitution: str | None = None  # 'Proprietorship' etc. — drives the name-match case


@dataclass(frozen=True)
class ReversePennyDrop:
    """Result-only reverse-penny-drop. ok=False on failure; payer_name None until paid."""

    ok: bool
    reference: str | None = None
    upi_handle: str | None = None  # where the owner sends ₹1 (initiate only)
    payer_name: str | None = None  # bank-registered name (poll only) — matched then discarded


def _creds() -> tuple[str, str] | None:
    key, secret = os.environ.get(_KEY_ENV, ""), os.environ.get(_SECRET_ENV, "")
    if not key or not secret:
        logger.warning("sandbox_kyc: %s/%s absent — fail-closed", _KEY_ENV, _SECRET_ENV)
        return None
    return key, secret


def _default_get(path: str, key: str, secret: str, params: dict[str, Any]) -> dict[str, Any]:
    import httpx

    resp = httpx.get(
        f"{_BASE_URL}{path}",
        headers={"x-api-key": key, "x-api-secret": secret, "accept": "application/json"},
        params=params,
        timeout=_TIMEOUT_S,
    )
    resp.raise_for_status()
    return dict(resp.json())


def _default_post(path: str, key: str, secret: str, body: dict[str, Any]) -> dict[str, Any]:
    import httpx

    resp = httpx.post(
        f"{_BASE_URL}{path}",
        headers={"x-api-key": key, "x-api-secret": secret, "accept": "application/json"},
        json=body,
        timeout=_TIMEOUT_S,
    )
    resp.raise_for_status()
    return dict(resp.json())


def search_gstin(
    gstin: str,
    *,
    get_fn: Callable[[str, str, str, dict[str, Any]], dict[str, Any]] | None = None,
) -> GstinLookup:
    """Public GSTIN search → authoritative name. Fail-closed (ok=False) on any error. Result-only."""
    creds = _creds()
    if creds is None:
        return GstinLookup(ok=False)
    key, secret = creds
    try:
        raw = (get_fn or _default_get)(
            "/gst/compliance/public/gstin/search", key, secret, {"gstin": gstin}
        )
        # Parse ONLY the match-relevant fields; discard the rest (result-only).
        data = raw.get("data", raw)
        return GstinLookup(
            ok=True,
            legal_name=_clean(data.get("legal_name") or data.get("lgnm")),
            trade_name=_clean(data.get("trade_name") or data.get("tradeNam")),
            status=_clean(data.get("status") or data.get("sts")),
            constitution=_clean(data.get("constitution_of_business") or data.get("ctb")),
        )
    except Exception:
        logger.exception("sandbox_kyc: search_gstin failed (fail-closed)")
        return GstinLookup(ok=False)


def initiate_reverse_penny_drop(
    *,
    post_fn: Callable[[str, str, str, dict[str, Any]], dict[str, Any]] | None = None,
) -> ReversePennyDrop:
    """Start a reverse penny-drop — owner will pay ₹1 to the returned UPI handle. No owner financial
    data collected. Fail-closed on error."""
    creds = _creds()
    if creds is None:
        return ReversePennyDrop(ok=False)
    key, secret = creds
    try:
        raw = (post_fn or _default_post)("/kyc/reverse-penny-drop", key, secret, {})
        data = raw.get("data", raw)
        return ReversePennyDrop(
            ok=True,
            reference=_clean(data.get("reference") or data.get("id")),
            upi_handle=_clean(data.get("upi") or data.get("upi_handle")),
        )
    except Exception:
        logger.exception("sandbox_kyc: initiate_reverse_penny_drop failed (fail-closed)")
        return ReversePennyDrop(ok=False)


def poll_reverse_penny_drop(
    reference: str,
    *,
    get_fn: Callable[[str, str, str, dict[str, Any]], dict[str, Any]] | None = None,
) -> ReversePennyDrop:
    """Poll a reverse penny-drop → the PAYER's bank-registered name once paid. We keep ONLY the name
    (for the match) and discard everything else. payer_name None until paid. Fail-closed."""
    creds = _creds()
    if creds is None:
        return ReversePennyDrop(ok=False, reference=reference)
    key, secret = creds
    try:
        raw = (get_fn or _default_get)(
            "/kyc/reverse-penny-drop/status", key, secret, {"reference": reference}
        )
        data = raw.get("data", raw)
        return ReversePennyDrop(
            ok=True,
            reference=reference,
            payer_name=_clean(data.get("name") or data.get("name_at_bank") or data.get("payer_name")),
        )
    except Exception:
        logger.exception("sandbox_kyc: poll_reverse_penny_drop failed (fail-closed)")
        return ReversePennyDrop(ok=False, reference=reference)


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
