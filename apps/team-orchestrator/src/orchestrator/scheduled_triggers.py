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
TRIAL_EVALUATION_CRON = "0 7 * * *"  # VT-90 — daily 7 AM IST trial sweep (off-peak)
MONTHLY_IMPACT_CRON = "0 8 1 * *"
L3_CONSTRUCTION_CRON = "0 3 * * *"  # VT-68 — nightly 3 AM IST L3 rebuild
WAITLIST_RETENTION_PURGE_CRON = "0 4 * * *"  # VT-354 — daily 4 AM IST waitlist 6-month bound
SLA_BREACH_SWEEP_CRON = "0 * * * *"  # VT-357 — hourly: alert Fazal on SLA-breached open escalations


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

ATTRIBUTION_CLOSE_SHELL_EVENT = (
    "attribution_close_shell"  # historical (VT-28); kept for audit-trail
)
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


def trial_evaluation_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires daily 7 AM IST. VT-90 trial sweep. NO LLM."""
    from orchestrator.billing.trial_sweep import run_trial_evaluation_body

    run_trial_evaluation_body(now=actual_time)


def waitlist_retention_purge_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires daily 4 AM IST. VT-354: ENFORCE the waitlist 6-month
    retention bound (un-notified pre-launch PII). NO LLM; idempotent; safe on an empty waitlist."""
    from orchestrator.api.waitlist import run_waitlist_retention_purge

    run_waitlist_retention_purge()


def sla_breach_sweep_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires hourly. VT-357: a 2nd Fazal alert on any OPEN escalation past
    its SLA (4h business-hours IST / 24h otherwise). NO LLM; idempotent (sla_alerted_at marker)."""
    from orchestrator.escalations import run_sla_breach_sweep_body

    run_sla_breach_sweep_body()


def l3_construction_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires daily 3 AM IST (VT-68). Rebuilds all L3
    cross-tenant patterns (idempotent full rebuild). Pure SQL aggregation (no LLM).
    Best-effort: a construction failure must not crash the scheduler."""
    from orchestrator.knowledge.l3_construction import construct_l3_patterns

    try:
        construct_l3_patterns(now=actual_time)
    except Exception:  # noqa: BLE001 — nightly rebuild is best-effort; next run retries
        logger.exception("VT-68 L3 construction scheduled run failed")


def reconstitution_sweep_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires daily 04:00 IST (VT-76). Runs the opt-out
    7-day reconstitution sweep + 8-day SLA-breach detection (the privacy
    mechanism over the VT-66 hook). Pure SQL (no LLM). Best-effort: a sweep
    failure must not crash the scheduler (the next run + the SLA detector
    re-catch any stuck customer)."""
    from orchestrator.privacy.reconstitution import run_reconstitution_sweep_body

    try:
        run_reconstitution_sweep_body(now=actual_time)
    except Exception:  # noqa: BLE001 — daily sweep is best-effort; next run retries
        logger.exception("VT-76 reconstitution scheduled run failed")


# VT-304: nightly audit-chain verify. 20:30 UTC = 02:00 IST (off-peak). Written
# UTC-correct (matches reconstitution + alerts/scheduler); the exact off-peak
# minute is immaterial to a nightly integrity check.
AUDIT_CHAIN_VERIFY_CRON = "30 20 * * *"


def audit_chain_verify_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — nightly 02:00 IST (VT-304). Verifies the VT-80
    ``privacy_audit_log`` tamper-evident hash-chain; on a break, raises a CRITICAL
    workspace alert (tamper/corruption is surfaced, not just logged). Best-effort:
    a verify failure must not crash the scheduler."""
    from orchestrator.observability.audit_verify import run_audit_chain_verify_body

    try:
        result = run_audit_chain_verify_body(now=actual_time)
        if not result.ok:
            _alert_audit_chain_break(result)
    except Exception:  # noqa: BLE001 — nightly verify is best-effort; next run retries
        logger.exception("VT-304 audit-chain verify scheduled run failed")


