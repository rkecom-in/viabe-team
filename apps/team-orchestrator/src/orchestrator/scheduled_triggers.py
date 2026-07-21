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
3. **Monthly impact** — 1st-of-month 8 AM IST. Pure deterministic data
   prep. **SHELL form** — emits ``monthly_impact_shell`` with
   ``status: skipped_schema_pending``. Reserved completion event
   ``monthly_impact_started`` gated on VT-175.

CL-274 plumbing-mode note
-------------------------
VT-28 proves the weekly cadence trigger fires + reaches Anthropic; it does
NOT prove the cadence produces useful output. The deterministic
triggers are SHELLS in this row pending VT-175 schema. Phantom-Done
prevention per CL-318/319/380: reserved completion event names
(``attribution_closed`` / ``monthly_impact_started``)
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
import os
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
TRIAL_EVALUATION_CRON = "0 7 * * *"  # VT-90 — daily 7 AM IST trial sweep (off-peak)
MONTHLY_IMPACT_CRON = "0 8 1 * *"
L3_CONSTRUCTION_CRON = "0 3 * * *"  # VT-68 — nightly 3 AM IST L3 rebuild
WAITLIST_RETENTION_PURGE_CRON = "0 4 * * *"  # VT-354 — daily 4 AM IST waitlist 6-month bound
SLA_BREACH_SWEEP_CRON = "0 * * * *"  # VT-357 — hourly: alert Fazal on SLA-breached open escalations
VTR_DIGEST_CRON = "30 8 * * *"  # VT-280 — daily 8:30 AM IST VTR digest (de-identified, app_vtr_role)
# VT-432: daily 04:30 IST (23:00 UTC) — after the attribution_close + redaction/reconstitution
# batch; before the trial-evaluation sweep. Pure SQL, no LLM, no send.
IMPLICIT_ATTRIBUTION_SWEEP_CRON = "0 23 * * *"
# VT-439: daily 01:00 UTC (06:30 IST) — Razorpay orphan-DETECT backstop. Runs after the
# attribution_close and outbox batches; off-peak billing window. DETECT-ONLY (no cancel/charge).
RECONCILE_SUBSCRIPTION_ORPHANS_CRON = "0 1 * * *"
# VT-679 (§7A proactive planning): monthly plan-refresh — 1st of month 03:30 UTC = 09:00 IST
# (matches monthly_impact's own 09:00 IST slot on a different minute).
PLAN_REFRESH_CRON = "30 3 1 * *"
# VT-679: daily proactive-initiative pick — 05:00 UTC = 10:30 IST, inside owner waking hours BY
# CRON PLACEMENT. See ``_assert_ist_daytime`` for the belt-and-suspenders runtime guard.
DAILY_INITIATIVE_CRON = "0 5 * * *"


SHELL_STATUS = "skipped_schema_pending"

# VT-679 — proactive planning is fully OFF by default; both new scheduled handlers below no-op
# fast (before any DB scan) when this is unset. Mirrors the house boolean-flag convention
# (TEAM_TWILIO_MOCK_MODE / TEAM_SANDBOX_GST_MOCK_MODE / TEAM_RAZORPAY_LIVE — exact string "1").
TEAM_PROACTIVE_PLANNING_FLAG = "TEAM_PROACTIVE_PLANNING"


def _proactive_planning_enabled() -> bool:
    return os.environ.get(TEAM_PROACTIVE_PLANNING_FLAG, "0") == "1"


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
# 3. Trial-lifecycle + other scheduled sweeps
# ---------------------------------------------------------------------------


