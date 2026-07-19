"""VT-421 — the fail-closed agent ACTIVATION gate (registry-driven).

Fazal HALT (2026-06-25): agent execution (for SR: detect → approve → win-back SEND) runs ONLY for a
tenant that has crossed that agent's activation bar. No out-of-track communication. Today NOTHING in
the SR stack reads onboarding-completion / verification / connector / customer-count — RLS scopes the
tenant but does not gate activation. THIS module is that gate.

Fazal PIN + EXPAND (2026-06-25):
  - The activation bar is **journey-complete, NOT paid-active.** The 1-month free trial is
    DELIBERATELY UNRESTRICTED — gate on ``onboarding_journey.status='complete'`` (admits BOTH trial
    AND paid). The old ``tenants.phase ∈ {paid_active, paid_at_risk}`` conjunct is REMOVED.
  - The bar is no longer a hardcoded SR condition — it is a DECLARATIVE per-agent prerequisite set in
    ``activation_registry.REGISTRY`` that THIS gate READS. A future agent declares its own prereqs
    there with ZERO change to gate logic.

``is_agent_eligible(tenant_id, agent, *, conn)`` is the single eligibility predicate, called from
TWO fail-closed sites (defense-in-depth, neither alone is trusted):

  - Call site A (DETECT side, an optimization + clean no-op):
    ``sales_recovery_executor.execute_item`` entry — a non-eligible tenant returns a
    ``skipped_not_onboarded`` ItemExecutionResult before detection runs.
  - Call site B (SEND side, THE load-bearing safety boundary, Gate 0):
    ``customer_send.agent_send_draft`` — Gate 0 sits at the TOP of the gate stack and covers BOTH L2
    (l2_send) and L3 (l3_hold), which converge on this single choke point. A non-eligible tenant's
    draft is SKIPPED (``SKIP_NOT_ONBOARDED``) before any Twilio call.

INTROSPECTABLE (Fazal: don't bury it in a boolean — the portal must render WHY an agent is inactive):
``unmet_prerequisites(tenant_id, agent, *, conn)`` returns the UNMET prereqs as human-readable
reasons, queryable per-tenant for the owner-facing surface.

FAIL-CLOSED EVERYWHERE: missing tenant row / NULL / unknown agent / read error → ineligible. One
try/except wraps the eligibility body and returns False on ANY exception — the precedent is
``transitions._activation_verification_ok`` and ``sales_recovery_executor._owner_inputs_ok``.

Reuses existing signals only — NO new migration:
  - ``onboarding_journey.status``      (mig 123 — the VT-367 guided-journey completion signal)
  - ``tenants.verification_status``    (mig 120 — the VT-361 activation tier)
  - ``tenant_connector_status``        (mig 034 — connected + has-pulled-data signal; ANY connector)
  - ``customers`` count                (the ingested-customer signal)

CL-390: this module logs IDs + a boolean reason code ONLY — never a display name, phone, or fact.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from orchestrator.agents.activation_registry import (
    PREREQ_CUSTOMERS,
    PREREQ_DATA_SOURCE,
    PREREQ_JOURNEY_COMPLETE,
    PREREQ_OWNERSHIP_VERIFIED,
    PREREQ_VERIFICATION,
    AgentPrerequisites,
    get_prerequisites,
    prereq_reason,
)

logger = logging.getLogger(__name__)

# The verification tiers that count as "≥ gstin_verified" (mirrors transitions._ACTIVATION_VERIFIED_TIERS).
# Asserted DIRECTLY (not inferred from phase): a hand-mutated tenant must NOT bypass the verification
# floor — phase does not PROVE current verification.
_VERIFIED_TIERS: frozenset[str] = frozenset({"gstin_verified", "vtr_verified"})


def _col(row: Any, key: str, idx: int) -> Any:
    """Read a column from a psycopg row that may be a dict or a tuple."""
    return row[key] if isinstance(row, dict) else row[idx]


def _unmet_codes(tenant_id: UUID | str, prereqs: AgentPrerequisites, *, conn: Any) -> list[str]:
    """Resolve the agent's DECLARED prerequisites against this tenant's live state → the list of
    UNMET prerequisite CODES (empty list = fully eligible). Reads on the caller's RLS-scoped conn.

    The gate owns the SQL + thresholds; the registry owns WHICH facts each agent requires. A prereq
    flag set False in the registry is simply not evaluated for that agent.

    NOT fail-closed on its own — it RAISES on a read error so the boolean ``is_agent_eligible``
    wrapper converts that into ineligible. (A bare ``unmet_prerequisites`` caller gets the same
    fail-closed contract via that public function's own try/except.)
    """
    tid = str(tenant_id)
    unmet: list[str] = []

    # tenants row — verification on one read (phase no longer gates: journey-complete replaced it).
    if prereqs.requires_verification:
        trow = conn.execute(
            "SELECT verification_status FROM tenants WHERE id = %s",
            (tid,),
        ).fetchone()
        if trow is None or _col(trow, "verification_status", 0) not in _VERIFIED_TIERS:
            unmet.append(PREREQ_VERIFICATION)

    # ownership_verified — VT-517 VTR-human ownership review (a UNIVERSAL execution bar). A VTR human
    # confirmed owner→business; until then the agent cannot send/act. Fail-closed: missing row or
    # false → unmet (and a read error raises → is_agent_eligible converts to ineligible).
    if prereqs.requires_ownership_verified:
        orow = conn.execute(
            "SELECT ownership_verified FROM tenants WHERE id = %s",
            (tid,),
        ).fetchone()
        if orow is None or not _col(orow, "ownership_verified", 0):
            unmet.append(PREREQ_OWNERSHIP_VERIFIED)

    # onboarding_journey.status='complete' — the journey-complete bar (admits trial AND paid).
    if prereqs.requires_journey_complete:
        jrow = conn.execute(
            "SELECT 1 FROM onboarding_journey WHERE tenant_id = %s AND status = 'complete' LIMIT 1",
            (tid,),
        ).fetchone()
        if jrow is None:
            unmet.append(PREREQ_JOURNEY_COMPLETE)

    # ≥1 ENABLED customer-data source that actually pulled data (ANY ingest connector — generalized).
    if prereqs.requires_enabled_data_source:
        crow = conn.execute(
            "SELECT 1 FROM tenant_connector_status "
            "WHERE tenant_id = %s AND enabled = TRUE "
            "  AND (last_status = 'ok' OR last_ingested_date IS NOT NULL) "
            "LIMIT 1",
            (tid,),
        ).fetchone()
        if crow is None:
            unmet.append(PREREQ_DATA_SOURCE)

    # ≥ min_customers ingested customers — via the wrapper (the no-direct-tenant-db-access lint owns
    # per-tenant customers SQL); pass the held RLS conn so it reads on the same connection.
    if prereqs.min_customers > 0:
        from orchestrator.db.wrappers import CustomersWrapper

        if CustomersWrapper().count_all(tid, conn=conn) < prereqs.min_customers:
            unmet.append(PREREQ_CUSTOMERS)

    return unmet


def is_agent_eligible(tenant_id: UUID | str, agent: str, *, conn: Any) -> bool:
    """True IFF the tenant has crossed ``agent``'s activation bar. Fail-closed.

    ``conn`` is the caller's RLS-scoped ``tenant_connection`` (already bound to this tenant). The read
    runs on that connection, so RLS independently confirms the row belongs to the tenant.

    Eligible IFF every prerequisite the agent DECLARES in ``activation_registry.REGISTRY`` is
    satisfied for this tenant. For ``sales_recovery`` that is:
      1. ``onboarding_journey.status='complete'`` (admits BOTH trial AND paid — journey-complete,
         NOT paid-active; the free trial is deliberately unrestricted).
      2. ``tenants.verification_status ∈ {gstin_verified, vtr_verified}`` — asserted DIRECTLY.
      3. ≥1 ``tenant_connector_status`` row that is ``enabled = TRUE`` AND has pulled data
         (``last_status = 'ok'`` OR ``last_ingested_date IS NOT NULL``) — ANY ingest connector.
      4. ≥1 ingested customer (``count(customers) >= min_customers``).

    Returns False on an UNKNOWN agent / NULL / missing row / read error (single try/except → False).
    """
    tid = str(tenant_id)
    try:
        prereqs = get_prerequisites(agent)  # KeyError on unknown agent → caught → fail-closed.
        unmet = _unmet_codes(tid, prereqs, conn=conn)
        if unmet:
            logger.info(
                "agent_activation_gate: tenant=%s agent=%s ineligible unmet=%s",
                tid, agent, unmet,
            )
            return False
        return True
    except Exception:  # noqa: BLE001 — fail-closed on ANY read/DB/unknown-agent error (precedent: transitions)
        logger.warning(
            "agent_activation_gate: eligibility read failed tenant=%s agent=%s -> ineligible (fail-closed)",
            tid, agent,
        )
        return False


def unmet_prerequisites(tenant_id: UUID | str, agent: str, *, conn: Any) -> list[str]:
    """The UNMET activation prerequisites for ``agent`` on this tenant, as human-readable reasons —
    the owner-facing portal introspection surface (Fazal: render WHY an agent is inactive, not a
    bare boolean).

    Returns ``[]`` when the agent is fully eligible. Fail-closed: an unknown agent / read error
    returns a single sentinel reason so the portal renders "inactive" rather than (wrongly) "active".
    The codes behind these reasons are stable (``activation_registry.PREREQ_*``); the strings are the
    display layer.
    """
    tid = str(tenant_id)
    try:
        prereqs = get_prerequisites(agent)
        codes = _unmet_codes(tid, prereqs, conn=conn)
        return [prereq_reason(c) for c in codes]
    except Exception:  # noqa: BLE001 — fail-closed: surface inactive-with-reason rather than active.
        logger.warning(
            "agent_activation_gate: unmet_prerequisites read failed tenant=%s agent=%s (fail-closed)",
            tid, agent,
        )
        return [prereq_reason("activation_check_failed")]


def tenant_is_sr_eligible(tenant_id: UUID | str, *, conn: Any) -> bool:
    """Backward-compat alias for the Sales-Recovery activation predicate.

    Thin wrapper over ``is_agent_eligible(tenant_id, 'sales_recovery', conn=conn)``. Retained so the
    DETECT-side call (``sales_recovery_executor``) and any other ``sales_recovery`` caller keep a
    stable name; the load-bearing SEND-side Gate 0 calls ``is_agent_eligible`` directly.
    """
    return is_agent_eligible(tenant_id, "sales_recovery", conn=conn)


__all__ = [
    "is_agent_eligible",
    "tenant_is_sr_eligible",
    "unmet_prerequisites",
]
