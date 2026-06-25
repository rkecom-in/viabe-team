"""VT-449 — Sandbox MCA Master Data (Company + Director). Reuses the GST 2-step Sandbox auth (same
vendor + creds). Company Master Data validates + enriches from a CIN (canonical name, status/compliance,
financials, registered address, directors); Director Master Data lists the companies a DIN directs (the
VT-411 KYC-grade ownership signal). Fail-closed (ok=False) on absent creds / network / 4xx-5xx / parse —
NEVER raise into onboarding.

PRIVACY (BINDING — CL-390/425/426/104): the directors[] (name + din/pan) + the registered address are
PERSONAL/identifying data — this module RETURNS them parsed; the CALLER encrypts at rest, keeps them out
of the VTR raw + names-only to any LLM, and stores them DSR-purgeable. ``consent='y'`` + a ``reason``
(≥20 chars) ride EVERY call (DPDP) — the caller records the owner's consent to the KYC lookup.

Endpoint = prod ``api.sandbox.co.in`` for REAL data (``test-api`` returns mock). Headers: Authorization
(the JWT from /authenticate, no 'Bearer'), x-api-key, x-api-version 1.0.0 (mirrors the GST search).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from orchestrator.integrations.methods import sandbox_kyc as _sk

logger = logging.getLogger(__name__)

_COMPANY_PATH = "/mca/company/master-data/search"
_DIRECTOR_PATH = "/mca/director/master-data/search"
_ENTITY = "in.co.sandbox.kyc.mca.master_data.request"
_MCA_API_VERSION = "1.0.0"
MIN_REASON_LEN = 20  # DPDP: a human-readable purpose, ≥20 chars, on every MCA call


@dataclass(frozen=True)
class CompanyMasterData:
    """Parsed MCA Company Master Data. ``company_name`` is the AUTHORITATIVE registry name — the
    name-match anchor for GST identify. ``directors`` + ``registered_address`` are PII (caller encrypts)."""

    ok: bool
    cin: str | None = None
    company_name: str | None = None          # canonical registry name (the name-match anchor)
    status: str | None = None                # company_status (for_efiling)
    active_compliance: str | None = None
    class_of_company: str | None = None
    company_category: str | None = None
    registered_address: str | None = None    # PII — caller encrypts
    roc_code: str | None = None
    date_of_incorporation: str | None = None
    paid_up_capital: str | None = None       # company financial (non-PII)
    authorised_capital: str | None = None
    directors: tuple[dict[str, Any], ...] = ()  # [{name, din, pan, designation}] — PII; caller encrypts


@dataclass(frozen=True)
class DirectorMasterData:
    """Parsed MCA Director Master Data. ``name`` is PII. ``companies`` is every company the DIN directs —
    the VT-411 KYC ownership check is ``directs_cin(<this company's CIN>)``."""

    ok: bool
    din: str | None = None
    name: str | None = None                       # PII
    companies: tuple[dict[str, Any], ...] = ()    # [{company_name, cin, designation}]

    def directs_cin(self, cin: str) -> bool:
        """VT-411 KYC ownership: True iff this DIN is a director of the company with this CIN."""
        c = (cin or "").strip().upper()
        return bool(c) and any((co.get("cin") or "").strip().upper() == c for co in self.companies)


def _mca_call(path: str, identifier: str, reason: str, request_fn: _sk.RequestFn | None) -> dict[str, Any] | None:
    """Auth (reuse the GST 2-step) → POST the MCA search. Returns the raw JSON, or None on any failure
    (fail-closed). ``reason`` must be ≥20 chars (DPDP); a short reason returns None without a call."""
    if len((reason or "").strip()) < MIN_REASON_LEN:
        logger.warning("mca: reason too short (<%d) — refusing the call", MIN_REASON_LEN)
        return None
    creds = _sk._creds()
    if creds is None:
        return None
    key, secret = creds
    req = request_fn or _sk._default_request
    body = {"@entity": _ENTITY, "id": identifier, "consent": "y", "reason": reason}
    headers = {"x-api-key": key, "x-api-version": _MCA_API_VERSION}
    try:
        token = _sk._get_token(key, secret, req)
        if not token:
            return None
        try:
            return req("POST", path, {**headers, "authorization": token}, body)
        except Exception as exc:  # noqa: BLE001
            if _sk._is_401(exc):  # stale token → re-auth once + retry
                token = _sk._get_token(key, secret, req, force=True)
                if not token:
                    return None
                return req("POST", path, {**headers, "authorization": token}, body)
            raise
    except Exception:
        logger.exception("mca: %s call failed (fail-closed)", path)
        return None


def _company_directors(env: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    raw = env.get("directors") or env.get("signatory_details") or []
    if not isinstance(raw, list):
        return ()
    out: list[dict[str, Any]] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        out.append({
            "name": _sk._clean(d.get("name")),
            "din": _sk._clean(d.get("din") or d.get("pan")),
            "designation": _sk._clean(d.get("designation")),
        })
    return tuple(out)


def company_master_data(cin: str, *, reason: str, request_fn: _sk.RequestFn | None = None) -> CompanyMasterData:
    """MCA Company Master Data by CIN → validation + enrichment + the canonical name. Fail-closed."""
    cin = (cin or "").strip().upper()
    if not cin:
        return CompanyMasterData(ok=False)
    raw = _mca_call(_COMPANY_PATH, cin, reason, request_fn)
    if not isinstance(raw, dict):
        return CompanyMasterData(ok=False)
    # Sandbox nests under data.company_master_data (+ data.directors). Tolerate a flatter shape too.
    data = raw.get("data", raw) if isinstance(raw.get("data"), dict) else raw
    env = data.get("company_master_data", data) if isinstance(data.get("company_master_data"), dict) else data
    if not isinstance(env, dict):
        return CompanyMasterData(ok=False)
    company_name = _sk._clean(env.get("company_name"))
    if company_name is None and _sk._clean(env.get("cin")) is None:
        return CompanyMasterData(ok=False)  # unparseable / not-found → fail-closed
    return CompanyMasterData(
        ok=True,
        cin=_sk._clean(env.get("cin")) or cin,
        company_name=company_name,
        status=_sk._clean(env.get("company_status") or env.get("status") or env.get("for_efiling")),
        active_compliance=_sk._clean(env.get("active_compliance")),
        class_of_company=_sk._clean(env.get("class_of_company")),
        company_category=_sk._clean(env.get("company_category")),
        registered_address=_sk._clean(env.get("registered_address")),
        roc_code=_sk._clean(env.get("roc_code")),
        date_of_incorporation=_sk._clean(env.get("date_of_incorporation")),
        paid_up_capital=_sk._clean(env.get("paid_up_capital")),
        authorised_capital=_sk._clean(env.get("authorised_capital")),
        directors=_company_directors(data if "directors" in data else env),
    )


def director_master_data(din: str, *, reason: str, request_fn: _sk.RequestFn | None = None) -> DirectorMasterData:
    """MCA Director Master Data by DIN → the director's name + the companies they direct. Fail-closed."""
    din = (din or "").strip().upper()
    if not din:
        return DirectorMasterData(ok=False)
    raw = _mca_call(_DIRECTOR_PATH, din, reason, request_fn)
    if not isinstance(raw, dict):
        return DirectorMasterData(ok=False)
    data = raw.get("data", raw) if isinstance(raw.get("data"), dict) else raw
    dd = data.get("director_data", data) if isinstance(data.get("director_data"), dict) else data
    companies_raw = data.get("company_data") or (dd.get("company_data") if isinstance(dd, dict) else None) or []
    name = _sk._clean(dd.get("name")) if isinstance(dd, dict) else None
    if name is None and not companies_raw:
        return DirectorMasterData(ok=False)
    companies: list[dict[str, Any]] = []
    if isinstance(companies_raw, list):
        for co in companies_raw:
            if isinstance(co, dict):
                companies.append({
                    "company_name": _sk._clean(co.get("company_name")),
                    "cin": _sk._clean(co.get("cin") or co.get("fcrn")),
                    "designation": _sk._clean(co.get("designation")),
                })
    return DirectorMasterData(
        ok=True,
        din=_sk._clean(dd.get("din")) if isinstance(dd, dict) else din,
        name=name,
        companies=tuple(companies),
    )
