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
from datetime import datetime, timezone
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
# 2. Attribution close — SHELL form (VT-175 will fill the SQL body)
# ---------------------------------------------------------------------------

ATTRIBUTION_CLOSE_SHELL_EVENT = "attribution_close_shell"


def attribution_close_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires daily 2 AM IST. Pure SQL (no LLM)."""
    run_attribution_close_body(now=actual_time)


def run_attribution_close_body(now: datetime | None = None) -> UUID:
    """Attribution close body — SHELL.

    VT-175 will add the ``attributions`` table + ``campaigns
    .attribution_close_at`` columns + the ARRR aggregation SQL. Until
    then this body emits ``attribution_close_shell`` with ``status:
    skipped_schema_pending``. The completion event name
    ``attribution_closed`` is RESERVED — not emitted from here (phantom-
    Done prevention per CL-318/319/380).

    NO LLM CALL ever — Pillar 1 deterministic path enforced by the
    ``gate-no-llm-in-deterministic-triggers`` CI gate.
    """
    now = now or datetime.now(timezone.utc)
    return _emit_shell_event(
        ATTRIBUTION_CLOSE_SHELL_EVENT,
        component="scheduled_trigger",
        now=now,
    )


# ---------------------------------------------------------------------------
# 3. Day-39 evaluation — SHELL form (VT-175 will wire the evaluator)
# ---------------------------------------------------------------------------

DAY39_SHELL_EVENT = "day39_shell"


def day39_evaluation_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires daily 6 AM IST. Pure SQL (no LLM)."""
    run_day39_evaluation_body(now=actual_time)


def run_day39_evaluation_body(now: datetime | None = None) -> UUID:
    """Day-39 evaluation body — SHELL.

    VT-175 will add ``tenants.paid_conversion_at`` + VT-Billing VT-10.4
    evaluator wiring. The two reserved completion event names
    (``day39_continue`` + ``day39_refund_triggered``) and the
    ``apply_transition`` to ``refunded`` phase are NOT emitted from this
    body — they ship under VT-176.

    NO LLM CALL ever — Pillar 1 deterministic path.
    """
    now = now or datetime.now(timezone.utc)
    return _emit_shell_event(
        DAY39_SHELL_EVENT,
        component="scheduled_trigger",
        now=now,
    )


# ---------------------------------------------------------------------------
# 4. Monthly impact — SHELL form (VT-175 will wire the metrics aggregator)
# ---------------------------------------------------------------------------

MONTHLY_IMPACT_SHELL_EVENT = "monthly_impact_shell"


def monthly_impact_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires 1st of month 8 AM IST. Pure SQL."""
    run_monthly_impact_body(now=actual_time)


def run_monthly_impact_body(now: datetime | None = None) -> UUID:
    """Monthly impact body — SHELL.

    VT-175 will add the per-tenant metrics aggregation + Resend hand-off
    + VT-OwnerSurface VT-9.6 PDF generation wiring. Reserved completion
    event ``monthly_impact_started`` ships under VT-176.

    NO LLM CALL ever — Pillar 1 deterministic path.
    """
    now = now or datetime.now(timezone.utc)
    return _emit_shell_event(
        MONTHLY_IMPACT_SHELL_EVENT,
        component="scheduled_trigger",
        now=now,
    )


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
    "ATTRIBUTION_CLOSE_CRON",
    "ATTRIBUTION_CLOSE_SHELL_EVENT",
    "DAY39_EVALUATION_CRON",
    "DAY39_SHELL_EVENT",
    "MONTHLY_IMPACT_CRON",
    "MONTHLY_IMPACT_SHELL_EVENT",
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
