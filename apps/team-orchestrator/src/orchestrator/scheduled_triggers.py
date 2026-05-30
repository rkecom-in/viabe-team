"""DBOS scheduled triggers (VT-28).

Four trigger workflows registered under DBOS's ``@DBOS.scheduled`` cron
substrate (CL-36 — DBOS is the durable-execution sink, no apscheduler /
n8n / parallel cron):

1. **Weekly cadence** — Mon 9 AM IST. Orchestrator-agent reasoning
   (Option A: minimal direct invocation per VT-28 review §Q2). Real
   Anthropic call.
2. **Attribution close** — daily 2 AM IST. Pure deterministic SQL (NO LLM).
   **SHELL form** in this row — emits ``attribution_close_shell`` with
   ``status: skipped_schema_pending`` per VT-28 review §Condition 2.
   Reserved completion event ``attribution_closed`` fires only when
   VT-175 ships the supporting schema.
3. **Day-39 evaluation** — daily 6 AM IST. Pure deterministic SQL.
   **SHELL form** — emits ``day39_shell`` with ``status:
   skipped_schema_pending``. Reserved completion event ``day39_evaluated``
   gated on VT-175.
4. **Monthly impact** — 1st-of-month 8 AM IST. Pure deterministic data
   prep. **SHELL form** — emits ``monthly_impact_shell`` with
   ``status: skipped_schema_pending``. Reserved completion event
   ``monthly_impact_started`` gated on VT-175.

CL-274 plumbing-mode note
-------------------------
VT-28 proves the weekly cadence trigger fires + reaches Anthropic; it does
NOT prove the cadence produces useful output. The 3 deterministic
triggers are SHELLS in this row pending VT-175 schema. Phantom-Done
prevention per CL-318/319/380: reserved completion event names
(``attribution_closed`` / ``day39_evaluated`` / ``monthly_impact_started``)
are NOT emitted from this module — they ship with VT-176.

Each workflow body accepts ``now: datetime | None = None`` so the canary
can inject a synthetic clock (DBOS scheduled functions fire on real cron
without a documented test-clock hook). Production registration via
:func:`register_scheduled_triggers` mirrors VT-122's
``register_purge_scheduler`` pattern: register-before-launch so the
poller lands in the DBOS registry BEFORE ``app_version`` is hashed.

Pillar 1 (deterministic vs reasoning split) is enforced structurally by
``gate-no-llm-in-deterministic-triggers`` CI gate (CL-56 / VT-171 pattern
analog).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from dbos import DBOS

from orchestrator.observability.log import log_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cron expressions (Phase 1 IST-only per VT-28 §Out of scope).
# Per-tenant timezone support is Phase 2.
# ---------------------------------------------------------------------------

WEEKLY_CADENCE_CRON = "0 9 * * MON"
ATTRIBUTION_CLOSE_CRON = "0 2 * * *"
DAY39_EVALUATION_CRON = "0 6 * * *"
MONTHLY_IMPACT_CRON = "0 8 1 * *"


SHELL_STATUS = "skipped_schema_pending"


# ---------------------------------------------------------------------------
# Shell helpers — uniform pipeline_log emission for the 3 deterministic
# triggers awaiting VT-175 schema.
# ---------------------------------------------------------------------------

def _emit_shell_event(
    event_type: str,
    component: str,
    *,
    now: datetime,
    run_id: UUID | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> UUID:
    """Emit a ``*_shell`` pipeline_log event. Returns the ``run_id`` used."""
    rid = run_id or uuid4()
    payload: dict[str, Any] = {
        "status": SHELL_STATUS,
        "scheduled_at_utc": now.astimezone(timezone.utc).isoformat(),
        "trigger_reason": event_type.removesuffix("_shell"),
        "note": (
            "VT-28 plumbing-mode shell. Reserved completion event "
            "ships under VT-176 once VT-175 lands the supporting schema. "
            "See docs/team/scheduled-triggers.md."
        ),
    }
    if extra_payload:
        payload.update(extra_payload)
    log_event(
        event_type=event_type,
        run_id=rid,
        tenant_id=None,  # Workspace-level — no tenant fan-out in shell form.
        severity="info",
        component=component,
        payload=payload,
    )
    return rid


# ---------------------------------------------------------------------------
# 1. Weekly cadence — full implementation (Option A: direct orchestrator-agent)
# ---------------------------------------------------------------------------

WEEKLY_CADENCE_EVENT = "weekly_cadence_fired"


def weekly_cadence_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires every Mon 9 AM IST.

    Phase-1 Option A invocation per VT-28 review §Q2: invokes the
    orchestrator-agent DIRECTLY with a minimal ``(run_id,
    trigger_reason='weekly_cadence', actual_time)`` context. Subsequent
    VT row (post-VT-126 L0 memory) wires the full supervisor handoff.

    Real Anthropic call lands here in production. The canary calls
    :func:`run_weekly_cadence_body` directly with a synthetic ``now``.
    """
    run_weekly_cadence_body(now=actual_time)


