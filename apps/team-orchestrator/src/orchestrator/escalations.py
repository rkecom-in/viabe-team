"""VT-292 — escalation queue writer + ops-audit (Ops Console V2 substrate).

The orchestrator writes an ``escalations`` row when it escalates (explicit + richer than a
pipeline_runs status). ``backfill_from_pipeline_runs`` seeds the queue from existing
pipeline_runs markers so Home isn't empty on first deploy (v1). ``record_ops_audit`` is the
append-only operator-action log (resolve/override/reassign) — distinct from the VT-188
privacy_audit_log (PII reveals).

Both tables are deny-all RLS (service-role only); these helpers use the bare pool
(``get_pool()``, RLS-bypassing) — the orchestrator IS the service path. No PII (CL-390).
"""

from __future__ import annotations

import logging
from uuid import UUID

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)

_VALID_SEVERITY = ("low", "medium", "high")


def record_escalation(
    tenant_id: UUID | str,
    kind: str,
    *,
    severity: str = "medium",
    run_id: UUID | str | None = None,
    notes: str | None = None,
) -> None:
    """Record an escalation for the Ops queue. Idempotent on run_id (one escalation per
    run) when run_id is provided."""
    if severity not in _VALID_SEVERITY:
        raise ValueError(f"invalid severity {severity!r}; valid: {_VALID_SEVERITY}")
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO escalations (tenant_id, run_id, kind, severity, notes)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (run_id) WHERE run_id IS NOT NULL DO NOTHING
            """,
            (str(tenant_id), str(run_id) if run_id else None, kind, severity, notes),
        )
    logger.info("escalation recorded tenant=%s kind=%s severity=%s", tenant_id, kind, severity)


# map a pipeline_runs status → (kind, severity) for the v1 backfill.
_BACKFILL_MAP = {
    "aborted_hard_limit": ("hard_limit", "high"),
    "escalated": ("agent_escalated", "medium"),
}


def backfill_from_pipeline_runs(*, since_hours: int = 24) -> int:
    """Seed `escalations` from recent pipeline_runs markers (idempotent on run_id).
    Returns rows inserted. v1 only — once the orchestrator writes escalations inline at the
    escalate site, this is just initial seeding."""
    inserted = 0
    with get_pool().connection() as conn, conn.cursor() as cur:
        for status, (kind, severity) in _BACKFILL_MAP.items():
            cur.execute(
                """
                INSERT INTO escalations (tenant_id, run_id, kind, severity)
                SELECT tenant_id, id, %s, %s FROM pipeline_runs
                WHERE status = %s AND started_at > now() - make_interval(hours => %s)
                ON CONFLICT (run_id) WHERE run_id IS NOT NULL DO NOTHING
                """,
                (kind, severity, status, since_hours),
            )
            inserted += cur.rowcount
    logger.info("escalations backfill inserted=%d", inserted)
    return inserted


def record_ops_audit(
    operator_id: UUID | str,
    action: str,
    target_kind: str,
    *,
    tenant_id: UUID | str | None = None,
    target_id: str | None = None,
    detail: str | None = None,
) -> None:
    """Append an operator-action audit row (resolve/override/reassign/...). Append-only;
    no PII in `detail` (CL-390). VT-294 consumes this for decision-quality measurement."""
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO ops_audit (operator_id, tenant_id, action, target_kind, target_id, detail)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (str(operator_id), str(tenant_id) if tenant_id else None, action, target_kind, target_id, detail),
        )


__all__ = ["record_escalation", "backfill_from_pipeline_runs", "record_ops_audit"]
