"""VT-73 — agent context isolation (the 3rd independent privacy layer).

Three structural guards make multi-tenant context contamination impossible,
each sufficient on its own (Pillar 3):

  * PRE-FLIGHT (`validate_context_isolation`) — the load-bearing layer. Called in
    the live dispatch path (supervisor `_sales_recovery_node`, before the agent
    SDK invoke). Independently RE-QUERIES every per-tenant entity id in the bundle
    (campaign / owner-input ids — VT-312: the ledger summary no longer carries
    customer ids) against its tenant-scoped table under `context.tenant_id`'s RLS.
    An id that doesn't resolve = a cross-tenant leak →
    record + raise `ContextIsolationViolation`. This is genuine defense-in-depth:
    it re-checks the builders' output rather than trusting layer-1 RLS.
  * IN-FLIGHT — the `@tool_step` decorator (observability/decorators.py) asserts a
    tool's tenant arg matches the ambient `_observability_context.tenant_id`.
  * POST-FLIGHT (`audit_run_isolation`) — after dispatch, a SERVICE-ROLE scan of
    `pipeline_steps` for the run_id asserts every row's tenant_id matches (catches
    a leak that escaped pre/in-flight, e.g. an unguarded write path).

L3 priors + L4 skills are EXEMPT — sanctioned cross-tenant aggregates with no
tenant entity ids (VT-74/68). A violation is recorded as a `tenant_isolation_breach`
pipeline_steps row (DIRECT insert — `emit_pipeline_step` is a log-only stub until
VT-122) so the existing VT-79 Detector-1 sweep (alerts/triggers) fires a critical
alert. CL-390: only entity UUIDs + counts are recorded, never raw PII.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator.db import tenant_connection
from orchestrator.graph import get_pool

if TYPE_CHECKING:
    from orchestrator.context_builder import SalesRecoveryContext

logger = logging.getLogger(__name__)


class ContextIsolationViolation(RuntimeError):
    """A bundle (or a run's steps) carried data for a tenant other than the
    dispatch's. Critical — kills the dispatch + fires a Detector-1 alert."""


def _record_breach(conn: Any, run_id: UUID, tenant_id: UUID, layer: str, detail: dict[str, Any]) -> None:
    """DIRECT pipeline_steps insert (step_kind='tenant_isolation_breach') so the
    VT-79 Detector-1 sweep picks it up. NOT emit_pipeline_step (log-only stub,
    VT-122). PII-free payload — entity UUIDs + counts only."""
    raw = conn.execute(
        "SELECT COALESCE(MAX(step_seq), 0) + 1 AS next FROM pipeline_steps WHERE run_id = %s",
        (str(run_id),),
    ).fetchone()
    next_seq = int((raw["next"] if isinstance(raw, dict) else raw[0]))
    conn.execute(
        """
        INSERT INTO pipeline_steps
            (run_id, tenant_id, step_seq, step_kind, step_name, output_envelope, status)
        VALUES (%s, %s, %s, 'tenant_isolation_breach', %s, %s, 'failed')
        """,
        (str(run_id), str(tenant_id), next_seq,
         f"context_isolation_{layer}", Jsonb(detail)),
    )


def _missing_ids(conn: Any, table: str, tenant_id: UUID, ids: set[str]) -> set[str]:
    """Ids from ``ids`` that do NOT resolve to a row under ``tenant_id`` in
    ``table`` (RLS-scoped re-query + explicit tenant filter). A non-empty result
    means those ids belong to another tenant (or don't exist) — a leak."""
    if not ids:
        return set()
    rows = conn.execute(
        f"SELECT id FROM {table} WHERE tenant_id = %s AND id = ANY(%s)",  # noqa: S608 — table is a fixed literal
        (str(tenant_id), list(ids)),
    ).fetchall()
    found = {str((r["id"] if isinstance(r, dict) else r[0])) for r in rows}
    return ids - found


def validate_context_isolation(context: SalesRecoveryContext) -> None:
    """PRE-FLIGHT: assert every per-tenant entity id in ``context`` belongs to
    ``context.tenant_id``. Raises ``ContextIsolationViolation`` (after recording a
    Detector-1 breach) on any cross-tenant id. L3/L4 are exempt."""
    tid = context.tenant_id
    # VT-312 brain-decides: the customer_ledger_summary no longer carries any
    # per-customer entity ids (it is raw percentile DISTRIBUTIONS + business_type
    # — see LedgerSummary). The ledger plane therefore cannot leak a customer id,
    # so there is no customers re-query here. The remaining per-tenant entity-id
    # bearing sections (recent_campaigns / pending_owner_inputs) are re-checked.
    camp_ids = {str(c.campaign_id) for c in (context.recent_campaigns or [])}
    input_ids = {str(oi.input_id) for oi in (context.pending_owner_inputs or [])}

    with tenant_connection(tid) as conn:
        leaks = {
            "campaigns": _missing_ids(conn, "campaigns", tid, camp_ids),
            "owner_inputs": _missing_ids(conn, "owner_inputs", tid, input_ids),
        }
        offenders = {k: sorted(v) for k, v in leaks.items() if v}
        if offenders:
            _record_breach(conn, context.run_id, tid, "preflight", {
                "layer": "pre_flight",
                "offending_ids": offenders,
                "counts": {k: len(v) for k, v in offenders.items()},
            })
    if offenders:
        logger.critical(
            "VT-73 context isolation breach (pre-flight) tenant=%s run=%s offenders=%s",
            tid, context.run_id, offenders,
        )
        raise ContextIsolationViolation(
            f"SalesRecoveryContext for tenant {tid} carries cross-tenant ids: {offenders}"
        )


def audit_run_isolation(run_id: UUID, expected_tenant_id: UUID) -> None:
    """POST-FLIGHT: SERVICE-ROLE scan of all pipeline_steps for ``run_id``; assert
    every row's tenant_id == ``expected_tenant_id``. An anomaly (a step logged
    under another tenant for this run) → record a breach. Does NOT raise — the
    dispatch already completed; this is detect-and-alert. Best-effort."""
    try:
        with get_pool().connection() as conn:  # cross-tenant scan — service role
            rows = conn.execute(
                "SELECT DISTINCT tenant_id FROM pipeline_steps WHERE run_id = %s",
                (str(run_id),),
            ).fetchall()
            tenants = {str((r["tenant_id"] if isinstance(r, dict) else r[0])) for r in rows}
            stray = tenants - {str(expected_tenant_id)}
            if stray:
                _record_breach(conn, run_id, expected_tenant_id, "postflight", {
                    "layer": "post_flight",
                    "expected_tenant": str(expected_tenant_id),
                    "stray_tenants": sorted(stray),
                })
                logger.critical(
                    "VT-73 context isolation breach (post-flight) run=%s expected=%s stray=%s",
                    run_id, expected_tenant_id, stray,
                )
    except Exception:  # noqa: BLE001 — post-flight audit is best-effort detect-and-alert
        logger.exception("VT-73 post-flight isolation audit failed (run=%s)", run_id)


__all__ = ["ContextIsolationViolation", "audit_run_isolation", "validate_context_isolation"]