def trial_evaluation_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires daily 7 AM IST. VT-90 trial sweep. NO LLM.

    VT-426 (Row C): wires the REAL owner notify (``_owner_notify`` → the VT-393
    ``send_owner_template`` seam, registry-driven by template NAME) so a trial-ending /
    expiring tenant actually gets the owner WhatsApp — replacing the logging-only
    ``_default_notify`` stub. The notify FAIL-SAFE-SKIPs while the trial-ending Content
    SID is a pending-approval stub (NEEDS-FAZAL); it sends with zero code change once the
    approved SID lands in twilio_templates.yaml.
    """
    from orchestrator.billing.trial_sweep import _owner_notify, run_trial_evaluation_body

    run_trial_evaluation_body(now=actual_time, notify_fn=_owner_notify)


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


def vtr_digest_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires daily 8:30 AM IST. VT-280: the VTR digest, read ONLY via
    app_vtr_role + the VT-281 de-identified views (CL-425 DB-enforced on this path). NO LLM, NO PII."""
    from orchestrator.owner_surface.vtr_digest import run_vtr_digest_body

    run_vtr_digest_body(now=actual_time)


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

    from orchestrator.alerts.clients import (
        alert_is_dev_routed,
        send_resend_email,
        send_telegram,
    )

    text = (
        "[CRITICAL] privacy_audit_log hash-chain BREAK (VT-80/VT-304) — "
        f"tamper/corruption at seq={getattr(result, 'broken_seq', None)}: "
        f"{getattr(result, 'reason', None)}. rows_checked="
        f"{getattr(result, 'rows_checked', None)}."
    )

    # VT-502: this is the OTHER alert path that emitted DIRECT to ViabeOps (it
    # can't use the per-tenant tenant_alerts path — the chain is global/NULL-tenant).
    # Gate it through the same VT-489 dev-routing decision so a dev-env chain
    # break routes to the DEV bot (and skips real email), never PROD ops. On
    # prod (EXPECTED_ENV=prod) this is False → OPS bot + email, exactly as before.
    dev_routed = alert_is_dev_routed(None)  # global alert — env arm only
    if dev_routed:
        bot_token = os.environ.get("TELEGRAM_DEV_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_DEV_CHAT_ID", "")
    else:
        bot_token = os.environ.get("TELEGRAM_OPS_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_OPS_CHAT_ID", "")

    async def _send() -> None:
        await send_telegram(bot_token, chat_id, text)
        if dev_routed:
            return  # dev/non-prod never emails real ops
        from orchestrator.alerts.email_senders import sender_from

        await send_resend_email(
            os.environ.get("RESEND_API_KEY", ""),
            sender_from("alerts"),  # VT-113: canonical registry (ops@ via RESEND_FROM_EMAIL override)
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


# VT-374: daily expired-override cancel sweep. 22:00 UTC = 03:30 IST (off-peak,
# between the 03:00 PII-log sweep and the 04:00 reconstitution sweep). UTC-correct
# cron (matches audit-chain/PII-log/KG-drain).
OVERRIDE_EXPIRY_SWEEP_CRON = "0 22 * * *"


def override_expiry_sweep_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — daily 03:30 IST (VT-374). Cancels expired, unconsumed
    ``step_overrides`` rows via ``run_control.expire_overrides_sweep`` (F8: next-run
    pins REQUIRE ``expires_at``, and the bound is only real because this sweep
    enforces it — an expired pin must never fire on a much-later run). NO LLM;
    idempotent. Best-effort: a sweep failure must not crash the scheduler (the
    consume predicate also expiry-gates fresh claims, so the next run re-catches)."""
    from orchestrator.run_control import expire_overrides_sweep

    try:
        expire_overrides_sweep()
    except Exception:  # noqa: BLE001 — daily sweep is best-effort; next run retries
        logger.exception("VT-374 override-expiry sweep scheduled run failed")


# VT-382: daily outbox-redaction sweep. 22:30 UTC = 04:00 IST (off-peak, after the
# 03:30 override-expiry sweep). UTC-correct cron (matches the other nightly sweeps).
# The reliability BACKSTOP for the inline redact-on-terminal hooks (customer_send /
# approval_glue / autonomy): redacts params/owner_feedback on rows ALREADY terminal that
# the inline hook never ran for — the CL-437 ruling-3.3 backfill clause — and captures the
# exact owner-facing text for historical 'sent' rows still holding raw params BEFORE
# redacting them (one-shot policy-honesty leg). NEVER touches non-terminal rows
# (drafted/sending/edit_requested) — retain-while-needed is the policy.
OUTBOX_REDACTION_SWEEP_CRON = "30 22 * * *"


def outbox_redaction_sweep_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — daily 04:00 IST (VT-382). Redacts outbox bodies on rows
    already in a terminal status (CL-437 ruling 3 backfill + the inline-hook backstop) and
    captures historical sent-row text into owner_message_audit before redacting. NO LLM;
    batched; idempotent (already-redacted values pass through unchanged). Best-effort: a
    sweep failure must not crash the scheduler (the next run + the inline hooks re-catch)."""
    from orchestrator.agents.outbox_redaction import sweep_terminal_rows

    try:
        sweep_terminal_rows()
    except Exception:  # noqa: BLE001 — daily sweep is best-effort; next run retries
        logger.exception("VT-382 outbox-redaction sweep scheduled run failed")


# VT-432: daily implicit-attribution sweep. 23:00 UTC = 04:30 IST — after the
# attribution_close + outbox-redaction/reconstitution batch. Pure SQL, no LLM,
# NO SEND — derives thumbs_up/thumbs_down from attribution_outcome vs baseline
# and writes implicit owner_feedback rows (idempotent via partial unique index
# on migration 041). The sweep is a computation/write pass only; no Twilio/Resend
# path is reachable from run_implicit_attribution_sweep.


def implicit_attribution_sweep_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — daily 04:30 IST (VT-432). Runs the VT-198 implicit
    attribution sweep: derives thumbs_up/thumbs_down from attribution_outcome vs
    baseline for campaigns completed in the last 7 days and writes implicit
    owner_feedback rows. Pure SQL (NO LLM, NO SEND). Idempotent — the partial unique
    index on (tenant_id, run_id, tier='implicit') makes re-runs safe. Best-effort: a
    sweep failure must not crash the scheduler."""
    from orchestrator.feedback.implicit_attribution import run_implicit_attribution_sweep

    try:
        result = run_implicit_attribution_sweep()
        logger.info(
            "VT-432 implicit_attribution_sweep: considered=%d written=%d skipped=%d",
            result.get("considered", 0),
            result.get("written", 0),
            result.get("skipped_no_outcome", 0),
        )
    except Exception:  # noqa: BLE001 — daily sweep is best-effort; next run retries
        logger.exception("VT-432 implicit-attribution sweep scheduled run failed")


# ---------------------------------------------------------------------------
# VT-439: daily Razorpay vendor-orphan reconciliation — DETECT-ONLY backstop
# ---------------------------------------------------------------------------
# Pairs with billing/dead_letter.py F7 (VT-352). DETECT-ONLY: no cancel,
# no charge, no send. Fetches all committed razorpay_subscription_ids from the
# subscriptions table and delegates to reconcile_subscription_orphans — which
# finds any vendor subscription with NO matching DB row and alerts Fazal.
#
# Pre-LIVE posture: the subscription.create is still a STUB, so the known set
# equals the DB set and orphan detection produces an empty list (vacuously safe).
# At TEAM_RAZORPAY_LIVE cutover, replace the DB query with razorpay.subscription.all()
# to get the vendor-authoritative list (VT-352 F2 acceptance step).


def reconcile_subscription_orphans_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — daily 01:00 UTC / 06:30 IST (VT-439). Runs the
    VT-352 F2 Razorpay vendor-orphan DETECT backstop: fetches all committed
    razorpay_subscription_ids from the subscriptions table and calls
    :func:`orchestrator.api.razorpay_subscribe.reconcile_subscription_orphans` to
    surface any vendor subscription with NO DB row (a commit-after-vendor failure
    the Idempotency-Key didn't cover). DETECT-ONLY — NO auto-cancel, NO charge,
    NO send. Best-effort: a reconcile failure must not crash the scheduler.

    Pre-LIVE: vendor list = DB-committed subscriptions (vacuously zero orphans).
    At TEAM_RAZORPAY_LIVE cutover, swap the DB query for razorpay.subscription.all()
    to get the vendor-authoritative list (VT-352 F2 live acceptance step)."""
    from orchestrator.api.razorpay_subscribe import reconcile_subscription_orphans
    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    try:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT razorpay_subscription_id FROM subscriptions "
                "WHERE razorpay_subscription_id IS NOT NULL"
            )
            vendor_ids = [r["razorpay_subscription_id"] for r in cur.fetchall()]
        orphans = reconcile_subscription_orphans(vendor_ids)
        logger.info(
            "VT-439 reconcile_subscription_orphans: checked=%d orphans=%d",
            len(vendor_ids),
            len(orphans),
        )
    except Exception:  # noqa: BLE001 — daily sweep is best-effort; next run retries
        logger.exception("VT-439 reconcile_subscription_orphans scheduled run failed")