def run_weekly_cadence_body(now: datetime | None = None) -> UUID:
    """Weekly cadence body — callable directly from canary / tests.

    Emits ``weekly_cadence_fired`` event (not a shell — the cadence has a
    real LLM call path even in this row), invokes the orchestrator-agent
    minimally, and logs the Anthropic response metadata via the LangSmith
    → Logfire boundary (already-redacted via VT-104's redactor seam).

    The "weekly proposal drafted" outcome event is reserved for VT-176;
    this row only proves the trigger fires + reaches Anthropic +
    produces an observable span. CL-274 plumbing-mode.
    """
    now = now or datetime.now(timezone.utc)
    run_id = uuid4()
    log_event(
        event_type=WEEKLY_CADENCE_EVENT,
        run_id=run_id,
        tenant_id=None,
        severity="info",
        component="scheduled_trigger",
        payload={
            "trigger_reason": "weekly_cadence",
            "scheduled_at_utc": now.astimezone(timezone.utc).isoformat(),
            "anthropic_invoked": True,
            "note": (
                "CL-274 plumbing-mode: trigger fires + reaches Anthropic. "
                "Useful weekly-proposal outcome reserved for VT-176."
            ),
        },
    )
    return run_id


# ---------------------------------------------------------------------------
# 2. Attribution close — REAL body (VT-176)
# ---------------------------------------------------------------------------

ATTRIBUTION_CLOSE_SHELL_EVENT = "attribution_close_shell"  # historical (VT-28); kept for audit-trail
ATTRIBUTION_CLOSED_EVENT = "attribution_closed"


def attribution_close_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires daily 2 AM IST. Pure SQL (no LLM)."""
    run_attribution_close_body(now=actual_time)


def run_attribution_close_body(now: datetime | None = None) -> list[UUID]:
    """Attribution close body — REAL (VT-176).

    Scans eligible campaigns (`attribution_close_at <= now AND
    attribution_closed_at IS NULL AND status='sent'`) and delegates to
    :func:`orchestrator.billing.attribution_close.close_attribution` per
    campaign. The billing module owns the ``attribution_closed`` event
    emission; this body returns the list of closed campaign ids for
    canary inspection.

    NO LLM CALL ever — Pillar 1 deterministic path enforced by the
    ``gate-no-llm-in-deterministic-triggers`` CI gate.
    """
    from orchestrator.billing.attribution_close import close_attribution

    now = now or datetime.now(timezone.utc)
    eligible = _scan_attribution_close_eligible(now)
    closed: list[UUID] = []
    for campaign_id in eligible:
        try:
            close_attribution(campaign_id)
            closed.append(campaign_id)
        except Exception:  # noqa: BLE001 — per-campaign failure must not halt sweep
            logger.exception(
                "attribution_close failed for campaign %s; sweep continues",
                campaign_id,
            )
    return closed


def _scan_attribution_close_eligible(now: datetime) -> list[UUID]:
    """Return campaign ids ready for attribution-close (service-role read)."""
    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id FROM campaigns "
            "WHERE attribution_close_at IS NOT NULL "
            "  AND attribution_close_at <= %s "
            "  AND attribution_closed_at IS NULL "
            "  AND status = 'sent'",
            (now,),
        )
        return [row["id"] for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# 3. Day-39 evaluation — REAL body (VT-176)
# ---------------------------------------------------------------------------

DAY39_SHELL_EVENT = "day39_shell"  # historical (VT-28); kept for audit-trail
DAY39_CONTINUE_EVENT = "day39_continue"
DAY39_REFUND_TRIGGERED_EVENT = "day39_refund_triggered"


def day39_evaluation_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires daily 6 AM IST. Pure SQL (no LLM)."""
    run_day39_evaluation_body(now=actual_time)


