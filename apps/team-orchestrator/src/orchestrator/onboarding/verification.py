"""VT-361 — business-verification orchestration (Option F).

Composes the Sandbox-by-Quicko vendor calls into the tenant verification tiers, RLS-scoped, fail-
closed, attempt-capped, cost-logged. Three operations:

- ``run_lookup(tenant_id, gstin)`` — GSTIN search → store the authoritative name + the gstin. Status
  STAYS 'unverified' (a public GSTIN is knowledge, not control — the bind proves control).
- ``run_initiate(tenant_id)`` — start a reverse penny-drop → return the UPI handle the owner pays ₹1 to.
- ``run_bind(tenant_id, reference)`` — poll the payer's bank name, match it, set the tier:
    payer matches the GSTIN-lookup name  → gstin_verified  (top: both sides vendor-authoritative, ungameable)
    payer matches the claimed business name (no GSTIN) → name_verified
    no match / vendor down → stays unverified (fail-closed)

Anti-gaming: the gstin tier matches two vendor-authoritative names (lookup name ∧ bank payer name) —
neither owner-crafted. The owner cannot type their way to gstin_verified.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from orchestrator.db import tenant_connection
from orchestrator.integrations.methods import sandbox_kyc

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS_PER_DAY = 5  # wallet economics — no retry storms
_MATCH_THRESHOLD = 0.6

# Dropped before token-matching so "RKECOM SERVICE (OPC) PRIVATE LIMITED" ~ "RKECOM Service".
_NOISE = {
    "private", "limited", "ltd", "pvt", "llp", "opc", "company", "co", "and", "the",
    "enterprises", "enterprise", "trading", "services", "service", "industries", "&",
}


def _tokens(name: str | None) -> set[str]:
    if not name:
        return set()
    words = re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split()
    return {w for w in words if w and w not in _NOISE}


def name_match(a: str | None, b: str | None) -> bool:
    """Token-set overlap (Jaccard) ≥ threshold. Both names are vendor-authoritative at the gstin tier."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / len(ta | tb)
    return overlap >= _MATCH_THRESHOLD


def _attempts_today(conn: Any, tenant_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM kyc_verification_log "
        "WHERE tenant_id = %s AND created_at > now() - interval '1 day'",
        (tenant_id,),
    ).fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


def _log(conn: Any, tenant_id: str, action: str, outcome: str | None, cost_category: str) -> None:
    conn.execute(
        "INSERT INTO kyc_verification_log (tenant_id, action, outcome, cost_category) "
        "VALUES (%s, %s, %s, %s)",
        (tenant_id, action, outcome, cost_category),
    )


def run_lookup(tenant_id: UUID | str, gstin: str, *, search_fn: Any = None) -> dict[str, Any]:
    """GSTIN search → store gstin + authoritative name. Status stays unverified. Fail-closed."""
    tid = str(tenant_id)
    with tenant_connection(tid) as conn:
        if _attempts_today(conn, tid) >= _MAX_ATTEMPTS_PER_DAY:
            return {"ok": False, "reason": "attempt_cap"}
        result = sandbox_kyc.search_gstin(gstin) if search_fn is None else search_fn(gstin)
        if not result.ok or not (result.legal_name or result.trade_name):
            _log(conn, tid, "lookup", "vendor_fail", "gstin_search")
            return {"ok": False, "reason": "lookup_failed"}
        name = result.trade_name or result.legal_name
        conn.execute(
            "UPDATE tenants SET gstin = %s, verified_business_name = %s WHERE id = %s",
            (gstin, name, tid),
        )
        _log(conn, tid, "lookup", "gstin_recorded", "gstin_search")
        return {"ok": True, "gstin": gstin, "name": name, "status": "unverified"}


def run_initiate(tenant_id: UUID | str, *, initiate_fn: Any = None) -> dict[str, Any]:
    """Start a reverse penny-drop. Returns the UPI handle the owner pays ₹1 to. Fail-closed."""
    tid = str(tenant_id)
    with tenant_connection(tid) as conn:
        if _attempts_today(conn, tid) >= _MAX_ATTEMPTS_PER_DAY:
            return {"ok": False, "reason": "attempt_cap"}
        rpd = sandbox_kyc.initiate_reverse_penny_drop() if initiate_fn is None else initiate_fn()
        if not rpd.ok or not rpd.reference:
            _log(conn, tid, "initiate", "vendor_fail", "reverse_penny_drop")
            return {"ok": False, "reason": "initiate_failed"}
        _log(conn, tid, "initiate", "initiated", "reverse_penny_drop")
        return {"ok": True, "reference": rpd.reference, "upi_handle": rpd.upi_handle}


def run_bind(tenant_id: UUID | str, reference: str, *, poll_fn: Any = None) -> dict[str, Any]:
    """Poll the payer's bank name + match → set the tier. Fail-closed (stays unverified). Result-only:
    the payer name is matched then DISCARDED — never stored, never logged."""
    tid = str(tenant_id)
    with tenant_connection(tid) as conn:
        if _attempts_today(conn, tid) >= _MAX_ATTEMPTS_PER_DAY:
            return {"ok": False, "reason": "attempt_cap"}
        rpd = sandbox_kyc.poll_reverse_penny_drop(reference) if poll_fn is None else poll_fn(reference)
        if not rpd.ok or not rpd.payer_name:
            _log(conn, tid, "bind", "pending_or_fail", "reverse_penny_drop")
            return {"ok": False, "reason": "not_paid_or_failed", "status": "unverified"}
        row = conn.execute(
            "SELECT business_name, verified_business_name, gstin FROM tenants WHERE id = %s", (tid,)
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "tenant_not_found", "status": "unverified"}
        r = dict(row) if isinstance(row, dict) else {
            "business_name": row[0], "verified_business_name": row[1], "gstin": row[2]
        }
        lookup_name = r.get("verified_business_name")  # set iff a GSTIN lookup ran
        claimed = r.get("business_name")

        if r.get("gstin") and lookup_name and name_match(rpd.payer_name, lookup_name):
            status, method, authoritative = "gstin_verified", "gstin_reverse_penny_drop", lookup_name
        elif name_match(rpd.payer_name, claimed):
            status, method, authoritative = "name_verified", "reverse_penny_drop", claimed
        else:
            _log(conn, tid, "bind", "no_match", "reverse_penny_drop")
            return {"ok": False, "reason": "name_mismatch", "status": "unverified"}

        conn.execute(
            "UPDATE tenants SET verification_status = %s, verification_method = %s, "
            "verified_business_name = %s, verified_at = %s WHERE id = %s",
            (status, method, authoritative, datetime.now(timezone.utc), tid),
        )
        _log(conn, tid, "bind", status, "reverse_penny_drop")
        return {"ok": True, "status": status, "method": method}