def _alert_audit_chain_break(result: Any) -> None:
    """CRITICAL workspace alert for a privacy_audit_log chain break (VT-304).

    Routes DIRECT to the OPS channel (Telegram + email), NOT the per-tenant
    ``tenant_alerts`` path: the chain is global (spans NULL-tenant workspace
    rows), so there is no single tenant to attribute and ``tenant_alerts.tenant_id``
    is a NOT-NULL FK. The message carries seq + reason only — privacy_audit_log is
    PII-free (CL-390), so no scrub needed. Best-effort send; the CRITICAL log in
    ``run_audit_chain_verify_body`` is the durable record."""
    import asyncio
    import os

    from orchestrator.alerts.clients import send_resend_email, send_telegram

    text = (
        "[CRITICAL] privacy_audit_log hash-chain BREAK (VT-80/VT-304) — "
        f"tamper/corruption at seq={getattr(result, 'broken_seq', None)}: "
        f"{getattr(result, 'reason', None)}. rows_checked="
        f"{getattr(result, 'rows_checked', None)}."
    )

    async def _send() -> None:
        await send_telegram(
            os.environ.get("TELEGRAM_OPS_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_OPS_CHAT_ID", ""),
            text,
        )
        await send_resend_email(
            os.environ.get("RESEND_API_KEY", ""),
            os.environ.get("RESEND_FROM_EMAIL", ""),
            os.environ.get("RESEND_TO_EMAIL", ""),
            "Viabe CRITICAL: audit-chain break",
            f"<pre>{text}</pre>",
        )

    try:
        asyncio.run(_send())
    except RuntimeError:  # already in an event loop
        asyncio.get_event_loop().create_task(_send())


# VT-305: nightly PII-in-log sweep. 21:30 UTC = 03:00 IST (off-peak, after the
# 02:00 audit-chain verify). UTC-correct cron (matches reconstitution/audit-chain).
PII_LOG_SWEEP_CRON = "30 21 * * *"


