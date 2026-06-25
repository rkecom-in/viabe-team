"""VT-361 — Sandbox by Quicko business-verification client (two-tier; GSTIN lookup only).

Documented Sandbox contract (verified against developer.sandbox.co.in — #420 subagent bounce):
1. AUTH (two-step): POST /authenticate with headers x-api-key + x-api-secret + x-api-version → returns
   data.access_token (a JWT, ~24h). The token is passed in the `authorization` header WITHOUT the
   "Bearer" prefix. We cache it in-process (~23h) and re-auth on expiry or a 401.
2. LOOKUP: POST /gst/compliance/public/gstin/search with headers x-api-key + authorization=<token> +
   x-api-version, GSTIN in the BODY (not a query param). The GST record is nested TWO levels deep:
   {"data": {"data": {lgnm, tradeNam, sts, gstin, ...}, "status_cd": ...}} — read data.data, not data
   (the single-level read silently mapped every field to None; VT-361 live canary). An ACTIVE result
   alone earns gstin_verified (no ownership bind — Fazal two-tier ruling 2026-06-08).

The VERIFIED signal is name + status only (those alone earn ok/active). VT-407 widens the parse to
also capture rich business context from the SAME data.data record — principal/additional addresses,
constitution, nature-of-business, registration date, geo — as EXTRA context for the auto-discovery
draft (owner-confirmed later, never asserted here); their absence NEVER flips ok. Graceful-degrade:
absent creds / network / 4xx-5xx / parse → ok=False (NEVER raise, NEVER fake-verified). The caller
separates vendor-down (ok=False) from GSTIN-not-active (ok=True, status != active) so ops can tell an
outage from bad input.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("SANDBOX_BASE_URL", "https://api.sandbox.co.in")
_KEY_ENV = "SANDBOX_API_KEY"
_SECRET_ENV = "SANDBOX_API_SECRET"
_API_VERSION = "1.0"  # /authenticate (200s as-is — do not change)
_SEARCH_API_VERSION = "1.0.0"  # VT-409: the GSTIN search wants 1.0.0 (Fazal's proven-good curl); "1.0" mismatched
_AUTH_PATH = "/authenticate"
_SEARCH_PATH = "/gst/compliance/public/gstin/search"
# VT-448 identify (PRIMARY) — Search-GSTIN-by-PAN: POST ?state_code=<NN> with {pan} → the GSTIN(s)
# registered under the PAN in that state (one per state). PAN = 5 letters + 4 digits + 1 letter.
_PAN_SEARCH_PATH = "/gst/compliance/public/pan/search"
_PAN_FMT = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
_GSTIN_FMT = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b")
_TIMEOUT_S = 20.0
_TOKEN_TTL_S = 23 * 3600  # re-auth before the documented ~24h validity lapses

# In-process token cache (single secret pair). time.time() is fine in orchestrator runtime (the
# Date.now/time ban is a Workflow-SCRIPT restriction, not application code).
_token: str | None = None
_token_exp: float = 0.0

# Injectable request transport for tests: (method, path, headers, body) -> json dict. A pinned-shape
# unit test asserts the method/headers/body WITHOUT live creds (the structural fix for the
# "canary present but never green" failure mode).
RequestFn = Callable[[str, str, dict[str, str], dict[str, Any] | None], dict[str, Any]]


@dataclass(frozen=True)
class GstinLookup:
    """Result-only GSTIN lookup. ok=False on any vendor failure (fail-closed → vendor_down).

    VT-407 widen: the verified signal is STILL name + status — those alone earn ``ok``/active.
    The richer GST fields below (principal address, constitution, nature-of-business, reg-date,
    additional addresses, geo) are EXTRA CONTEXT pulled from the same ``data.data`` record; their
    absence NEVER flips ``ok`` (every one is optional → None/[]). They feed the auto-discovery
    DRAFT (owner-confirmed later), never asserted as fact here. ``business_fields()`` packages the
    business-level extras for the draft. ``legal_name`` is deliberately NOT in that dict: for a
    proprietorship ``lgnm`` is a natural person's name (PII) — the caller gates it on constitution
    (CL-390/425, DPDP)."""

    ok: bool
    legal_name: str | None = None
    trade_name: str | None = None
    status: str | None = None  # 'Active' etc. — only Active earns gstin_verified
    # VT-407 — rich GST context (extra only; absence never flips ok)
    principal_address: str | None = None  # composed from pradr.addr subfields
    geo_lat: str | None = None  # pradr.addr.lt
    geo_lng: str | None = None  # pradr.addr.lg
    constitution: str | None = None  # ctb — e.g. 'Proprietorship', 'Private Limited Company'
    nature_of_business: list[str] = field(default_factory=list)  # nba — list of activities
    registration_date: str | None = None  # rgdt
    additional_addresses: tuple[str, ...] = ()  # adadr[].addr composed (frozen-safe → tuple)

    def is_active(self) -> bool:
        return self.ok and (self.status or "").strip().lower() == "active"

    def authoritative_name(self) -> str | None:
        return self.trade_name or self.legal_name

    def is_proprietorship(self) -> bool:
        """True when the constitution is a sole proprietorship — for which ``lgnm`` is a natural
        person's name (personal PII), NOT a business name. discover_gst gates whether legal_name
        may be written to the draft on this (CL-390/425)."""
        return "propriet" in (self.constitution or "").strip().lower()

    def business_fields(self) -> dict[str, Any]:
        """Business-level extras suitable for the auto-discovery DRAFT. DELIBERATELY EXCLUDES
        legal_name (PII for a proprietorship; the caller decides, gated on constitution) and every
        person-level field (director/proprietor name, DIN, PAN). Only non-empty values included."""
        out: dict[str, Any] = {
            "trade_name": self.trade_name,
            "principal_address": self.principal_address,
            "constitution": self.constitution,
            "registration_date": self.registration_date,
            "geo_lat": self.geo_lat,
            "geo_lng": self.geo_lng,
        }
        fields = {k: v for k, v in out.items() if v}
        if self.nature_of_business:
            fields["nature_of_business"] = list(self.nature_of_business)
        if self.additional_addresses:
            fields["additional_addresses"] = list(self.additional_addresses)
        return fields


def _creds() -> tuple[str, str] | None:
    key, secret = os.environ.get(_KEY_ENV, ""), os.environ.get(_SECRET_ENV, "")
    if not key or not secret:
        logger.warning("sandbox_kyc: %s/%s absent — fail-closed", _KEY_ENV, _SECRET_ENV)
        return None
    return key, secret


def _default_request(
    method: str, path: str, headers: dict[str, str], body: dict[str, Any] | None
) -> dict[str, Any]:
    import httpx

    resp = httpx.request(
        method, f"{_BASE_URL}{path}", headers={**headers, "accept": "application/json"},
        json=body, timeout=_TIMEOUT_S,
    )
    resp.raise_for_status()
    return dict(resp.json())


def _authenticate(key: str, secret: str, request_fn: RequestFn) -> str | None:
    """POST /authenticate → access_token. Returns None on failure (caller fails closed)."""
    raw = request_fn(
        "POST", _AUTH_PATH,
        {"x-api-key": key, "x-api-secret": secret, "x-api-version": _API_VERSION},
        None,
    )
    # VT-409: Sandbox returns the token at BOTH top-level `access_token` (the one that WORKS) AND nested
    # `data.access_token` (which 500s on the subsequent /search) — a contract quirk found 2026-06-24 via
    # Fazal's direct curl. The old `raw.get("data", raw).get("access_token")` grabbed the NESTED (dud)
    # token, so every auth 200'd but every search Internal-Server-Error'd. Prefer top-level; fall back
    # to nested only for back-compat with a response that omits the top-level key.
    token = raw.get("access_token") or (raw.get("data") or {}).get("access_token")
    return _clean(token)


def _get_token(key: str, secret: str, request_fn: RequestFn, *, force: bool = False) -> str | None:
    global _token, _token_exp
    if not force and _token and time.time() < _token_exp:
        return _token
    _token = _authenticate(key, secret, request_fn)
    _token_exp = time.time() + _TOKEN_TTL_S if _token else 0.0
    return _token


def search_gstin(gstin: str, *, request_fn: RequestFn | None = None) -> GstinLookup:
    """Two-step (auth → lookup) GSTIN search → authoritative name + status. Fail-closed (ok=False) on
    any error. Result-only. Re-auths once on a 401 (stale token)."""
    creds = _creds()
    if creds is None:
        return GstinLookup(ok=False)
    key, secret = creds
    req = request_fn or _default_request
    try:
        token = _get_token(key, secret, req)
        if not token:
            return GstinLookup(ok=False)
        try:
            raw = _lookup(req, key, token, gstin)
        except Exception as exc:  # noqa: BLE001
            if _is_401(exc):  # stale token → re-auth once + retry
                token = _get_token(key, secret, req, force=True)
                if not token:
                    return GstinLookup(ok=False)
                raw = _lookup(req, key, token, gstin)
            else:
                raise
        # Sandbox nests the GST record one level deeper than the envelope: the lookup body is
        # {"data": {"data": {lgnm, tradeNam, sts, ...}, "status_cd": ...}}. The single-level read
        # silently produced ok=True with all-None fields — a real 200 that read as unverified
        # (caught by the VT-361 live canary; the mocked shape test had pinned the wrong shape).
        env = raw.get("data", raw)
        record = env.get("data", env) if isinstance(env, dict) else env
        if not isinstance(record, dict):
            record = {}
        legal_name = _clean(record.get("legal_name") or record.get("lgnm"))
        trade_name = _clean(record.get("trade_name") or record.get("tradeNam"))
        status = _clean(record.get("status") or record.get("sts"))
        # Fail-closed on shape drift: a 200 we cannot parse into a name/status MUST NOT read as
        # verified — the module contract is "parse → ok=False, NEVER fake-verified". The richer
        # fields below are EXTRA context only; their absence does NOT participate in this gate
        # (a record carrying only name+status is still a fully valid verified result).
        if legal_name is None and trade_name is None and status is None:
            logger.error("sandbox_kyc: lookup 200 but unparseable name/status — shape drift, fail-closed")
            return GstinLookup(ok=False)
        # VT-407 — widen: pull the rich business context from the SAME record. Every field is
        # defensively optional (missing → None/[]); a parse miss here NEVER flips ok.
        pradr = record.get("pradr") if isinstance(record.get("pradr"), dict) else {}
        paddr = pradr.get("addr") if isinstance(pradr.get("addr"), dict) else {}
        return GstinLookup(
            ok=True,
            legal_name=legal_name,
            trade_name=trade_name,
            status=status,
            principal_address=_compose_address(paddr),
            geo_lat=_clean(paddr.get("lt")),
            geo_lng=_clean(paddr.get("lg")),
            constitution=_clean(record.get("constitution") or record.get("ctb")),
            nature_of_business=_nature_of_business(record.get("nba")),
            registration_date=_clean(record.get("registration_date") or record.get("rgdt")),
            additional_addresses=_additional_addresses(record.get("adadr")),
        )
    except Exception:
        logger.exception("sandbox_kyc: search_gstin failed (fail-closed → vendor_down)")
        return GstinLookup(ok=False)


def _lookup(req: RequestFn, key: str, token: str, gstin: str) -> dict[str, Any]:
    return req(
        "POST", _SEARCH_PATH,
        {"x-api-key": key, "authorization": token, "x-api-version": _SEARCH_API_VERSION},
        {"gstin": gstin},
    )


@dataclass(frozen=True)
class PanGstinResult:
    """The GSTIN(s) registered under a PAN+state (VT-448 identify PRIMARY). ``ok`` = call success;
    ``gstins`` = the (possibly empty) ordered-unique list the owner PICKS from + we then verify."""

    ok: bool
    gstins: tuple[str, ...] = ()


def _lookup_by_pan(req: RequestFn, key: str, token: str, pan: str, state_code: str) -> dict[str, Any]:
    return req(
        "POST", f"{_PAN_SEARCH_PATH}?state_code={state_code}",
        {"x-api-key": key, "authorization": token, "x-api-version": _SEARCH_API_VERSION},
        {"pan": pan},
    )


def _extract_gstins(obj: Any) -> list[str]:
    """Recursively collect GSTIN-format strings from a nested vendor response (shape-tolerant — the
    success envelope nests a level or two deep, like the gstin search; never depend on its exact shape)."""
    found: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, str):
            found.extend(_GSTIN_FMT.findall(o.upper()))
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)

    walk(obj)
    return list(dict.fromkeys(found))  # ordered-unique


def search_gstins_by_pan(pan: str, state_code: str, *, request_fn: RequestFn | None = None) -> PanGstinResult:
    """Search-GSTIN-by-PAN (Sandbox ``/gst/compliance/public/pan/search?state_code=NN`` with {pan}) → the
    GSTIN(s) registered under the PAN in that state. Fail-closed (ok=False) on any error / bad input. The
    identify-and-confirm PRIMARY: PAN → GSTIN list → owner PICKS → ``search_gstin`` verify + name-match →
    gstin_verified (no 15-char GSTIN typing). ``request_fn`` injectable for tests (no live creds)."""
    pan = (pan or "").strip().upper()
    state_code = (state_code or "").strip()
    if not _PAN_FMT.fullmatch(pan) or not state_code:
        return PanGstinResult(ok=False)
    creds = _creds()
    if creds is None:
        return PanGstinResult(ok=False)
    key, secret = creds
    req = request_fn or _default_request
    try:
        token = _get_token(key, secret, req)
        if not token:
            return PanGstinResult(ok=False)
        try:
            raw = _lookup_by_pan(req, key, token, pan, state_code)
        except Exception as exc:  # noqa: BLE001
            if _is_401(exc):  # stale token → re-auth once + retry
                token = _get_token(key, secret, req, force=True)
                if not token:
                    return PanGstinResult(ok=False)
                raw = _lookup_by_pan(req, key, token, pan, state_code)
            else:
                raise
        return PanGstinResult(ok=True, gstins=tuple(_extract_gstins(raw)))
    except Exception:
        logger.exception("sandbox_kyc: search_gstins_by_pan failed (fail-closed)")
        return PanGstinResult(ok=False)


def _is_401(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) == 401


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# VT-407 — GSTN address subfield order for a readable single-line compose. The GST `addr` object
# uses these short keys; we join the present, non-empty ones in postal order. (Door/floor, building
# name, street, locality, district, state, PIN.) `lt`/`lg` are geo, handled separately; `landMark`
# is included for completeness.
_ADDR_SUBFIELDS = ("flno", "bno", "bnm", "st", "loc", "landMark", "dst", "stcd", "pncd")


def _clean_addr_subfield(v: Any) -> str | None:
    """Clean a single GST address subfield for joining. Strips surrounding whitespace AND
    separator punctuation (commas) so a subfield that is only a comma — GSTN routinely returns
    ``flno: ','`` for businesses with no floor number — collapses to None instead of injecting a
    leading/doubled comma into the joined address (live RKECOM canary: ``flno=','`` produced the
    cosmetic ``',, A/403, ...'``). A meaningful internal comma is preserved (only the edges are
    trimmed)."""
    if v is None:
        return None
    s = str(v).strip().strip(",").strip()
    return s or None


def _compose_address(addr: Any) -> str | None:
    """Compose a readable single-line address from a GSTN ``addr`` dict (pradr.addr / adadr[].addr).
    Defensive: non-dict or no usable subfields → None. Order = postal; empty / comma-only subfields
    dropped (so there are no leading or doubled commas in the result)."""
    if not isinstance(addr, dict):
        return None
    parts = [s for k in _ADDR_SUBFIELDS if (s := _clean_addr_subfield(addr.get(k)))]
    return ", ".join(parts) or None


def _additional_addresses(adadr: Any) -> tuple[str, ...]:
    """Compose each additional place-of-business address (``adadr[].addr``) to a string. Defensive:
    non-list / malformed entries skipped; empties dropped. Returns a tuple (frozen-dataclass-safe)."""
    if not isinstance(adadr, list):
        return ()
    out = []
    for entry in adadr:
        addr = entry.get("addr") if isinstance(entry, dict) else None
        composed = _compose_address(addr)
        if composed:
            out.append(composed)
    return tuple(out)


def _nature_of_business(nba: Any) -> list[str]:
    """Normalise the nature-of-business field (``nba``) to a list of clean strings. The GST shape is
    typically a list of activity strings, but a single string is tolerated. Defensive → []."""
    if isinstance(nba, str):
        cleaned = _clean(nba)
        return [cleaned] if cleaned else []
    if isinstance(nba, list):
        return [c for v in nba if (c := _clean(v))]
    return []
