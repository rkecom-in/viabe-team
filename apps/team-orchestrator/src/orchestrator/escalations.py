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
) -> bool:
    """Record an escalation for the Ops queue. Idempotent on run_id (one escalation per run)
    when run_id is provided. Returns True if a row was INSERTED, False if it conflicted (an
    idempotent hit) — the caller gates the Fazal alert on this so a DBOS workflow replay does
    NOT re-fire the Telegram ping (VT-343 nit A)."""
    if severity not in _VALID_SEVERITY:
        raise ValueError(f"invalid severity {severity!r}; valid: {_VALID_SEVERITY}")
    # VT-279: deterministically route the escalation — knowledge-gap → 'vtr', authority/identity →
    # 'owner' (Pillar 7). NO LLM (Pillar 1); fail-safe to 'owner'. VT-280's digest reads `route`.
    from orchestrator.owner_surface.vtr_classifier import classify_escalation_route

    route, _route_reason = classify_escalation_route(notes, kind=kind)
    with get_pool().connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO escalations (tenant_id, run_id, kind, severity, notes, route)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) WHERE run_id IS NOT NULL DO NOTHING
            """,
            (str(tenant_id), str(run_id) if run_id else None, kind, severity, notes, route),
        )
        inserted = (cur.rowcount or 0) > 0
    logger.info(
        "escalation recorded tenant=%s kind=%s severity=%s route=%s inserted=%s",
        tenant_id, kind, severity, route, inserted,
    )
    return inserted


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


# VT-357 part 2 — SLA windows (IST): an escalation OPENED during business hours (10am–7pm) breaches
# at +4h; otherwise +24h. The breach fires a SECOND Fazal alert; sla_alerted_at gates re-alerting.
_SLA_BUSINESS_START_HOUR = 10
_SLA_BUSINESS_END_HOUR = 19  # exclusive — [10am, 7pm) IST


def run_sla_breach_sweep_body() -> list[str]:
    """VT-357 part 2: alert Fazal a SECOND time on every OPEN escalation past its SLA (4h if opened
    in business hours 10am–7pm IST, else 24h). `sla_alerted_at` makes this fire ONCE per breach
    (the hourly sweep won't re-ping). Best-effort per row. Returns the breached escalation ids.

    NO LLM (Pillar 1). Service-role (escalations is deny-all RLS)."""
    from orchestrator.alerts.clients import alert_fazal as _alert_fazal

    breached: list[str] = []
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT id, tenant_id FROM escalations
            WHERE status = 'open' AND sla_alerted_at IS NULL
              AND now() > opened_at + (
                CASE
                  WHEN EXTRACT(hour FROM opened_at AT TIME ZONE 'Asia/Kolkata') >= %s
                   AND EXTRACT(hour FROM opened_at AT TIME ZONE 'Asia/Kolkata') < %s
                  THEN interval '4 hours'
                  ELSE interval '24 hours'
                END)
            """,
            (_SLA_BUSINESS_START_HOUR, _SLA_BUSINESS_END_HOUR),
        ).fetchall()
        for row in rows:
            r = dict(row)
            eid, tid = str(r["id"]), str(r["tenant_id"])
            try:
                _alert_fazal(
                    f"⚠️ SLA BREACH (VT-357) — escalation {eid} (tenant={tid}) is unresolved past "
                    f"its SLA. Open it in the Ops Console."
                )
            except Exception:
                logger.exception("VT-357 SLA alert failed escalation=%s", eid)
            # Mark regardless (a failed Telegram ping must not loop the alert every hour).
            conn.execute("UPDATE escalations SET sla_alerted_at = now() WHERE id = %s", (eid,))
            breached.append(eid)
    logger.info("VT-357 SLA sweep: %d breach alert(s)", len(breached))
    return breached


__all__ = [
    "record_escalation",
    "backfill_from_pipeline_runs",
    "record_ops_audit",
    "run_sla_breach_sweep_body",
]
