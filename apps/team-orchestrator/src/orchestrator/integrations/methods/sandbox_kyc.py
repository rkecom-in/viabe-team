"""VT-361 — Sandbox by Quicko business-verification client (two-tier; lookup only).

ONE vendor call, ORCHESTRATOR-side, fail-closed, result-only:

``search_gstin(gstin)`` — Sandbox public GSTIN search (no taxpayer auth). Returns the authoritative
legal/trade name + status. An ACTIVE result alone earns the ``gstin_verified`` tier (Fazal two-tier
ruling 2026-06-08 — NO ownership bind at launch; the reverse-penny-drop bind was CUT).

Result-only: parse ONLY the name + status, DISCARD the rest of the vendor response — it never reaches
storage or logs. Graceful-degrade: absent creds / network / 4xx-5xx / parse → ok=False (NEVER raise,
NEVER fake-verified). The caller distinguishes vendor-down (ok=False) from GSTIN-not-active (ok=True
but status != active) so ops can tell an outage from bad input.
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


def search_gstin(
    gstin: str,
    *,
    get_fn: Callable[[str, str, str, dict[str, Any]], dict[str, Any]] | None = None,
) -> GstinLookup:
    """Public GSTIN search → authoritative name + status. Fail-closed (ok=False) on any error.
    Result-only — parses only name + status, discards the rest."""
    creds = _creds()
    if creds is None:
        return GstinLookup(ok=False)
    key, secret = creds
    try:
        raw = (get_fn or _default_get)(
            "/gst/compliance/public/gstin/search", key, secret, {"gstin": gstin}
        )
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


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
