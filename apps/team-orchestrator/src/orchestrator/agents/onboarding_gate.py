"""VT-421 — the fail-closed Sales-Recovery ONBOARDED gate.

Fazal HALT (2026-06-25): SR execution (detect → approve → win-back SEND) runs ONLY for a
FULLY-ONBOARDED tenant. No out-of-track communication. Today NOTHING in the SR stack reads
``tenants.phase`` / ``verification_status`` / connector / customer-count — RLS scopes the
tenant but does not gate onboarding. THIS module is that gate.

``tenant_is_sr_eligible(tenant_id, conn)`` is the single eligibility predicate, called from
TWO fail-closed sites (defense-in-depth, neither alone is trusted):

  - Call site A (DETECT side, an optimization + clean no-op):
    ``sales_recovery_executor.execute_item`` entry — a non-eligible tenant returns a
    ``skipped_not_onboarded`` ItemExecutionResult before detection runs.
  - Call site B (SEND side, THE load-bearing safety boundary, Gate 0):
    ``customer_send.agent_send_draft`` — Gate 0 sits at the TOP of the gate stack and covers
    BOTH L2 (l2_send) and L3 (l3_hold), which converge on this single choke point. A
    non-eligible tenant's draft is SKIPPED (``SKIP_NOT_ONBOARDED``) before any Twilio call.

FAIL-CLOSED EVERYWHERE: missing tenant row / NULL / unknown phase / read error → False. One
try/except wraps the whole body and returns False on ANY exception — the precedent is
``transitions._activation_verification_ok`` and ``sales_recovery_executor._owner_inputs_ok``.

Reuses existing signals only — NO new migration:
  - ``tenants.phase``                  (mig 001 / 121 — the lifecycle phase)
  - ``tenants.verification_status``    (mig 120 — the VT-361 activation tier)
  - ``tenant_connector_status``        (mig 034 — connected + has-pulled-data signal)
  - ``customers`` count                (the ingested-customer signal)

CL-390: this module logs IDs + a boolean reason ONLY — never a display name, phone, or fact.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# --- ELIGIBLE_PHASES — the single load-bearing knob ---------------------------------------
#
# The whole gate keys on this ONE named constant: flipping the "fully onboarded" definition is a
# one-line change here, with the helper + both call sites unaffected (mirrors
# transitions._ACTIVATION_VERIFIED_TIERS).
#
# Fazal decision pending (Cowork relaying): paid-active-only {paid_active,paid_at_risk} vs
# journey-complete (admits trial)
#
# Default = the CONSERVATIVE paid-only set. Rationale: paid_active is "fully onboarded + verified
# + actively paid"; paid_at_risk is a still-active paid tenant in an engagement dip — SR is exactly
# the win-back tool for it, so excluding it would be wrong. trial is EXCLUDED: a trial tenant can
# have completed the guided journey + hold a business_plan + customers WITHOUT being a paying,
# fully-activated customer (journey-complete ≠ paid), and the fail-closed launch posture is to
# admit only the unambiguous "active paying" set.
ELIGIBLE_PHASES: frozenset[str] = frozenset({"paid_active", "paid_at_risk"})

# The verification tiers that count as "≥ gstin_verified" (mirrors transitions._ACTIVATION_VERIFIED_TIERS).
# Re-asserted DIRECTLY (not folded into phase): reaching paid_active already requires this, but a
# hand-mutated tenants.phase must NOT bypass the verification floor — phase does not PROVE current
# verification.
_VERIFIED_TIERS: frozenset[str] = frozenset({"gstin_verified", "vtr_verified"})


def _col(row: Any, key: str, idx: int) -> Any:
    """Read a column from a psycopg row that may be a dict or a tuple."""
    return row[key] if isinstance(row, dict) else row[idx]


def tenant_is_sr_eligible(tenant_id: UUID | str, *, conn: Any) -> bool:
    """True IFF the tenant is FULLY ONBOARDED for Sales-Recovery execution. Fail-closed.

    ``conn`` is the caller's RLS-scoped ``tenant_connection`` (already bound to this tenant). The
    read runs on that connection, so RLS independently confirms the row belongs to the tenant.

    Returns True only when ALL of:
      1. ``tenants.phase ∈ ELIGIBLE_PHASES``.
      2. ``tenants.verification_status ∈ {gstin_verified, vtr_verified}`` — re-asserted DIRECTLY,
         NOT collapsed into phase (a hand-mutated phase cannot bypass the verification floor).
      3. ≥1 ``tenant_connector_status`` row that is ``enabled = TRUE`` AND has actually pulled data
         (``last_status = 'ok'`` OR ``last_ingested_date IS NOT NULL``).
      4. ≥1 ingested customer (``count(customers) >= 1``).

    Returns False on ANY unknown / NULL / missing row / read error (single try/except → False).
    """
    tid = str(tenant_id)
    try:
        # tenants row — phase + verification on ONE read.
        trow = conn.execute(
            "SELECT phase, verification_status FROM tenants WHERE id = %s",
            (tid,),
        ).fetchone()
        if trow is None:
            logger.info("sr_onboarded_gate: no tenant row tenant=%s -> ineligible", tid)
            return False
        phase = _col(trow, "phase", 0)
        verification_status = _col(trow, "verification_status", 1)

        if phase not in ELIGIBLE_PHASES:
            logger.info(
                "sr_onboarded_gate: tenant=%s phase=%s not in ELIGIBLE_PHASES -> ineligible",
                tid, phase,
            )
            return False
        # DIRECT re-assertion (NOT folded into phase): a hand-mutated phase must not bypass this.
        if verification_status not in _VERIFIED_TIERS:
            logger.info(
                "sr_onboarded_gate: tenant=%s verification_status=%s below gstin_verified -> ineligible",
                tid, verification_status,
            )
            return False

        # Connected data source that actually pulled data at least once.
        crow = conn.execute(
            "SELECT 1 FROM tenant_connector_status "
            "WHERE tenant_id = %s AND enabled = TRUE "
            "  AND (last_status = 'ok' OR last_ingested_date IS NOT NULL) "
            "LIMIT 1",
            (tid,),
        ).fetchone()
        if crow is None:
            logger.info(
                "sr_onboarded_gate: tenant=%s no enabled+pulled connector -> ineligible", tid
            )
            return False

        # ≥1 ingested customer — via the wrapper (the no-direct-tenant-db-access lint owns
        # per-tenant customers SQL); pass the held RLS conn so it reads on the same connection.
        from orchestrator.db.wrappers import CustomersWrapper

        if CustomersWrapper().count_all(tid, conn=conn) < 1:
            logger.info("sr_onboarded_gate: tenant=%s 0 customers -> ineligible", tid)
            return False

        return True
    except Exception:  # noqa: BLE001 — fail-closed on ANY read/DB error (the precedent: transitions)
        logger.warning(
            "sr_onboarded_gate: eligibility read failed tenant=%s -> ineligible (fail-closed)",
            tid,
        )
        return False


__all__ = ["ELIGIBLE_PHASES", "tenant_is_sr_eligible"]
