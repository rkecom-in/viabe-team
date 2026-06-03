"""VT-76 — opt-out 7-day reconstitution sweep (the closing moat row).

Reconstitution is the opt-out RIGHT: 7 days after a customer opts out, the agent
de-LINKS that customer from its L2 episodic footprint
(``episodic_events.referenced_entity_id`` → the all-zeros sentinel) while KEEPING
the event row. Two reasons (Cowork ruling 20260604T033000Z):

1. De-linking stops re-identification (the opt-out right) — distinct from CL-416
   DSR-purge, which is full DELETION (a different right).
2. Keeping the row preserves k-anon aggregate integrity: the cohort counts L3
   builds on stay intact. Deleting would corrupt them.

The mechanism (this row): a daily ``@DBOS.scheduled`` sweep (registered alongside
the other scheduled triggers per CL-240 — EXTEND the surface, no parallel poller)
that finds opted-out customers whose 7-day clock has elapsed and anonymizes their
episodic footprint, plus an 8-day SLA-breach detector that fires
``reconstitution_sla_breach`` (critical) on the existing VT-202 alerts path.

Gate-live posture: the sweep is correct + canaried on SYNTHETIC
customer-referencing episodic rows, but a no-op on real data until VT-312's
detectors emit customer-referencing events (``referenced_entity_type='customer'``
— VT-312 Blocked on Fazal thresholds). The receive side (inbound STOP classifier
that SETS opt_out_at) is VT-318, gate-live on the customer-inbound path (WABA).

Connection model: the workspace-wide eligibility + SLA scans read
``customers`` cross-tenant via the service-role pool (``get_pool`` — the sanctioned
cross-tenant path, same posture as the day-39 / approval-timeout sweeps; reads
ids + opt_out_at only, never PII — CL-390). Each per-customer anonymization writes
through ``tenant_connection`` so the episodic + customers UPDATEs run under RLS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# The anonymization target: episodic_events.referenced_entity_id is rewritten to
# this all-zeros UUID, severing the customer link while keeping the event row.
RECONSTITUTION_SENTINEL = UUID("00000000-0000-0000-0000-000000000000")

# Reconstitute 7 days after opt-out; breach the SLA if still pending after 8.
RECONSTITUTION_WINDOW_DAYS = 7
RECONSTITUTION_SLA_DAYS = 8

# Cron written in UTC (no container TZ is set, so DBOS fires on UTC) to land at
# 04:00 IST — matches the alerts/scheduler.py convention (`30 3 = 09:00 IST`).
# NOTE: the sibling crons in scheduled_triggers.py write the IST hour literally
# (e.g. `0 3 # 3 AM IST`) — a separate latent TZ discrepancy flagged to Cowork;
# not repaired here. The exact firing minute is immaterial to a daily privacy
# sweep (the 7→8-day SLA window absorbs any sub-day offset).
RECONSTITUTION_CRON = "30 22 * * *"  # 22:30 UTC = 04:00 IST

SLA_BREACH_TRIGGER_KIND = "reconstitution_sla_breach"


@dataclass
class ReconstitutionResult:
    """Sweep outcome — for canary inspection + structured logging."""

    reconstituted: list[UUID] = field(default_factory=list)
    events_anonymized: int = 0
    sla_breaches: list[UUID] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Service-role scans (cross-tenant, ids + opt_out_at only — CL-390)
# ---------------------------------------------------------------------------

def _scan_reconstitution_eligible(now: datetime) -> list[dict[str, Any]]:
    """Opted-out customers whose 7-day clock elapsed and not yet reconstituted.

    Workspace-wide service-role read (the per-customer anonymization below sets
    the tenant GUC). Projects ids only — no display_name / phone / email.
    """
    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id::text AS customer_id, tenant_id::text AS tenant_id
            FROM customers
            WHERE opt_out_status = 'opted_out'
              AND opt_out_at IS NOT NULL
              AND opt_out_at <= %s - make_interval(days => %s)
              AND reconstitution_completed_at IS NULL
            ORDER BY opt_out_at ASC
            """,
            (now, RECONSTITUTION_WINDOW_DAYS),
        )
        return [dict(row) for row in cur.fetchall()]