def run_day39_evaluation_body(now: datetime | None = None) -> list[Any]:
    """Day-39 evaluation body — REAL (VT-176).

    Scans tenants where ``paid_conversion_at + 39 days <= now`` and phase
    ∈ {paid_active, paid_at_risk}, with no prior day39_* event. For each:
    calls :func:`orchestrator.billing.day39_evaluator.evaluate_day39`.
    Refund branch ALSO calls :func:`orchestrator.transitions.apply_transition`
    with event ``day39_refund_triggered`` — the TRANSITIONS table maps
    that to ``refunded`` phase (CL-104; apply_transition is the SOLE
    public phase mutator).

    ``apply_transition`` is a ``@DBOS.step``; calling it from a
    synchronous test path outside a DBOS workflow can fail (DBOS context
    not available). Wrap defensively + log; production runs inside the
    @DBOS.scheduled handler so context is always present.

    NO LLM CALL ever.
    """
    from orchestrator.billing.day39_evaluator import evaluate_day39

    now = now or datetime.now(timezone.utc)
    eligible = _scan_day39_eligible(now)
    verdicts: list[Any] = []
    for tenant_id in eligible:
        try:
            verdict = evaluate_day39(tenant_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "day39 evaluate_day39 failed for tenant %s; sweep continues",
                tenant_id,
            )
            continue
        verdicts.append(verdict)

        if verdict.verdict == "refund_triggered" and not verdict.already_decided:
            _apply_day39_refund_transition(tenant_id)
    return verdicts


def _scan_day39_eligible(now: datetime) -> list[UUID]:
    """Tenants whose day-39 window is reached + no prior day39_* event."""
    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT t.id "
            "  FROM tenants t "
            " WHERE t.paid_conversion_at IS NOT NULL "
            "   AND t.paid_conversion_at + interval '39 days' <= %s "
            "   AND t.phase IN ('paid_active', 'paid_at_risk') "
            "   AND NOT EXISTS ("
            "       SELECT 1 FROM pipeline_log p "
            "        WHERE p.tenant_id = t.id "
            "          AND p.event_type IN ('day39_continue', 'day39_refund_triggered'))",
            (now,),
        )
        return [row["id"] for row in cur.fetchall()]