def pii_log_sweep_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — nightly 03:00 IST (VT-305). Sweeps the VT-79
    Detector-5 (``detect_pii_in_logs``) across active tenants and dispatches a
    per-tenant CRITICAL ``pii_in_log`` alert for each finding (unredacted PII left
    in pipeline_steps payloads — a CL-390 regression catcher). Per-tenant, so it
    uses the standard ``tenant_alerts`` path (unlike VT-304's workspace alert).
    Best-effort per tenant: one tenant's failure must not halt the sweep."""
    from orchestrator.alerts.dispatch import dispatch_alert
    from orchestrator.alerts.triggers import all_active_tenant_ids, detect_pii_in_logs

    for tenant_id in all_active_tenant_ids():
        try:
            for trigger in detect_pii_in_logs(tenant_id):
                dispatch_alert(trigger)
        except Exception:  # noqa: BLE001 — per-tenant isolation; the sweep continues
            logger.exception(
                "VT-305 PII-in-log sweep failed for tenant %s; sweep continues",
                tenant_id,
            )


# VT-307: nightly KG-events outbox-drain sweep. 21:00 UTC = 02:30 IST (off-peak).
KG_DRAIN_SWEEP_CRON = "0 21 * * *"


def kg_drain_sweep_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — nightly 02:30 IST (VT-307). The reliability
    BACKSTOP for the VT-65 immediate post-commit kg_events drain: re-drains any
    undrained outbox events across active tenants, and if a tenant has stragglers
    the drain could NOT project (``drain_kg_events`` ``failed`` > 0), dispatches a
    per-tenant ``kg_drain_straggler`` warning via the VT-202 path. Best-effort per
    tenant: one tenant's failure must not halt the sweep."""
    from orchestrator.alerts.dispatch import dispatch_alert
    from orchestrator.alerts.triggers import (
        Trigger,
        all_active_tenant_ids,
        severity_for,
    )
    from orchestrator.knowledge.kg_emit import drain_kg_events

    for tenant_id in all_active_tenant_ids():
        try:
            result = drain_kg_events(tenant_id)
            failed = int(result.get("failed", 0))
            if failed > 0:
                dispatch_alert(
                    Trigger(
                        tenant_id=tenant_id,
                        trigger_kind="kg_drain_straggler",
                        severity=severity_for("kg_drain_straggler"),
                        message_text=(
                            f"KG-events drain straggler: {failed} event(s) failed to "
                            f"project for tenant {tenant_id} "
                            f"(drained {result.get('drained', 0)})."
                        ),
                        payload={"failed": failed, "drained": int(result.get("drained", 0))},
                    )
                )
        except Exception:  # noqa: BLE001 — per-tenant isolation; the sweep continues
            logger.exception(
                "VT-307 KG-drain sweep failed for tenant %s; sweep continues",
                tenant_id,
            )


# VT-311: nightly L2 episodic retention soft-delete. 20:00 UTC = 01:30 IST (off-peak).
L2_RETENTION_SWEEP_CRON = "0 20 * * *"


def l2_retention_sweep_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — nightly 01:30 IST (VT-311). Soft-deletes episodic
    rows past the retention window (``TEAM_L2_RETENTION_DAYS``, default ~18 months)
    so the L2 read path stays bounded. Best-effort: a sweep failure must not crash
    the scheduler (the next run re-catches)."""
    from orchestrator.knowledge.l2_retention import run_l2_retention_sweep_body

    try:
        run_l2_retention_sweep_body(now=actual_time)
    except Exception:  # noqa: BLE001 — nightly sweep is best-effort; next run retries
        logger.exception("VT-311 L2 retention sweep scheduled run failed")


def run_day39_evaluation_body(now: datetime | None = None) -> list[Any]:
    """Day-39 evaluation body — REAL (VT-176).

    Scans tenants where ``paid_conversion_at + 39 days <= now`` and phase
    ∈ {paid_active, paid_at_risk}, with no prior day39_* event. For each:
    calls :func:`orchestrator.billing.day39_evaluator.evaluate_day39`.
    Refund branch (VT-85) sends an OFFER via :func:`_send_day39_refund_offer`: it
    parks the tenant in ``refund_offered`` (apply_transition with event
    ``day39_refund_offered``) — NO auto-refund. The owner's REFUND/CONTINUE/DISCUSS
    reply (or the 48h timeout -> CONTINUE) resolves it; the actual refund fires only
    on REFUND (VT-93 execute_refund). (CL-104; apply_transition is the SOLE public
    phase mutator.)

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

        # VT-92: persist the structured decision-audit (skip replays — idempotent).
        if not verdict.already_decided:
            _persist_day39_evaluation(tenant_id, verdict)

        # VT-197: close the learning loop — distill the FRESH verdict into the
        # tenant's agent_reflection L1 entity (calibrates the next context
        # bundle). Skip already-decided (idempotent, mirrors the refund branch).
        if not verdict.already_decided:
            _write_day39_reflection(tenant_id, verdict)

        if verdict.verdict == "refund_triggered" and not verdict.already_decided:
            _send_day39_refund_offer(tenant_id, verdict)
    return verdicts


DAY39_EVALUATOR_VERSION = "1.0.0"  # VT-92 D4: bump = Type-3 governance.


def _persist_day39_evaluation(tenant_id: Any, verdict: Any) -> None:
    """VT-92: persist the structured decision to day39_evaluations (RLS-scoped).
    Best-effort — the authoritative signal is the day39_* pipeline_log event."""
    from orchestrator.db import tenant_connection

    try:
        with tenant_connection(tenant_id) as conn:
            conn.execute(
                "INSERT INTO day39_evaluations "
                "(tenant_id, verdict, arrr_paise, cumulative_fees_paise, evaluator_version) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    str(tenant_id),
                    verdict.verdict,
                    int(verdict.arrr_paise),
                    int(verdict.cumulative_fees_paise),
                    DAY39_EVALUATOR_VERSION,
                ),
            )
    except Exception:  # noqa: BLE001 — audit persist is best-effort under the sweep
        logger.exception("day39: persist evaluation failed tenant=%s; sweep continues", tenant_id)