# ---------------------------------------------------------------------------
# VT-440: dead-letter retry sweep — DETECT/ALERT-ONLY backstop
# ---------------------------------------------------------------------------
# Pairs with billing/dead_letter.py F7 (VT-352). The dead-letter "retry" is the
# operator-driven dead_letter.replay(event_id, corrected_payload): it RE-FEEDS a
# CORRECTED event through razorpay_ingress so the dropped charge's fee NOW applies.
# That replay CANNOT be automated by a cron — it needs an operator-supplied
# corrected payload (the row was dropped precisely because its amount was a
# DETERMINISTIC malformation; the sweep has no way to invent the correct integer).
# So this scheduled handler is DETECT/ALERT-ONLY: it counts the still-pending
# dead-letters and alerts Fazal when any exist (the F7 "scheduled job that
# list_pending → alerts" prescription). NO replay, NO charge, NO send-of-money,
# NO write — a read-only sweep, trivially idempotent (running it twice produces
# the same count + the same best-effort alert; zero money effect either way).
#
# MONEY-SAFETY (the load-bearing invariant): the exactly-once guarantee for the
# ACTUAL retry lives in razorpay_ingress — the razorpay_webhook_events.event_id
# dedup row is the COMMIT POINT (ingress docstring Q1). A genuinely-PROCESSED
# event re-arriving (the same event_id, processed_at set) CONFLICTs → no
# re-processing → {"status": "duplicate", "action": "noop"}: fees never
# double-count. Only an un-applied parse-drop (processed_at IS NULL AND
# _status='dropped_parse_error') replays past the dedup and applies its fee
# exactly once, then sets processed_at + flips the dead-letter row to 'replayed'.
# This sweep surfaces the stuck drops; the ingress keystone makes any replay of
# them exactly-once.
#
# 22:30 UTC / 04:00 IST — off-peak, after the 01:00 UTC VT-439 orphan-detect
# backstop (same daily Razorpay-reconciliation batch).
DEAD_LETTER_RETRY_SWEEP_CRON = "30 22 * * *"


