"""VT-361 — business-verification orchestration (two-tier).

RLS-scoped, fail-closed, attempt-capped, cost-logged. Two operations:

- ``run_lookup(tenant_id, gstin)`` — Sandbox GSTIN search. An ACTIVE GSTIN → ``gstin_verified``
  ("yellow") + the authoritative name stored. Lookup success alone earns it (no ownership bind —
  Fazal two-tier ruling 2026-06-08). Distinguishes vendor-down (retryable) from invalid/inactive
  GSTIN (bad input) in the log so ops can tell an outage from a fraud/typo signal.
- ``run_vtr_override(tenant_id, operator_id, basis)`` — manual VTR/ops upgrade to ``vtr_verified``
  ("green"). Audited (who/when/free-text basis). Gates nothing today; value arrives later.

The activation gate (subscribe → paid_active requires gstin_verified) lives in transitions.py —
it reads verification_status server-side, never a client field.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from orchestrator.db import tenant_connection

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS_PER_DAY = 5  # wallet economics — no retry storms


def _attempts_today(conn: Any, tenant_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM kyc_verification_log "
        "WHERE tenant_id = %s AND action = 'lookup' AND created_at > now() - interval '1 day'",
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
    """GSTIN search → gstin_verified on an ACTIVE result. Fail-closed; per-day capped. The outcome
    log separates vendor_down (retry next attempt/day) from invalid_gstin (bad input)."""
    from orchestrator.integrations.methods import sandbox_kyc

    tid = str(tenant_id)
    with tenant_connection(tid) as conn:
        if _attempts_today(conn, tid) >= _MAX_ATTEMPTS_PER_DAY:
            return {"ok": False, "reason": "attempt_cap"}
        result = sandbox_kyc.search_gstin(gstin) if search_fn is None else search_fn(gstin)

        if not result.ok:
            _log(conn, tid, "lookup", "vendor_down", "gstin_search")  # retryable — outage, not fraud
            return {"ok": False, "reason": "vendor_down", "status": "unverified"}
        if not result.is_active() or not result.authoritative_name():
            _log(conn, tid, "lookup", "invalid_gstin", "gstin_search")  # bad input, not an outage
            return {"ok": False, "reason": "invalid_gstin", "status": "unverified"}

        name = result.authoritative_name()
        conn.execute(
            "UPDATE tenants SET verification_status = 'gstin_verified', verified_business_name = %s, "
            "verification_method = 'gstin_lookup', gstin = %s, verified_at = %s WHERE id = %s",
            (name, gstin, datetime.now(timezone.utc), tid),
        )
        _log(conn, tid, "lookup", "gstin_verified", "gstin_search")
        return {"ok": True, "status": "gstin_verified", "gstin": gstin, "name": name}


def run_vtr_override(
    tenant_id: UUID | str, operator_id: str, basis: str
) -> dict[str, Any]:
    """Manual VTR/ops upgrade → vtr_verified ("green"). tenant_id is the server-resolved target row
    id (the endpoint derives it server-side — IDOR rule). The upgrade + the ops-audit row + the log
    are ATOMIC in ONE service-role txn (#420 subagent should-fix: a separate-connection audit could
    commit the upgrade with no audit row — CL-390 atomic-audit standard). ops_audit is deny-all RLS
    (app_role can't write it), so this runs under the service role with an explicit WHERE id
    predicate (the dsr_purge admin-action pattern); the override is operator-JWT gated upstream."""
    from orchestrator.graph import get_pool

    tid = str(tenant_id)
    now = datetime.now(timezone.utc)
    with get_pool().connection() as conn, conn.transaction():
        row = conn.execute("SELECT 1 FROM tenants WHERE id = %s", (tid,)).fetchone()
        if row is None:
            return {"ok": False, "reason": "tenant_not_found"}
        conn.execute(
            "UPDATE tenants SET verification_status = 'vtr_verified', "
            "verification_method = 'vtr_override', verified_at = %s WHERE id = %s",
            (now, tid),
        )
        conn.execute(
            "INSERT INTO ops_audit (operator_id, tenant_id, action, target_kind, target_id, detail) "
            "VALUES (%s, %s, 'vtr_verify', 'tenant', %s, %s)",
            (str(operator_id), tid, tid, (basis[:200] or None)),
        )
        conn.execute(
            "INSERT INTO kyc_verification_log (tenant_id, action, outcome, cost_category) "
            "VALUES (%s, 'vtr_override', 'vtr_verified', 'none')",
            (tid,),
        )
    return {"ok": True, "status": "vtr_verified"}