def _write_day39_reflection(tenant_id: Any, verdict: Any) -> None:
    """VT-197: write the agent-owned 'agent_reflection' L1 entity from a Day-39
    verdict. LLM-FREE — a deterministic distillation (no agent/LLM call; keeps
    the no-LLM-in-deterministic-triggers gate green). NEVER touches the owner-
    curated 'business_profile' entity (Fazal D3 / VT-268). Best-effort: a write
    failure logs + the sweep continues."""
    try:
        from orchestrator.knowledge import upsert_agent_reflection

        recovered = verdict.arrr_paise
        fees = verdict.cumulative_fees_paise
        note = (
            "recovery on track; sustain the current cadence."
            if verdict.verdict == "continue"
            else "recovery under target; favor higher-yield campaigns next cycle."
        )
        upsert_agent_reflection(
            str(tenant_id),
            {
                "source": "day39",
                "verdict": verdict.verdict,
                "arrr_paise": recovered,
                "cumulative_fees_paise": fees,
                "decided_at": verdict.decided_at.isoformat(),
                "summary": (
                    f"Day-39 {verdict.verdict}: attributed recovery {recovered}p "
                    f"vs cumulative fees {fees}p — {note}"
                ),
            },
        )
    except Exception:  # noqa: BLE001 — reflection is best-effort enrichment
        logger.exception(
            "day39 reflection write failed for tenant %s; sweep continues",
            tenant_id,
        )


def _scan_day39_eligible(now: datetime) -> list[UUID]:
    """Tenants whose day-39 window is reached + no prior day39_* event."""
    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        # VT-85 suppression (offer model): the phase filter excludes tenants with an
        # OPEN offer (phase=refund_offered) and EXECUTED refunds (phase=refunded), so
        # a re-offer can't fire mid-window. An EXECUTED refund (refund_executed event)
        # is the terminal marker (belt-and-suspenders if a phase ever reverts). A
        # CONTINUE (reply or 48h timeout) emits day39_continue and suppresses for 90
        # days, then the tenant is re-eligible (re-offer on ~day 129 if still under).
        cur.execute(
            "SELECT t.id "
            "  FROM tenants t "
            " WHERE t.paid_conversion_at IS NOT NULL "
            "   AND t.paid_conversion_at + interval '39 days' <= %s "
            "   AND t.phase IN ('paid_active', 'paid_at_risk') "
            "   AND NOT EXISTS ("
            "       SELECT 1 FROM pipeline_log p "
            "        WHERE p.tenant_id = t.id "
            "          AND p.event_type = 'refund_executed') "
            "   AND NOT EXISTS ("
            "       SELECT 1 FROM pipeline_log p "
            "        WHERE p.tenant_id = t.id "
            "          AND p.event_type = 'day39_continue' "
            "          AND p.created_at > %s::timestamptz - interval '90 days')",
            (now, now),
        )
        return [row["id"] for row in cur.fetchall()]


def _send_day39_refund_offer(tenant_id: UUID, verdict: Any) -> None:
    """VT-85: day-39 refund OFFER (replaces the VT-92 auto-refund). Sends the
    refund_offer template + parks the tenant in ``refund_offered`` via
    ``apply_transition(day39_refund_offered)``. NO money moves here (Pillar 7 — no
    auto-refund without consent); the owner's REFUND/CONTINUE/DISCUSS reply or the
    48h timeout resolves it. ``apply_transition`` is a @DBOS.step and may fail under
    a direct synchronous (canary) call without a DBOS context — log + continue; the
    day39_refund_offered pipeline_log event (emitted by the evaluator) is the
    primary signal."""
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
                "day39 offer: tenant %s phase=%s not eligible for an offer; skipped",
                tenant_id,
                current_phase,
            )
            return
        # Flip the phase FIRST (the gate the reply intake keys on), THEN send the
        # offer template — a tenant who receives the offer is then always correctly
        # parked in refund_offered to make a reply. The template send is best-effort
        # (null SID -> no-op today, NEEDS-FAZAL).
        state = new_subscriber_state(tenant_id=tenant_id, run_id=uuid4(), phase=current_phase)
        apply_transition(state, "day39_refund_offered", {"reason": "day39_offer"})
        _send_refund_offer_template(tenant_id, verdict)
    except Exception:  # noqa: BLE001
        logger.exception(
            "day39 offer-send failed for tenant %s; the day39_refund_offered "
            "pipeline_log event was emitted by the evaluator and remains the primary signal",
            tenant_id,
        )