def dead_letter_retry_sweep_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — daily 04:00 IST / 22:30 UTC (VT-440). The VT-352 F7
    dead-letter backstop: counts still-pending Razorpay webhook dead-letters
    (parse-dropped charge events awaiting an operator-supplied corrected replay)
    and alerts Fazal when any exist. DETECT/ALERT-ONLY — NO replay, NO charge, NO
    send-of-money, NO write (the actual replay is operator-driven
    dead_letter.replay, which needs a corrected payload a cron can't synthesize).

    Idempotent: read-only COUNT + best-effort alert; two runs produce the same
    result and zero money effect. The exactly-once guarantee for any eventual
    replay lives in razorpay_ingress (the event_id dedup keystone — a processed
    event re-arriving is a noop, never a double-charge). Best-effort: a count
    failure must not crash the scheduler (the next run re-catches)."""
    from orchestrator.billing.dead_letter import count_pending

    try:
        pending = count_pending()
        logger.info("VT-440 dead_letter_retry_sweep: pending=%d", pending)
        if pending > 0:
            from orchestrator.alerts.clients import alert_fazal

            # PII-free: a count only (the dead-letter table is PII-free routing
            # fields). No event_ids/payloads in the alert.
            alert_fazal(
                f"VT-352/VT-440 razorpay dead-letter backstop: {pending} pending "
                "parse-dropped charge event(s) awaiting a corrected replay "
                "(dead_letter.replay) — manual reconciliation needed."
            )
    except Exception:  # noqa: BLE001 — daily sweep is best-effort; next run retries
        logger.exception("VT-440 dead_letter_retry_sweep scheduled run failed")


# ---------------------------------------------------------------------------
# VT-560: the boot-only reapers as STEADY-STATE scheduled sweeps
# ---------------------------------------------------------------------------
# reap_stalled_manager_tasks (VT-525/VT-557 retry ladder — VT-560 also wakes
# reaper-parked tasks) and reap_orphan_runs (VT-481) previously ran EXACTLY ONCE
# in the FastAPI lifespan (a boot catch-up). On a long-lived process they
# therefore never re-swept — so the VT-557 retry ladder could never progress.
# VT-560 registers BOTH on the @DBOS.scheduled substrate as the steady-state
# sweeps; the main.py boot invocations stay as the startup catch-up. Both bodies
# are already best-effort (never raise); the handler wrappers mirror the other
# sweeps in this module. NO LLM — pure SQL reaper paths (Pillar 1).
#
# detect_silent_terminal_runs (VT-552) is DELIBERATELY NOT scheduled (batch-review
# finding): no live code writes the final_outcome the detector keys on — the
# close path stamps neither the column nor the JSONB key — so under traffic a
# scheduled detector would open an incident + fire an alert for essentially every
# completed run, every tick (a storm that also drowns real silent terminals). It
# stays a boot-time catch-up (main.py); schedule it ONLY once the close-path
# final_outcome writer lands (rostered follow-up).
STALLED_TASK_SWEEP_CRON = "*/10 * * * *"  # every 10 min — VT-557 retry-ladder progression
ORPHAN_RUN_REAPER_CRON = "0 * * * *"  # hourly — VT-481 stranded-'running' run reaper
TEST_TENANT_REAPER_CRON = "15 * * * *"  # hourly (offset 15m) — VT-620 leaked convo-harness tenant GC


def stalled_task_sweep_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — every 10 min (VT-560). Runs the VT-557 manager-task
    retry ladder: wakes reaper-parked tasks whose backoff elapsed (blocked->planned,
    VT-560 Defect 1) and re-sweeps tasks stranded active with no runnable step, walking
    them up the backoff ladder to dead_letter at the budget. NO LLM; the body is
    best-effort (never raises). Best-effort: a sweep failure must not crash the scheduler."""
    from orchestrator.orphan_reaper import reap_stalled_manager_tasks

    try:
        reap_stalled_manager_tasks()
    except Exception:  # noqa: BLE001 — sweep is best-effort; next run retries
        logger.exception("VT-560 stalled-task sweep scheduled run failed")


def orphan_run_reaper_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — hourly (VT-560). Runs the VT-481 orphan-run reaper:
    closes pipeline_runs stranded status='running' past the >1h floor (a process died
    mid-run; DBOS can't recover a prior-app-version row). NO LLM; the body is best-effort
    (never raises). Best-effort: a reaper failure must not crash the scheduler."""
    from orchestrator.orphan_reaper import reap_orphan_runs

    try:
        reap_orphan_runs()
    except Exception:  # noqa: BLE001 — reaper is best-effort; next run retries
        logger.exception("VT-560 orphan-run reaper scheduled run failed")


def test_tenant_reaper_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — hourly (VT-620). Runs the test-tenant reaper: FK-safely deletes
    leaked ``convo-harness-…`` tenants (+ their runs/steps) past the >1h floor so they stop
    flowing into the alert detectors as ops noise. STRICT scope: only the convo-harness name
    pattern. NO LLM; the body is best-effort (never raises). Best-effort: a reaper failure must
    not crash the scheduler."""
    from orchestrator.test_tenant_reaper import reap_test_tenants

    try:
        reap_test_tenants()
    except Exception:  # noqa: BLE001 — reaper is best-effort; next run retries
        logger.exception("VT-620 test-tenant reaper scheduled run failed")


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
                   run_id::text AS run_id, approval_type
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

    VT-609 fix round 2: a ``business_policy_grant`` approval has no LangGraph checkpoint to resume
    (its ``run_id`` is a minimal ``pipeline_runs`` row) — it skips ``resume_run`` and closes that
    run directly instead (same durable-state shape ``runner.try_resume_pending_approval`` uses).

    VT-611 pre-work #7: an ``agent_customer_send`` approval ALSO has no LangGraph checkpoint
    (a real dispatch run, but not a graph-invoke one) — it skips BOTH ``resume_run`` AND the
    pipeline_runs close (that run belongs to the agent dispatch workflow, not this approval;
    mirrors ``runner.try_resume_pending_approval``'s own agent_customer_send branch, which also
    never closes it).

    Callable directly with an injected ``now`` (mirrors the other bodies)
    so the canary can drive a past-timeout row without waiting for the cron.

    Per-approval try/except: one stuck resume must not halt the sweep
    (observability-safe, matches the attribution_close body).
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
        approval_type = approval.get("approval_type")
        try:
            # Set the tenant GUC for the resolve write (RLS).
            with tenant_connection(tenant_id) as conn:
                mark_approval_resolved(conn, tenant_id, approval_id, "timeout")
            # VT-609 fix round 2: a business_policy_grant proposal's run_id is a MINIMAL
            # pipeline_runs row (propose_business_policy_grant opens it only to satisfy the FK) —
            # never a paused LangGraph run, so there is NO checkpoint for resume_run to resume
            # (same shape as runner.try_resume_pending_approval's own business_policy_grant
            # branch). The grant/no-grant decision already landed inside mark_approval_resolved's
            # transaction above; calling resume_run here would just raise. Close the minimal run
            # directly instead.
            if approval_type == "business_policy_grant":
                with tenant_connection(tenant_id) as conn:
                    conn.execute(
                        "UPDATE pipeline_runs SET status = 'completed', ended_at = now() "
                        "WHERE id = %s",
                        (run_id,),
                    )
            elif approval_type == "agent_customer_send":
                # VT-611 pre-work #7 (the SAME class VT-609 fixed for business_policy_grant, but
                # pre-existing/unrelated to VT-609): an agent_customer_send approval's run_id IS a
                # REAL pipeline_runs row (l3_hold._resolve_batch_run_id resolves the agent
                # dispatch's own deterministic run id — no fabricated FK-satisfying stub), but it
                # has NO LangGraph checkpoint under that thread_id: "the agent dispatch workflow
                # owns its own run lifecycle and picks the batch status up on its next
                # deterministic step" (runner.try_resume_pending_approval's OWN agent_customer_send
                # branch, which likewise never calls resume_run). Calling resume_run here would
                # just raise (a guaranteed checkpoint-miss), get swallowed by this loop's own
                # try/except below, and skip the close-update entirely — leaving the run stuck
                # 'running' forever AND logging a false "resume failed". The owner-decision side
                # (batch -> 'cancelled' on a timeout) already landed inside mark_approval_resolved's
                # transaction above (approval_glue's shared resolution choke point) — there is
                # nothing left to resume, and this run is NOT this approval's to close (mirrors
                # runner.py's branch: no resume_run, no pipeline_runs UPDATE, just log + continue).
                pass
            else:
                # Resume the suspended run with the timeout decision, then close
                # the original paused run.
                resume_run(run_id, "timeout")
                with tenant_connection(tenant_id) as conn:
                    conn.execute(
                        "UPDATE pipeline_runs SET status = 'completed', ended_at = now() "
                        "WHERE id = %s",
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
# VT-683 P2b — owner-comms queue sweep (the drainer's scheduled leg + hygiene)
# ---------------------------------------------------------------------------
# EXTENDS the scheduled-trigger surface (CL-240 — never a parallel poller). Three legs, every
# 10 minutes:
#   1. DELIVER: for each tenant holding queued owner-comms, when the 24h session is OPEN and the
#      owner has been idle >= SWEEP_IDLE_MINUTES (never mid-exchange), deliver ONE item
#      (owner_comms_drainer.drain_one — approval > report > notice). The post-turn hook
#      (runner._post_turn_drain_step) is the low-latency leg; this sweep is the systematic one.
#   2. DROP-STALE: queued items never delivered within MAX_QUEUE_AGE drop honestly
#      (status='dropped', never a silent vanish — owner_comms_queue.drop_stale).
#   3. NULL-CLOCK BELT (POINT A safety): an OPEN pending_approvals row whose decision clock never
#      started (timeout_at IS NULL, mig 179) and whose arm is older than the grace bound is a
#      crash-orphaned arm (in P2c every live path starts the clock at delivery, seconds after the
#      arm). Start its clock AT now() so the existing */30 approval-timeout sweep resolves it
#      through the ONE resolution choke point (mark_approval_resolved + resume semantics) — this
#      sweep never resolves approvals itself, the money authority stays where it was.
#      P3 NOTE: when the wake-up loop makes long-queued undelivered approvals legitimate, this
#      grace bound must be revisited alongside it.

OWNER_COMMS_SWEEP_CRON = "*/10 * * * *"  # every 10 minutes

# Crash-orphan grace for the NULL-clock belt: every live P2c arm starts the clock at delivery
# (same call stack, seconds later), so a NULL clock older than this is a crashed arm, not a
# queued ask.
_NULL_CLOCK_GRACE_HOURS = 24


def owner_comms_sweep_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires every 10 min. NO LLM CALL (pure deterministic drain)."""
    run_owner_comms_sweep_body(now=actual_time)


def _tenants_with_queued_comms() -> list[str]:
    """Service-role scan: every tenant currently holding a queued owner-comms item (the partial
    drain index covers this). The per-tenant delivery below runs tenant-scoped (RLS)."""
    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT DISTINCT tenant_id::text AS tenant_id FROM owner_comms_queue "
            "WHERE status = 'queued'"
        )
        return [row["tenant_id"] for row in cur.fetchall()]


def _owner_recipient_phone(tenant_id: str) -> str | None:
    """The owner delivery phone (owner_phone, falling back to whatsapp_number) — tenant-scoped
    read, mirroring task_outcome's resolution."""
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT owner_phone, whatsapp_number FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    if row is None:
        return None
    phone = (
        (row.get("owner_phone") or row.get("whatsapp_number"))
        if isinstance(row, dict)
        else (row[0] or row[1])
    )
    return str(phone) if phone else None


def run_owner_comms_sweep_body(now: datetime | None = None) -> dict[str, int]:
    """Owner-comms sweep body — REAL (VT-683 P2b). Returns counts for canary inspection.

    Per-tenant try/except: one broken tenant must not halt the sweep (matches the
    approval-timeout body's posture). ``now`` is injectable for the canary; the idle gate reads
    live DB time regardless (session_window derives from conversation_log).
    """
    from orchestrator.owner_surface import owner_comms_queue as comms_q
    from orchestrator.owner_surface.freeform_acks import resolve_owner_locale
    from orchestrator.owner_surface.owner_comms_drainer import (
        SWEEP_IDLE_MINUTES,
        drain_one,
    )
    from orchestrator.owner_surface.session_window import idle_minutes, session_open

    delivered = 0
    scanned = 0
    for tenant_id in _tenants_with_queued_comms():
        scanned += 1
        try:
            if not session_open(tenant_id):
                continue
            idle = idle_minutes(tenant_id)
            if idle is None or idle < SWEEP_IDLE_MINUTES:
                # Mid-exchange (or unreadable — treat as busy): never interleave into an active
                # back-and-forth; the post-turn hook / next tick covers it.
                continue
            recipient = _owner_recipient_phone(tenant_id)
            result = drain_one(tenant_id, recipient, lang=resolve_owner_locale(tenant_id))
            if result is not None:
                delivered += 1
        except Exception:  # noqa: BLE001 — one tenant must not halt the sweep
            logger.exception("owner_comms_sweep: tenant %s failed; sweep continues", tenant_id)

    # Hygiene leg 2 — honest-expiry drop of never-delivered items.
    dropped = 0
    try:
        dropped = comms_q.drop_stale()
    except Exception:  # noqa: BLE001 — hygiene must not halt the sweep
        logger.exception("owner_comms_sweep: drop_stale failed; sweep continues")

    # Hygiene leg 3 — the POINT A NULL-clock belt (service-role, cross-tenant): start the clock
    # NOW on crash-orphaned arms so the existing approval-timeout sweep resolves them through the
    # single resolution choke point. Never resolves anything here.
    belted = 0
    try:
        from orchestrator.graph import get_pool

        with get_pool().connection() as conn:
            cur = conn.execute(
                "UPDATE pending_approvals SET timeout_at = now() "
                "WHERE resolved_at IS NULL AND timeout_at IS NULL "
                "  AND requested_at < now() - make_interval(hours => %s)",
                (_NULL_CLOCK_GRACE_HOURS,),
            )
            belted = cur.rowcount if cur.rowcount is not None else 0
        if belted:
            logger.warning(
                "owner_comms_sweep: NULL-clock belt started the clock on %d crash-orphaned "
                "arm(s) — the approval-timeout sweep will resolve them", belted,
            )
    except Exception:  # noqa: BLE001 — belt must not halt the sweep
        logger.exception("owner_comms_sweep: NULL-clock belt failed; sweep continues")

    return {"scanned": scanned, "delivered": delivered, "dropped": dropped, "belted": belted}


# ---------------------------------------------------------------------------
# VT-679 (§7A) — proactive planning: monthly plan-refresh + daily initiative
# ---------------------------------------------------------------------------
# Two triggers, both gated behind TEAM_PROACTIVE_PLANNING (default OFF — the handlers below no-op
# fast, before any DB scan, when unset; dev turns it ON, prod stays unset/Fazal per the VT-101
# rollout pattern). Shared workspace scan: active paid/trial tenants only (an onboarding/lapsed/
# cancelled tenant has no business reason for either a plan refresh or a daily initiative).


def _scan_active_paid_or_trial_tenants() -> list[UUID]:
    """The shared workspace scan for BOTH proactive-planning triggers below."""
    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id FROM tenants WHERE phase = ANY(%s)", (["trial", "paid_active"],))
        return [row["id"] for row in cur.fetchall()]


def plan_refresh_scheduled(scheduled_time: datetime, actual_time: datetime) -> None:
    """DBOS scheduled handler — fires 1st of month 09:00 IST / 03:30 UTC (VT-679 D1.1). Behind
    ``TEAM_PROACTIVE_PLANNING`` (no-op fast when unset, before any DB scan). NO LLM in THIS
    handler — the LLM call lives inside the per-tenant ``refresh_business_plan_workflow`` child
    workflow it starts."""
    if not _proactive_planning_enabled():
        return
    run_plan_refresh_body(now=actual_time)


def run_plan_refresh_body(now: datetime | None = None) -> list[UUID]:
    """Workspace scan → per-tenant loop → per-tenant ``refresh_business_plan_workflow`` child-start
    (VT-679 D1.1). Per-tenant try/except: one tenant's failure never halts the sweep (mirrors
    ``monthly_impact``'s own posture). Keyed on ``monthly_workflow_id(tenant, YYYY-MM)`` so a
    duplicate cron fire within the same month cannot double-start the SAME tenant's refresh
    (``DBOS.start_workflow`` no-ops on a known workflow id). Returns the tenant ids the refresh was
    STARTED for (the child workflow runs durably on its own — this does not wait for it)."""
    from dbos import DBOS, SetWorkflowID

    from orchestrator.business_plan.generator import refresh_business_plan_workflow

    now = now or datetime.now(timezone.utc)
    year_month = now.strftime("%Y-%m")
    started: list[UUID] = []
    for tenant_id in _scan_active_paid_or_trial_tenants():
        try:
            with SetWorkflowID(monthly_workflow_id(tenant_id, year_month)):
                DBOS.start_workflow(refresh_business_plan_workflow, str(tenant_id))
            started.append(tenant_id)
        except Exception:  # noqa: BLE001 — one tenant's failure must not halt the sweep
            logger.exception(
                "VT-679 plan_refresh: failed to start refresh for tenant=%s", tenant_id
            )
    return started


# D1.2 belt: a mis-set cron must never fire the daily initiative outside owner waking hours. The
# cron PLACEMENT (05:00 UTC = 10:30 IST) already guarantees this; this is a defensive runtime
# assert, not the primary control (scout confirmed no runtime quiet-hours helper exists anywhere
# in the codebase — building a general one is out of scope for this row).
_DAILY_INITIATIVE_IST_HOUR_MIN = 10
_DAILY_INITIATIVE_IST_HOUR_MAX = 19
_IST_OFFSET = timedelta(hours=5, minutes=30)


def _assert_ist_daytime(now: datetime) -> None:
    ist_hour = (now.astimezone(timezone.utc) + _IST_OFFSET).hour
    if not (_DAILY_INITIATIVE_IST_HOUR_MIN <= ist_hour < _DAILY_INITIATIVE_IST_HOUR_MAX):
        raise RuntimeError(
            "VT-679 daily_initiative_scheduled: fired outside the 10:00-19:00 IST belt "
            f"(IST hour={ist_hour}) — refusing to run (cron mis-configuration guard)"
        )


def daily_initiative_scheduled(scheduled_time: datetime, actual_time: datetime) -> None:
    """DBOS scheduled handler — fires daily 10:30 IST / 05:00 UTC (VT-679 D1.2). Behind
    ``TEAM_PROACTIVE_PLANNING`` (no-op fast when unset, before any DB scan or the IST belt check).
    NO LLM — the daily-initiative selection is fully deterministic (Pillar 1)."""
    if not _proactive_planning_enabled():
        return
    _assert_ist_daytime(actual_time)
    run_daily_initiative_body(now=actual_time)


def run_daily_initiative_body(now: datetime | None = None) -> list[dict[str, Any]]:
    """Workspace scan → per-tenant deterministic selection + dispatch (VT-679 D2/D3/D4, the real
    logic lives in ``business_plan.daily_initiative.dispatch_daily_initiative`` — this is just the
    trigger + sweep). Per-tenant try/except: one tenant's failure never halts the sweep. Returns
    the per-tenant dispatch results for tenants that actually got something dispatched — most days
    most tenants dispatch nothing (back-pressure skip / no plan / no accepted item left)."""
    from orchestrator.business_plan.daily_initiative import dispatch_daily_initiative

    now = now or datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    for tenant_id in _scan_active_paid_or_trial_tenants():
        try:
            result = dispatch_daily_initiative(tenant_id, now=now)
            if result is not None:
                results.append(result)
        except Exception:  # noqa: BLE001 — one tenant's failure must not halt the sweep
            logger.exception("VT-679 daily_initiative: dispatch failed tenant=%s", tenant_id)
    return results


# ---------------------------------------------------------------------------
# Deterministic workflow_id derivation (DBOS exactly-once short-circuit)
# ---------------------------------------------------------------------------


def weekly_workflow_id(tenant_id: UUID | str, iso_week: str) -> str:
    """``weekly:{tenant_id}:{iso_week}`` — VT-28 §1."""
    return f"weekly:{tenant_id}:{iso_week}"


def attribution_close_workflow_id(campaign_id: UUID | str) -> str:
    """``attribution_close:{campaign_id}`` — VT-28 §2."""
    return f"attribution_close:{campaign_id}"


def monthly_workflow_id(tenant_id: UUID | str, year_month: str) -> str:
    """``monthly:{tenant_id}:{YYYY-MM}`` — VT-28 §4."""
    return f"monthly:{tenant_id}:{year_month}"


# ---------------------------------------------------------------------------
# Registration — register-before-launch_dbos() pattern (mirrors VT-122)
# ---------------------------------------------------------------------------

_registered = False


def register_scheduled_triggers() -> None:
    """Apply the ``@DBOS.scheduled`` decoration to all scheduled trigger handlers.

    (Count grows over sprints — the authoritative tally is the register count test in
    test_scheduled_triggers.py, not a number here, to avoid doc-drift.)

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

    # VT-464 D3: register each handler as a @DBOS.workflow BEFORE applying
    # @DBOS.scheduled. In DBOS 2.x (we run 2.22), DBOS.scheduled() ONLY
    # registers a cron POLLER — it does NOT also register the function as a
    # workflow. When the poller fires, the scheduler enqueues the function by
    # its registered workflow name and recovery looks it up in
    # workflow_info_map; a function that was only scheduled (never decorated
    # @DBOS.workflow) has no dbos_function_name, so the fire raises
    # DBOSWorkflowFunctionNotFoundError ("not a registered workflow function")
    # — observed every ~30 min for approval_timeout_sweep_scheduled +
    # l2_approved_send_sweep_scheduled (their crons fire most often). The
    # daily/nightly handlers carried the same latent gap; wrapping ALL of them
    # is the correct, non-weakening fix (the approval-timeout sweep MUST run so
    # stale Pillar-7 approvals clear). Idempotent via the _registered guard.
    def _register_scheduled(cron: str, fn: Any) -> None:
        DBOS.scheduled(cron)(DBOS.workflow()(fn))

    _register_scheduled(WEEKLY_CADENCE_CRON, weekly_cadence_scheduled)
    _register_scheduled(ATTRIBUTION_CLOSE_CRON, attribution_close_scheduled)
    # VT-90 (VT-365): daily trial-lifecycle sweep — trial expiry to `lapsed`.
    _register_scheduled(TRIAL_EVALUATION_CRON, trial_evaluation_scheduled)
    _register_scheduled(MONTHLY_IMPACT_CRON, monthly_impact_scheduled)
    _register_scheduled(APPROVAL_TIMEOUT_SWEEP_CRON, approval_timeout_sweep_scheduled)
    # VT-683 P2b: owner-comms queue sweep (deliver + drop-stale + NULL-clock belt). EXTENDS
    # this surface (CL-240), never a parallel poller.
    _register_scheduled(OWNER_COMMS_SWEEP_CRON, owner_comms_sweep_scheduled)
    _register_scheduled(L3_CONSTRUCTION_CRON, l3_construction_scheduled)
    # VT-76 (CL-240): 7th handler — opt-out reconstitution sweep. EXTENDS this
    # surface, NOT a parallel poller. The cron + body live in privacy/reconstitution.
    from orchestrator.privacy.reconstitution import RECONSTITUTION_CRON

    _register_scheduled(RECONSTITUTION_CRON, reconstitution_sweep_scheduled)
    # VT-304: 8th handler — nightly audit-chain verify. EXTENDS the surface
    # (same register-before-launch posture; app_version shifts once, here).
    _register_scheduled(AUDIT_CHAIN_VERIFY_CRON, audit_chain_verify_scheduled)
    # VT-305: 9th handler — nightly PII-in-log sweep (VT-79 Detector-5).
    _register_scheduled(PII_LOG_SWEEP_CRON, pii_log_sweep_scheduled)
    # VT-307: 10th handler — nightly KG-events outbox-drain straggler sweep.
    _register_scheduled(KG_DRAIN_SWEEP_CRON, kg_drain_sweep_scheduled)
    # VT-311: 11th handler — nightly L2 episodic retention soft-delete sweep.
    _register_scheduled(L2_RETENTION_SWEEP_CRON, l2_retention_sweep_scheduled)
    # VT-354: waitlist 6-month retention purge — ENFORCES the DPDP pre-launch PII bound (was
    # runbook-manual). EXTENDS this surface (same register-before-launch posture).
    _register_scheduled(WAITLIST_RETENTION_PURGE_CRON, waitlist_retention_purge_scheduled)
    # VT-357: hourly SLA-breach sweep — 2nd Fazal alert on overdue open escalations (marker-gated).
    _register_scheduled(SLA_BREACH_SWEEP_CRON, sla_breach_sweep_scheduled)
    # VT-280: daily VTR digest — de-identified, app_vtr_role + the VT-281 views only.
    _register_scheduled(VTR_DIGEST_CRON, vtr_digest_scheduled)
    # VT-374: daily expired-override cancel sweep (F8 next-run pin expiry bound).
    # EXTENDS this surface (same register-before-launch posture), NOT a parallel poller.
    _register_scheduled(OVERRIDE_EXPIRY_SWEEP_CRON, override_expiry_sweep_scheduled)
    # VT-382: daily outbox-redaction backfill/backstop sweep (CL-437 ruling 3.3).
    # EXTENDS this surface (same register-before-launch posture), NOT a parallel poller.
    _register_scheduled(OUTBOX_REDACTION_SWEEP_CRON, outbox_redaction_sweep_scheduled)
    # VT-432: daily implicit-attribution sweep (VT-198 feedback tier-1). Runs at
    # 23:00 UTC / 04:30 IST — after attribution_close and the redaction/reconstitution
    # batch. Pure SQL, NO LLM, NO SEND. EXTENDS this surface, NOT a parallel poller.
    _register_scheduled(IMPLICIT_ATTRIBUTION_SWEEP_CRON, implicit_attribution_sweep_scheduled)
    # VT-418: the L2 owner-approve→send reconciler sweep — recovery-only (heals the
    # crash-between-commit-and-start residual where the runner's post-commit start_l2_send
    # never ran). Idempotent on the l2_send_{batch_id} workflow-id; the per-draft ledger
    # dedup makes a genuine re-drive no-double-send. EXTENDS this surface (same
    # register-before-launch posture), NOT a parallel poller.
    from orchestrator.agents.l2_send import (
        L2_APPROVED_SEND_SWEEP_CRON,
        l2_approved_send_sweep_scheduled,
    )

    _register_scheduled(L2_APPROVED_SEND_SWEEP_CRON, l2_approved_send_sweep_scheduled)
    # VT-439: daily Razorpay orphan-DETECT backstop (VT-352 F7). DETECT-ONLY — no
    # cancel, no charge, no send. Runs at 01:00 UTC / 06:30 IST (off-peak billing).
    _register_scheduled(
        RECONCILE_SUBSCRIPTION_ORPHANS_CRON, reconcile_subscription_orphans_scheduled
    )
    # VT-440: daily dead-letter retry backstop (VT-352 F7). DETECT/ALERT-ONLY — counts
    # pending parse-dropped charge events + alerts Fazal; NO replay/charge/send/write.
    # The actual replay is operator-driven (needs a corrected payload); exactly-once is
    # guaranteed by the razorpay_ingress event_id dedup keystone. 22:30 UTC / 04:00 IST.
    _register_scheduled(DEAD_LETTER_RETRY_SWEEP_CRON, dead_letter_retry_sweep_scheduled)
    # VT-560: the boot-only reapers as STEADY-STATE scheduled sweeps — they previously ran
    # ONLY at boot, so on a long-lived process the VT-557 retry ladder never progressed.
    # EXTENDS this surface (same register-before-launch posture), NOT parallel pollers.
    # NO LLM (pure SQL reaper). The VT-552 silent-terminal detector is deliberately NOT
    # here — see the comment above STALLED_TASK_SWEEP_CRON (no final_outcome writer yet ⇒
    # scheduling it would storm an incident/alert per completed run).
    _register_scheduled(STALLED_TASK_SWEEP_CRON, stalled_task_sweep_scheduled)
    _register_scheduled(ORPHAN_RUN_REAPER_CRON, orphan_run_reaper_scheduled)
    # VT-620: hourly GC of leaked convo-harness test tenants (offset 15m from the orphan reaper).
    # Pure SQL FK-safe delete, NO LLM, STRICT convo-harness-% scope. EXTENDS this surface.
    _register_scheduled(TEST_TENANT_REAPER_CRON, test_tenant_reaper_scheduled)
    # VT-679 (§7A proactive planning): monthly plan-refresh + daily initiative pick. Both
    # unconditionally REGISTERED here (cron infrastructure is always-on); TEAM_PROACTIVE_PLANNING
    # gates the HANDLER BODY (no-op fast when unset) — EXTENDS this surface, NOT parallel pollers.
    _register_scheduled(PLAN_REFRESH_CRON, plan_refresh_scheduled)
    _register_scheduled(DAILY_INITIATIVE_CRON, daily_initiative_scheduled)
    _registered = True


__all__ = [
    "APPROVAL_TIMED_OUT_EVENT",
    "APPROVAL_TIMEOUT_SWEEP_CRON",
    "ATTRIBUTION_CLOSED_EVENT",
    "ATTRIBUTION_CLOSE_CRON",
    "ATTRIBUTION_CLOSE_SHELL_EVENT",
    "DEAD_LETTER_RETRY_SWEEP_CRON",
    "IMPLICIT_ATTRIBUTION_SWEEP_CRON",
    "RECONCILE_SUBSCRIPTION_ORPHANS_CRON",
    "L3_CONSTRUCTION_CRON",
    "MONTHLY_IMPACT_CRON",
    "MONTHLY_IMPACT_SHELL_EVENT",
    "MONTHLY_IMPACT_STARTED_EVENT",
    "ORPHAN_RUN_REAPER_CRON",
    "OUTBOX_REDACTION_SWEEP_CRON",
    "OVERRIDE_EXPIRY_SWEEP_CRON",
    "SHELL_STATUS",
    "STALLED_TASK_SWEEP_CRON",
    "TEST_TENANT_REAPER_CRON",
    "WEEKLY_CADENCE_CRON",
    "WEEKLY_CADENCE_EVENT",
    "approval_timeout_sweep_scheduled",
    "attribution_close_scheduled",
    "attribution_close_workflow_id",
    "AUDIT_CHAIN_VERIFY_CRON",
    "KG_DRAIN_SWEEP_CRON",
    "L2_RETENTION_SWEEP_CRON",
    "PII_LOG_SWEEP_CRON",
    "audit_chain_verify_scheduled",
    "dead_letter_retry_sweep_scheduled",
    "implicit_attribution_sweep_scheduled",
    "reconcile_subscription_orphans_scheduled",
    "kg_drain_sweep_scheduled",
    "l2_retention_sweep_scheduled",
    "pii_log_sweep_scheduled",
    "monthly_impact_scheduled",
    "monthly_workflow_id",
    "orphan_run_reaper_scheduled",
    "outbox_redaction_sweep_scheduled",
    "override_expiry_sweep_scheduled",
    "reconstitution_sweep_scheduled",
    "register_scheduled_triggers",
    "stalled_task_sweep_scheduled",
    "test_tenant_reaper_scheduled",
    "run_approval_timeout_sweep_body",
    "run_attribution_close_body",
    "run_monthly_impact_body",
    "run_weekly_cadence_body",
    "TEAM_PROACTIVE_PLANNING_FLAG",
    "PLAN_REFRESH_CRON",
    "DAILY_INITIATIVE_CRON",
    "plan_refresh_scheduled",
    "run_plan_refresh_body",
    "daily_initiative_scheduled",
    "run_daily_initiative_body",
    "weekly_cadence_scheduled",
    "weekly_workflow_id",
]