def _scan_sla_breaches(now: datetime) -> list[dict[str, Any]]:
    """Opted-out customers still un-reconstituted 8+ days after opt-out.

    A breach means the sweep failed to reconstitute within the extra day — a P0
    privacy-SLA signal. Run AFTER the reconstitution pass so a just-completed
    customer never reports as breached.
    """
    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id::text AS customer_id, tenant_id::text AS tenant_id,
                   opt_out_at
            FROM customers
            WHERE opt_out_status = 'opted_out'
              AND opt_out_at IS NOT NULL
              AND opt_out_at < %s - make_interval(days => %s)
              AND reconstitution_completed_at IS NULL
            ORDER BY opt_out_at ASC
            """,
            (now, RECONSTITUTION_SLA_DAYS),
        )
        return [dict(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Per-customer anonymization (RLS write via tenant_connection)
# ---------------------------------------------------------------------------

def reconstitute_customer(
    tenant_id: UUID | str,
    customer_id: UUID | str,
    *,
    now: datetime | None = None,
) -> int:
    """De-link one opted-out customer from its L2 episodic footprint.

    Rewrites every ``episodic_events`` row that references this customer
    (``referenced_entity_type='customer'``) so ``referenced_entity_id`` points at
    the sentinel — the event ROW stays (audit + k-anon integrity). Then stamps
    ``reconstitution_completed_at``. Both writes run under the tenant GUC (RLS),
    so a customer's footprint can only be touched within its own tenant.

    Returns the number of episodic rows anonymized.
    """
    from orchestrator.db import tenant_connection

    now = now or datetime.now(timezone.utc)
    with tenant_connection(tenant_id) as conn:
        cur = conn.execute(
            """
            UPDATE episodic_events
            SET referenced_entity_id = %s
            WHERE referenced_entity_type = 'customer'
              AND referenced_entity_id = %s
            """,
            (str(RECONSTITUTION_SENTINEL), str(customer_id)),
        )
        anonymized = cur.rowcount if cur.rowcount is not None else 0
        conn.execute(
            "UPDATE customers SET reconstitution_completed_at = %s WHERE id = %s",
            (now, str(customer_id)),
        )
    return anonymized


# ---------------------------------------------------------------------------
# SLA-breach trigger (pure — built here, dispatched on the VT-202 path)
# ---------------------------------------------------------------------------

def _build_sla_trigger(row: dict[str, Any], now: datetime) -> Any:
    """Build the ``reconstitution_sla_breach`` Trigger for one overdue customer.

    Pure (no DB). CL-390: ids + day-count only — never display_name / phone.
    """
    from orchestrator.alerts.triggers import Trigger, severity_for

    opt_out_at = row["opt_out_at"]
    days_overdue = (now - opt_out_at).days if isinstance(opt_out_at, datetime) else None
    customer_id = row["customer_id"]
    return Trigger(
        tenant_id=UUID(str(row["tenant_id"])),
        trigger_kind=SLA_BREACH_TRIGGER_KIND,
        severity=severity_for(SLA_BREACH_TRIGGER_KIND),
        message_text=(
            f"Reconstitution SLA breach: customer {customer_id} opted out "
            f"{days_overdue}d ago and is still not reconstituted "
            f"(SLA {RECONSTITUTION_SLA_DAYS}d)."
        ),
        payload={
            "customer_id": customer_id,
            "opt_out_at": opt_out_at.isoformat() if isinstance(opt_out_at, datetime) else None,
            "days_overdue": days_overdue,
            "sla_days": RECONSTITUTION_SLA_DAYS,
        },
    )


# ---------------------------------------------------------------------------
# The sweep body (callable directly with an injected `now` for the canary)
# ---------------------------------------------------------------------------

def run_reconstitution_sweep_body(now: datetime | None = None) -> ReconstitutionResult:
    """Daily reconstitution sweep — REAL (VT-76). NO LLM (Pillar 1 deterministic).

    1. Reconstitute every customer whose 7-day clock elapsed (per-customer
       try/except — one failure must not halt the sweep).
    2. Scan for 8-day SLA breaches (still pending after the pass) and dispatch a
       critical ``reconstitution_sla_breach`` alert per breach via the VT-202 path.

    Callable directly with an injected ``now`` so the canary drives the date math
    without waiting on the cron (mirrors the scheduled_triggers bodies).
    """
    now = now or datetime.now(timezone.utc)
    result = ReconstitutionResult()

    for row in _scan_reconstitution_eligible(now):
        tenant_id = row["tenant_id"]
        customer_id = row["customer_id"]
        try:
            anonymized = reconstitute_customer(tenant_id, customer_id, now=now)
        except Exception:  # noqa: BLE001 — one stuck customer must not halt the sweep
            logger.exception(
                "reconstitution failed for customer %s (tenant %s); sweep continues",
                customer_id, tenant_id,
            )
            continue
        result.reconstituted.append(UUID(customer_id))
        result.events_anonymized += anonymized

    # SLA scan runs AFTER the reconstitution pass so a just-completed customer
    # is not falsely reported (only genuinely-stuck rows remain pending).
    from orchestrator.alerts.dispatch import dispatch_alert

    for row in _scan_sla_breaches(now):
        customer_id = row["customer_id"]
        try:
            dispatch_alert(_build_sla_trigger(row, now))
            result.sla_breaches.append(UUID(customer_id))
        except Exception:  # noqa: BLE001 — alert dispatch must not halt the sweep
            logger.exception(
                "reconstitution SLA-breach dispatch failed for customer %s; sweep continues",
                customer_id,
            )

    if result.reconstituted or result.sla_breaches:
        logger.info(
            "reconstitution sweep: %d customer(s) reconstituted (%d episodic rows), "
            "%d SLA breach(es)",
            len(result.reconstituted), result.events_anonymized, len(result.sla_breaches),
        )
    return result


__all__ = [
    "RECONSTITUTION_CRON",
    "RECONSTITUTION_SENTINEL",
    "RECONSTITUTION_SLA_DAYS",
    "RECONSTITUTION_WINDOW_DAYS",
    "SLA_BREACH_TRIGGER_KIND",
    "ReconstitutionResult",
    "reconstitute_customer",
    "run_reconstitution_sweep_body",
]