def _send_refund_offer_template(tenant_id: UUID, verdict: Any) -> None:
    """Send the refund_offer WABA template (full-refund amount = cumulative fees).
    null SID (NEEDS-FAZAL) -> send returns success=False; log loud, never block the
    offer state (Pillar 7 — honest; the template send is best-effort)."""
    from orchestrator.utils.twilio_send import send_template_message

    try:
        refund_inr = round(int(verdict.cumulative_fees_paise) / 100)
        result = send_template_message(
            tenant_id,
            "refund_offer",
            {"1": str(refund_inr), "2": "Reply REFUND, CONTINUE, or DISCUSS"},
        )
        if not result.success:
            logger.warning(
                "day39 offer: refund_offer template not sent (SID null / NEEDS-FAZAL) tenant=%s",
                tenant_id,
            )
    except Exception:  # noqa: BLE001 — notify is best-effort, never blocks the offer
        logger.exception("day39 offer: refund_offer send raised tenant=%s", tenant_id)


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
                tenant_id,
                target_month,
            )
        notified.append(tenant_id)
    return notified


# ---------------------------------------------------------------------------
# 5. Owner-approval timeout sweep — REAL body (VT-47)
# ---------------------------------------------------------------------------
#
# CL-240: EXTEND the existing scheduled-trigger surface — do NOT add a parallel
# poller. This is the 5th @DBOS.scheduled handler, registered alongside the
# other four in register_scheduled_triggers(). It marks owner-approval pauses
# that blew past their timeout_at as timed_out and resumes the affected run
# with decision='timeout' (an explicit NON-approval terminal — Pillar 7:
# timeout never auto-approves).

APPROVAL_TIMEOUT_SWEEP_CRON = "*/30 * * * *"  # every 30 minutes
APPROVAL_TIMED_OUT_EVENT = "approval_timed_out"


def approval_timeout_sweep_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires every 30 min. Resumes timed-out pauses.

    NO LLM CALL — the timeout decision is a fixed verb, not classified.
    """
    run_approval_timeout_sweep_body(now=actual_time)


def _scan_timed_out_approvals(now: datetime) -> list[dict[str, Any]]:
    """Return open approvals past timeout_at (service-role read, workspace-wide).

    Workspace-level scan (no GUC) so it sees every tenant's stale pauses; the
    per-row resume below sets the tenant GUC for each (R4 — RLS on the
    checkpoint + pending_approvals tables needs the tenant context per resume).
    """
    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id::text AS id, tenant_id::text AS tenant_id,
                   run_id::text AS run_id
            FROM pending_approvals
            WHERE resolved_at IS NULL AND timeout_at <= %s
            ORDER BY timeout_at ASC
            """,
            (now,),
        )
        return [dict(row) for row in cur.fetchall()]


def run_approval_timeout_sweep_body(now: datetime | None = None) -> list[UUID]:
    """Owner-approval timeout sweep body — REAL (VT-47).

    For each open approval past its timeout_at: resolve it with
    decision='timeout' (status='timed_out') and resume the paused run so the
    graph reaches a terminal state (the gate node returns owner_decision=
    'timeout'; the campaign does NOT send — Pillar 7). Returns the list of
    resolved approval ids for canary inspection.

    Callable directly with an injected ``now`` (mirrors the other four bodies)
    so the canary can drive a past-timeout row without waiting for the cron.

    Per-approval try/except: one stuck resume must not halt the sweep
    (observability-safe, matches attribution_close / day39 bodies).
    """
    from orchestrator.agent.approval_resume import mark_approval_resolved, resume_run
    from orchestrator.db import tenant_connection

    now = now or datetime.now(timezone.utc)
    timed_out = _scan_timed_out_approvals(now)
    resolved: list[UUID] = []
    for approval in timed_out:
        approval_id = approval["id"]
        tenant_id = approval["tenant_id"]
        run_id = approval["run_id"]
        try:
            # Set the tenant GUC for the resolve write (RLS).
            with tenant_connection(tenant_id) as conn:
                mark_approval_resolved(conn, tenant_id, approval_id, "timeout")
            # Resume the suspended run with the timeout decision, then close
            # the original paused run.
            resume_run(run_id, "timeout")
            with tenant_connection(tenant_id) as conn:
                conn.execute(
                    "UPDATE pipeline_runs SET status = 'completed', ended_at = now() WHERE id = %s",
                    (run_id,),
                )
            log_event(
                event_type=APPROVAL_TIMED_OUT_EVENT,
                run_id=UUID(run_id),
                tenant_id=UUID(tenant_id),
                severity="info",
                component="scheduled_trigger",
                payload={
                    # CL-390: ids + decision only; no PII.
                    "approval_id": approval_id,
                    "decision": "timeout",
                    "swept_at_utc": now.astimezone(timezone.utc).isoformat(),
                },
            )
            resolved.append(UUID(approval_id))
        except Exception:  # noqa: BLE001 — one stuck resume must not halt the sweep
            logger.exception(
                "approval_timeout_sweep: resume failed for approval %s "
                "(tenant=%s run=%s); sweep continues",
                approval_id,
                tenant_id,
                run_id,
            )
    return resolved