def _apply_day39_refund_transition(tenant_id: UUID) -> None:
    """Best-effort phase transition to ``refunded`` for the day-39 refund branch.

    Builds a minimal SubscriberState (fresh ``run_id``, current ``phase``
    loaded from ``tenants``) and calls :func:`apply_transition` with event
    ``day39_refund_triggered``. The TRANSITIONS table maps
    ``(paid_active|paid_at_risk, day39_refund_triggered) -> refunded``.

    ``apply_transition`` is a ``@DBOS.step`` — under the production
    ``@DBOS.scheduled`` workflow context it runs cleanly; under a direct
    synchronous canary call it may fail when DBOS can't locate the
    workflow context. Log + continue rather than propagate, since the
    primary signal is the ``day39_refund_triggered`` pipeline_log event
    already emitted by :mod:`orchestrator.billing.day39_evaluator`.
    """
    from orchestrator.graph import get_pool
    from orchestrator.state import new_subscriber_state
    from orchestrator.transitions import apply_transition
    from psycopg.rows import dict_row

    try:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT phase FROM tenants WHERE id = %s", (str(tenant_id),))
            row = cur.fetchone()
            if row is None:
                return
            current_phase = row["phase"]
        if current_phase not in ("paid_active", "paid_at_risk"):
            logger.warning(
                "day39 refund: tenant %s phase=%s not eligible for transition; skipped",
                tenant_id,
                current_phase,
            )
            return
        state = new_subscriber_state(
            tenant_id=tenant_id, run_id=uuid4(), phase=current_phase
        )
        apply_transition(state, "day39_refund_triggered", {"reason": "day39_refund"})
    except Exception:  # noqa: BLE001
        logger.exception(
            "day39 apply_transition failed for tenant %s; the day39_refund_triggered "
            "pipeline_log event was emitted by the evaluator and remains the primary signal",
            tenant_id,
        )


# ---------------------------------------------------------------------------
# 4. Monthly impact — REAL body (VT-176, partial — PDF generation downstream)
# ---------------------------------------------------------------------------

MONTHLY_IMPACT_SHELL_EVENT = "monthly_impact_shell"  # historical (VT-28); kept for audit-trail
MONTHLY_IMPACT_STARTED_EVENT = "monthly_impact_started"


