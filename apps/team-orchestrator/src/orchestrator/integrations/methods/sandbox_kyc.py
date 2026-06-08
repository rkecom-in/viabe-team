"""VT-361 — Sandbox by Quicko business-verification client (two-tier; GSTIN lookup only).

Documented Sandbox contract (verified against developer.sandbox.co.in — #420 subagent bounce):
1. AUTH (two-step): POST /authenticate with headers x-api-key + x-api-secret + x-api-version → returns
   data.access_token (a JWT, ~24h). The token is passed in the `authorization` header WITHOUT the
   "Bearer" prefix. We cache it in-process (~23h) and re-auth on expiry or a 401.
2. LOOKUP: POST /gst/compliance/public/gstin/search with headers x-api-key + authorization=<token> +
   x-api-version, GSTIN in the BODY (not a query param). An ACTIVE result alone earns gstin_verified
   (no ownership bind — Fazal two-tier ruling 2026-06-08).

Result-only: parse ONLY name + status, DISCARD the rest. Graceful-degrade: absent creds / network /
4xx-5xx / parse → ok=False (NEVER raise, NEVER fake-verified). The caller separates vendor-down
(ok=False) from GSTIN-not-active (ok=True, status != active) so ops can tell an outage from bad input.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("SANDBOX_BASE_URL", "https://api.sandbox.co.in")
_KEY_ENV = "SANDBOX_API_KEY"
_SECRET_ENV = "SANDBOX_API_SECRET"
_API_VERSION = "1.0"
_AUTH_PATH = "/authenticate"
_SEARCH_PATH = "/gst/compliance/public/gstin/search"
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
    """Result-only GSTIN lookup. ok=False on any vendor failure (fail-closed → vendor_down)."""

    ok: bool
    legal_name: str | None = None
    trade_name: str | None = None
    status: str | None = None  # 'Active' etc. — only Active earns gstin_verified

    def is_active(self) -> bool:
        return self.ok and (self.status or "").strip().lower() == "active"

    def authoritative_name(self) -> str | None:
        return self.trade_name or self.legal_name


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
    data = raw.get("data", raw)
    return _clean(data.get("access_token"))


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
        data = raw.get("data", raw)
        return GstinLookup(
            ok=True,
            legal_name=_clean(data.get("legal_name") or data.get("lgnm")),
            trade_name=_clean(data.get("trade_name") or data.get("tradeNam")),
            status=_clean(data.get("status") or data.get("sts")),
        )
    except Exception:
        logger.exception("sandbox_kyc: search_gstin failed (fail-closed → vendor_down)")
        return GstinLookup(ok=False)


def _lookup(req: RequestFn, key: str, token: str, gstin: str) -> dict[str, Any]:
    return req(
        "POST", _SEARCH_PATH,
        {"x-api-key": key, "authorization": token, "x-api-version": _API_VERSION},
        {"gstin": gstin},
    )


def _is_401(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) == 401


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