# ---------------------------------------------------------------------------
# 6. Day-39 refund-OFFER timeout sweep — VT-85
# ---------------------------------------------------------------------------
#
# An un-answered day-39 refund offer defaults to CONTINUE after 48h (Pillar 7 —
# the timeout NEVER auto-refunds; auto-refund without consent is financially
# destabilizing, so the safe default keeps the tenant on). EXTENDS the
# scheduled-trigger surface (CL-240), not a parallel poller.

REFUND_OFFER_TIMEOUT_HOURS = 48
REFUND_OFFER_TIMEOUT_SWEEP_CRON = "15 */6 * * *"  # every 6h, offset off the others


def refund_offer_timeout_sweep_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — defaults un-answered day-39 refund offers (>48h in
    refund_offered) to CONTINUE. NO LLM — the timeout default is a fixed verb."""
    run_refund_offer_timeout_sweep_body(now=actual_time)


def _scan_timed_out_refund_offers(now: datetime) -> list[str]:
    """Tenants parked in 'refund_offered' past the 48h deadline (service-role
    workspace scan; the per-tenant default below sets the tenant GUC via RLS)."""
    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id::text AS id FROM tenants "
            " WHERE phase = 'refund_offered' "
            "   AND phase_entered_at <= %s::timestamptz - make_interval(hours => %s) "
            " ORDER BY phase_entered_at ASC",
            (now, REFUND_OFFER_TIMEOUT_HOURS),
        )
        return [row["id"] for row in cur.fetchall()]


def run_refund_offer_timeout_sweep_body(now: datetime | None = None) -> list[UUID]:
    """48h refund-offer timeout sweep body — VT-85. Defaults each un-answered offer
    to CONTINUE (reuses the reply handler's continue path: day39_continue event +
    transition refund_offered -> paid_active). Per-tenant try/except (one stuck
    default must not halt the sweep). Returns the defaulted tenant ids (canary)."""
    from orchestrator.owner_inputs.refund_reply import _resume_paid_active

    now = now or datetime.now(timezone.utc)
    defaulted: list[UUID] = []
    for tid in _scan_timed_out_refund_offers(now):
        try:
            _resume_paid_active(UUID(tid), source="timeout")
            log_event(
                event_type="day39_refund_decision",
                run_id=uuid4(),
                tenant_id=UUID(tid),
                severity="info",
                component="scheduled_trigger",
                payload={"tenant_id": tid, "decision": "continue", "source": "timeout"},
            )
            defaulted.append(UUID(tid))
        except Exception:  # noqa: BLE001 — one stuck default must not halt the sweep
            logger.exception(
                "refund_offer_timeout_sweep: default-continue failed tenant=%s; sweep continues",
                tid,
            )
    return defaulted


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
    """Apply the ``@DBOS.scheduled`` decoration to the 5 trigger handlers.

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

    VT-47 (CL-240): the 5th handler is the owner-approval timeout sweep —
    EXTENDING this surface, NOT a parallel poller.
    """
    global _registered
    if _registered:
        return
    DBOS.scheduled(WEEKLY_CADENCE_CRON)(weekly_cadence_scheduled)
    DBOS.scheduled(ATTRIBUTION_CLOSE_CRON)(attribution_close_scheduled)
    DBOS.scheduled(DAY39_EVALUATION_CRON)(day39_evaluation_scheduled)
    # VT-90: 12th handler — daily trial-lifecycle sweep (extend/exhaust/warn).
    DBOS.scheduled(TRIAL_EVALUATION_CRON)(trial_evaluation_scheduled)
    DBOS.scheduled(MONTHLY_IMPACT_CRON)(monthly_impact_scheduled)
    DBOS.scheduled(APPROVAL_TIMEOUT_SWEEP_CRON)(approval_timeout_sweep_scheduled)
    DBOS.scheduled(L3_CONSTRUCTION_CRON)(l3_construction_scheduled)
    # VT-76 (CL-240): 7th handler — opt-out reconstitution sweep. EXTENDS this
    # surface, NOT a parallel poller. The cron + body live in privacy/reconstitution.
    from orchestrator.privacy.reconstitution import RECONSTITUTION_CRON

    DBOS.scheduled(RECONSTITUTION_CRON)(reconstitution_sweep_scheduled)
    # VT-304: 8th handler — nightly audit-chain verify. EXTENDS the surface
    # (same register-before-launch posture; app_version shifts once, here).
    DBOS.scheduled(AUDIT_CHAIN_VERIFY_CRON)(audit_chain_verify_scheduled)
    # VT-305: 9th handler — nightly PII-in-log sweep (VT-79 Detector-5).
    DBOS.scheduled(PII_LOG_SWEEP_CRON)(pii_log_sweep_scheduled)
    # VT-307: 10th handler — nightly KG-events outbox-drain straggler sweep.
    DBOS.scheduled(KG_DRAIN_SWEEP_CRON)(kg_drain_sweep_scheduled)
    # VT-311: 11th handler — nightly L2 episodic retention soft-delete sweep.
    DBOS.scheduled(L2_RETENTION_SWEEP_CRON)(l2_retention_sweep_scheduled)
    # VT-85: 12th handler — day-39 refund-offer 48h timeout sweep (default CONTINUE).
    DBOS.scheduled(REFUND_OFFER_TIMEOUT_SWEEP_CRON)(refund_offer_timeout_sweep_scheduled)
    # VT-354: waitlist 6-month retention purge — ENFORCES the DPDP pre-launch PII bound (was
    # runbook-manual). EXTENDS this surface (same register-before-launch posture).
    DBOS.scheduled(WAITLIST_RETENTION_PURGE_CRON)(waitlist_retention_purge_scheduled)
    # VT-357: hourly SLA-breach sweep — 2nd Fazal alert on overdue open escalations (marker-gated).
    DBOS.scheduled(SLA_BREACH_SWEEP_CRON)(sla_breach_sweep_scheduled)
    _registered = True


__all__ = [
    "APPROVAL_TIMED_OUT_EVENT",
    "APPROVAL_TIMEOUT_SWEEP_CRON",
    "ATTRIBUTION_CLOSED_EVENT",
    "ATTRIBUTION_CLOSE_CRON",
    "ATTRIBUTION_CLOSE_SHELL_EVENT",
    "DAY39_CONTINUE_EVENT",
    "DAY39_EVALUATION_CRON",
    "DAY39_REFUND_TRIGGERED_EVENT",
    "L3_CONSTRUCTION_CRON",
    "DAY39_SHELL_EVENT",
    "MONTHLY_IMPACT_CRON",
    "MONTHLY_IMPACT_SHELL_EVENT",
    "MONTHLY_IMPACT_STARTED_EVENT",
    "SHELL_STATUS",
    "WEEKLY_CADENCE_CRON",
    "WEEKLY_CADENCE_EVENT",
    "approval_timeout_sweep_scheduled",
    "attribution_close_scheduled",
    "attribution_close_workflow_id",
    "day39_evaluation_scheduled",
    "day39_workflow_id",
    "AUDIT_CHAIN_VERIFY_CRON",
    "KG_DRAIN_SWEEP_CRON",
    "L2_RETENTION_SWEEP_CRON",
    "PII_LOG_SWEEP_CRON",
    "audit_chain_verify_scheduled",
    "kg_drain_sweep_scheduled",
    "l2_retention_sweep_scheduled",
    "pii_log_sweep_scheduled",
    "monthly_impact_scheduled",
    "monthly_workflow_id",
    "reconstitution_sweep_scheduled",
    "register_scheduled_triggers",
    "run_approval_timeout_sweep_body",
    "run_attribution_close_body",
    "run_day39_evaluation_body",
    "run_monthly_impact_body",
    "run_weekly_cadence_body",
    "weekly_cadence_scheduled",
    "weekly_workflow_id",
]