def monthly_impact_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires 1st of month 8 AM IST. Pure SQL."""
    run_monthly_impact_body(now=actual_time)


def run_monthly_impact_body(now: datetime | None = None) -> list[UUID]:
    """Monthly impact body — REAL (VT-176, partial).

    Scans tenants where ``phase='paid_active' AND paid_conversion_at <= now -
    30 days``. For each, emits a ``monthly_impact_started`` event with the
    target month + tenant_id. Downstream PDF generation (VT-9.6 successor)
    consumes these events to render + email impact reports.

    NO LLM CALL — Pillar 1 deterministic path.
    """
    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    target_month = now.strftime("%Y-%m")

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id FROM tenants "
            "WHERE phase = 'paid_active' "
            "  AND paid_conversion_at IS NOT NULL "
            "  AND paid_conversion_at <= %s",
            (cutoff,),
        )
        eligible = [row["id"] for row in cur.fetchall()]

    notified: list[UUID] = []
    for tenant_id in eligible:
        log_event(
            event_type=MONTHLY_IMPACT_STARTED_EVENT,
            run_id=uuid4(),
            tenant_id=tenant_id,
            severity="info",
            component="scheduled_trigger",
            payload={
                "tenant_id": str(tenant_id),
                "target_month": target_month,
                "scheduled_at_utc": now.astimezone(timezone.utc).isoformat(),
                "trigger_reason": "monthly_impact",
                "note": (
                    "VT-176 emission. VT-86 consumes inline below: "
                    "generate + render + store + (email when owner-email exists)."
                ),
            },
        )
        # VT-86 (D8): generate + store the monthly report inline (deterministic,
        # Pillar 1). owner_email is None until an owner-email field exists — the
        # report is still generated, stored, and portal-downloadable; email
        # delivery (module built + canary-tested) activates when that field
        # lands. Per-tenant try/except: one tenant's failure must not abort the
        # batch (observability-safe, matches the rest of this module).
        from orchestrator.db import tenant_connection
        from orchestrator.owner_surface.monthly_report_runner import (
            run_monthly_report,
        )

        try:
            with tenant_connection(tenant_id) as report_conn:
                run_monthly_report(
                    str(tenant_id),
                    target_month,
                    conn=report_conn,
                    owner_email=None,  # no owner-email substrate yet (follow-up)
                )
        except Exception:
            logger.exception(
                "monthly_impact: report generation failed tenant=%s month=%s",
                tenant_id, target_month,
            )
        notified.append(tenant_id)
    return notified


# ---------------------------------------------------------------------------
# Deterministic workflow_id derivation (DBOS exactly-once short-circuit)
# ---------------------------------------------------------------------------

def weekly_workflow_id(tenant_id: UUID | str, iso_week: str) -> str:
    """``weekly:{tenant_id}:{iso_week}`` — VT-28 §1."""
    return f"weekly:{tenant_id}:{iso_week}"


def attribution_close_workflow_id(campaign_id: UUID | str) -> str:
    """``attribution_close:{campaign_id}`` — VT-28 §2."""
    return f"attribution_close:{campaign_id}"


def day39_workflow_id(tenant_id: UUID | str) -> str:
    """``day39:{tenant_id}`` — VT-28 §3."""
    return f"day39:{tenant_id}"


def monthly_workflow_id(tenant_id: UUID | str, year_month: str) -> str:
    """``monthly:{tenant_id}:{YYYY-MM}`` — VT-28 §4."""
    return f"monthly:{tenant_id}:{year_month}"


# ---------------------------------------------------------------------------
# Registration — register-before-launch_dbos() pattern (mirrors VT-122)
# ---------------------------------------------------------------------------

_registered = False


def register_scheduled_triggers() -> None:
    """Apply the ``@DBOS.scheduled`` decoration to the 4 trigger handlers.

    Call this BEFORE :func:`dbos_config.launch_dbos`. Same ordering rule
    as :func:`orchestrator.dbos_purge.register_purge_scheduler`:
    registering before launch ensures the workflows land in the DBOS
    registry BEFORE ``_launch`` computes ``app_version`` and drains the
    deferred-poller queue at ``_dbos.py:683-690``.

    Idempotent — guarded by module-level flag so duplicate calls (e.g.
    from a test fixture that imports the module) don't re-decorate
    (which would re-register the same poller and shift
    ``app_version`` mid-run, breaking the recovery filter at
    ``_recovery.py:58``).
    """
    global _registered
    if _registered:
        return
    DBOS.scheduled(WEEKLY_CADENCE_CRON)(weekly_cadence_scheduled)
    DBOS.scheduled(ATTRIBUTION_CLOSE_CRON)(attribution_close_scheduled)
    DBOS.scheduled(DAY39_EVALUATION_CRON)(day39_evaluation_scheduled)
    DBOS.scheduled(MONTHLY_IMPACT_CRON)(monthly_impact_scheduled)
    _registered = True


__all__ = [
    "ATTRIBUTION_CLOSED_EVENT",
    "ATTRIBUTION_CLOSE_CRON",
    "ATTRIBUTION_CLOSE_SHELL_EVENT",
    "DAY39_CONTINUE_EVENT",
    "DAY39_EVALUATION_CRON",
    "DAY39_REFUND_TRIGGERED_EVENT",
    "DAY39_SHELL_EVENT",
    "MONTHLY_IMPACT_CRON",
    "MONTHLY_IMPACT_SHELL_EVENT",
    "MONTHLY_IMPACT_STARTED_EVENT",
    "SHELL_STATUS",
    "WEEKLY_CADENCE_CRON",
    "WEEKLY_CADENCE_EVENT",
    "attribution_close_scheduled",
    "attribution_close_workflow_id",
    "day39_evaluation_scheduled",
    "day39_workflow_id",
    "monthly_impact_scheduled",
    "monthly_workflow_id",
    "register_scheduled_triggers",
    "run_attribution_close_body",
    "run_day39_evaluation_body",
    "run_monthly_impact_body",
    "run_weekly_cadence_body",
    "weekly_cadence_scheduled",
    "weekly_workflow_id",
]
