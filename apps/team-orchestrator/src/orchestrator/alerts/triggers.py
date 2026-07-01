"""VT-202 — trigger detection.

Reads recent pipeline_runs / pipeline_steps / privacy_audit_log;
compares against ``tenant_alert_baselines``; returns a list of
Trigger objects describing what should fire.

Trigger kinds (8 per VT-202 brief):
- hard_limit             critical: status='aborted_hard_limit' lands
- escalation             critical: status='escalated' lands
- error_envelope         critical: any error_envelope step_kind
- cost_anomaly           warning : single-run cost > 2× p95
- latency_anomaly        warning : single-run latency > 2× p95
- privacy_audit_event    critical: any new privacy_audit_log row
- volume_spike           warning : last-hour volume > 3× baseline
- outbound_failure       critical: Twilio send failure surfaced

Slow triggers (cost / latency / volume / privacy / error) are
swept by the 5-min DBOS scheduler. Critical triggers fire from the
runner.py write-hook for ≤60s SLA per AC-1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)


TriggerKind = Literal[
    "hard_limit",
    "escalation",
    "error_envelope",
    "cost_anomaly",
    "latency_anomaly",
    "privacy_audit_event",
    "volume_spike",
    "outbound_failure",
    # VT-79 breach detectors (Phase-1 slice).
    "tenant_isolation_breach",  # Detector-1 (P0 — confirmed cross-tenant exposure)
    "dsr_rate_anomaly",  # Detector-3 (DSR request-rate over threshold)
    "pii_in_log",  # Detector-5 (unredacted PII found in pipeline_steps payloads)
    # VT-76 opt-out reconstitution SLA (fired by the daily reconstitution sweep).
    "reconstitution_sla_breach",  # P0 — opted-out customer un-reconstituted past 8d
    # VT-307 KG-events outbox-drain straggler (nightly drain sweep, warning).
    "kg_drain_straggler",  # an outbox event the immediate + nightly drain failed to project
    # VT-529 (B6): a manager_task stranded active with no runnable step, reaped to 'blocked'.
    "orphaned_task",  # the B2 stalled-task reaper flipped a task → blocked
]

Severity = Literal["critical", "warning"]

# Severity per trigger kind (Cowork brief locks).
_SEVERITY_BY_KIND: dict[TriggerKind, Severity] = {
    "hard_limit": "critical",
    "escalation": "critical",
    "error_envelope": "critical",
    "privacy_audit_event": "critical",
    "outbound_failure": "critical",
    "cost_anomaly": "warning",
    "latency_anomaly": "warning",
    "volume_spike": "warning",
    # VT-79 breach detectors.
    "tenant_isolation_breach": "critical",
    "dsr_rate_anomaly": "warning",
    "pii_in_log": "critical",
    # VT-76 opt-out reconstitution SLA.
    "reconstitution_sla_breach": "critical",
    # VT-307 KG-drain straggler — reliability backstop signal (batched digest).
    "kg_drain_straggler": "warning",
    # VT-529 — a stalled/orphaned task needs attention but isn't a customer-facing critical.
    "orphaned_task": "warning",
}

# VT-79 Detector-3: DSR request-rate threshold (Phase-1 fixed value; cohort
# baselines need real traffic — flagged for tuning, gate-live posture).
_DSR_RATE_WINDOW_HOURS = 24
_DSR_RATE_THRESHOLD = 10  # DSR tickets per tenant per window before alerting


@dataclass(frozen=True)
class Trigger:
    """One alert-worthy event ready to be persisted + dispatched."""

    tenant_id: UUID
    trigger_kind: TriggerKind
    severity: Severity
    message_text: str
    run_id: UUID | None = None
    payload: dict[str, Any] | None = None


def severity_for(kind: TriggerKind) -> Severity:
    """Public severity lookup."""
    return _SEVERITY_BY_KIND[kind]


def _make_trigger(
    tenant_id: UUID,
    kind: TriggerKind,
    message: str,
    *,
    run_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> Trigger:
    return Trigger(
        tenant_id=tenant_id,
        trigger_kind=kind,
        severity=_SEVERITY_BY_KIND[kind],
        message_text=message,
        run_id=run_id,
        payload=payload or {},
    )


# VT-476 dev_send_guard mock-SID marker. A mocked dev send returns a SID with
# this prefix instead of a real Twilio ``SM…`` SID (utils/dev_send_guard._MockMessage).
_MOCK_SID_PREFIX = "MKDEV"
# The two outbound-send ledger tables (mig 049) that carry the resolved message_sid.
_SEND_LEDGER_TABLES = ("send_idempotency_keys", "campaign_messages")
_VOLUME_WINDOW = "1 hour"


def _volume_is_mock_only(tenant_id: UUID) -> bool:
    """VT-489 (b): True when the tenant's outbound sends in the volume window were
    ALL mocked (VT-476 ``MKDEV…`` SIDs) — i.e. ZERO real sends landed.

    A mocked send is not a real send (no customer was messaged), so a run-volume
    spike whose entire outbound activity was mocked is a dev/test artifact, not a
    real-send volume alarm. The send ledger (mig 049) carries ``message_sid`` +
    ``send_status``; we count REAL successful sends (``send_status='sent'`` /
    ``'template_sent'`` AND a non-MKDEV SID) in the same window as the run count.

    Returns True (suppress) ONLY when there is at least one mocked send AND zero
    real sends in the window. FAIL-SAFE: returns False (do NOT suppress → alert)
    when uncertain — no mocked sends found, or any read error — so a real prod
    spike is never silenced by this guard.
    """
    pool = get_pool()
    real_sends = 0
    mock_sends = 0
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            for table in _SEND_LEDGER_TABLES:
                cur.execute(
                    f"""
                    SELECT
                        COUNT(*) FILTER (
                            WHERE message_sid IS NOT NULL
                              AND message_sid NOT LIKE %s
                              AND send_status IN ('sent', 'template_sent')
                        ) AS real_n,
                        COUNT(*) FILTER (
                            WHERE message_sid LIKE %s
                        ) AS mock_n
                    FROM {table}
                    WHERE tenant_id = %s
                      AND created_at > now() - interval '{_VOLUME_WINDOW}'
                    """,  # noqa: S608 — table name from a fixed module constant, never user input
                    (f"{_MOCK_SID_PREFIX}%", f"{_MOCK_SID_PREFIX}%", str(tenant_id)),
                )
                r = cur.fetchone()
                rd = (dict(r) if not isinstance(r, dict) else r) if r is not None else {}
                real_sends += int(rd.get("real_n") or 0)
                mock_sends += int(rd.get("mock_n") or 0)
    except Exception:  # noqa: BLE001 — fail-safe: an unreadable ledger must NOT suppress a real spike
        logger.warning(
            "VT-489: mock-only volume check failed for tenant %s; NOT suppressing "
            "(fail-safe — prefer to alert)", tenant_id, exc_info=True,
        )
        return False
    # Suppress ONLY when the window had mocked send(s) and not a single real send.
    return mock_sends > 0 and real_sends == 0


def detect_critical_for_run(run_id: UUID) -> list[Trigger]:
    """Write-hook entry — examine a single just-closed run for critical triggers.

    Called from runner.py on terminal-status transitions (or its
    equivalent in dispatch.py). Reads pipeline_runs.status + recent
    pipeline_steps. Returns 0..N Triggers (most runs return 0).
    """
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, status, total_cost_paise "
            "FROM pipeline_runs WHERE id = %s",
            (str(run_id),),
        )
        raw = cur.fetchone()
    if raw is None:
        return []
    row = dict(raw) if not isinstance(raw, dict) else raw
    tenant_id = UUID(str(row["tenant_id"]))
    status = row["status"]
    triggers: list[Trigger] = []
    if status == "aborted_hard_limit":
        triggers.append(_make_trigger(
            tenant_id, "hard_limit",
            f"Run {run_id} aborted on hard-limit",
            run_id=run_id,
            payload={"status": status, "total_cost_paise": row.get("total_cost_paise")},
        ))
    elif status == "escalated":
        triggers.append(_make_trigger(
            tenant_id, "escalation",
            f"Run {run_id} escalated to operator",
            run_id=run_id,
            payload={"status": status},
        ))
    return triggers


def detect_slow_triggers(tenant_id: UUID) -> list[Trigger]:
    """Sweep entry — examine baselines vs recent observations.

    Called by the 5-min DBOS scheduler. Returns the slow-trigger set
    (cost / latency / volume / privacy / error_envelope).
    """
    pool = get_pool()
    triggers: list[Trigger] = []

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cost_p95_paise, latency_p95_ms, volume_per_hour "
            "FROM tenant_alert_baselines WHERE tenant_id = %s",
            (str(tenant_id),),
        )
        baseline = cur.fetchone()
    # A missing baseline must NOT skip the whole sweep — the VT-79 breach
    # detectors (tenant-isolation P0, DSR-rate) don't depend on baselines. The
    # baseline-dependent checks (cost/latency/volume) already guard on `base`
    # being populated, so an empty base simply no-ops them.
    base = (
        (dict(baseline) if not isinstance(baseline, dict) else baseline)
        if baseline is not None
        else {}
    )

    # Cost + latency anomaly — sweep last 5-min terminal runs.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, total_cost_paise,
                EXTRACT(EPOCH FROM (ended_at - started_at)) * 1000 AS latency_ms
            FROM pipeline_runs
            WHERE tenant_id = %s
              AND ended_at IS NOT NULL
              AND ended_at > now() - interval '5 minutes'
            """,
            (str(tenant_id),),
        )
        recent = cur.fetchall()
    for r in recent:
        rd = dict(r) if not isinstance(r, dict) else r
        run_id = UUID(str(rd["id"]))
        cost = rd.get("total_cost_paise") or 0
        latency = int(rd.get("latency_ms") or 0)
        p95_cost = base.get("cost_p95_paise") or 0
        p95_lat = base.get("latency_p95_ms") or 0
        if p95_cost and cost > 2 * p95_cost:
            triggers.append(_make_trigger(
                tenant_id, "cost_anomaly",
                f"Run {run_id} cost {cost}p exceeds 2× p95 ({p95_cost}p)",
                run_id=run_id,
                payload={"cost_paise": cost, "baseline_p95": p95_cost},
            ))
        if p95_lat and latency > 2 * p95_lat:
            triggers.append(_make_trigger(
                tenant_id, "latency_anomaly",
                f"Run {run_id} latency {latency}ms exceeds 2× p95 ({p95_lat}ms)",
                run_id=run_id,
                payload={"latency_ms": latency, "baseline_p95": p95_lat},
            ))

    # Volume spike — last-hour count vs baseline.
    #
    # VT-489 (b): the volume_spike metric is a REAL-SEND volume alarm. A dev
    # mocked send (VT-476 dev_send_guard → ``MKDEV…`` SID) is NOT a real send and
    # must not trip it. The metric counts inbound ``pipeline_runs`` (one per
    # webhook), which are decoupled from the outbound send ledger — so a mocked
    # send never increments the run-count directly. But a re-drive burst inflates
    # the inbound-run count while its OUTBOUND sends are all mocked (the dev guard
    # mocks every non-allowlisted dev send). So we gate on real-send presence:
    # if the tenant produced ZERO real (non-``MKDEV``) outbound sends in the
    # window, the run-volume is a dev/test artifact (mocked-only) — do NOT fire.
    # FAIL-SAFE: if the ledger read errors, we DON'T suppress (prefer to alert).
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM pipeline_runs "
            "WHERE tenant_id = %s AND started_at > now() - interval '1 hour'",
            (str(tenant_id),),
        )
        vraw = cur.fetchone()
    if vraw is not None:
        vdict = dict(vraw) if not isinstance(vraw, dict) else vraw
        observed = int(vdict.get("n") or 0)
        baseline_vol = base.get("volume_per_hour") or 0
        if baseline_vol and observed > 3 * baseline_vol:
            if _volume_is_mock_only(tenant_id):
                logger.info(
                    "VT-489: volume_spike for tenant %s suppressed — all outbound "
                    "sends in the window were mocked (MKDEV); not a real-send spike",
                    tenant_id,
                )
            else:
                triggers.append(_make_trigger(
                    tenant_id, "volume_spike",
                    f"Tenant {tenant_id} hourly volume {observed} exceeds 3× baseline ({baseline_vol})",
                    payload={"observed": observed, "baseline": baseline_vol},
                ))

    # Error envelope sweep — recent pipeline_steps with the canonical
    # 'error' step_kind (the VT-179 step_kind for error envelopes —
    # see STEP_KIND_REGISTRY in observability/envelopes/__init__.py).
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, step_name FROM pipeline_steps
            WHERE tenant_id = %s
              AND step_kind = 'error'
              AND started_at > now() - interval '5 minutes'
            ORDER BY started_at DESC LIMIT 10
            """,
            (str(tenant_id),),
        )
        errors = cur.fetchall()
    for r in errors:
        rd = dict(r) if not isinstance(r, dict) else r
        run_id = UUID(str(rd["run_id"]))
        triggers.append(_make_trigger(
            tenant_id, "error_envelope",
            f"Error envelope on run {run_id}: {rd.get('step_name') or 'unknown'}",
            run_id=run_id,
            payload={"step_name": rd.get("step_name")},
        ))

    # VT-79 Detector-1 — tenant-isolation breach (P0). The RLS guard
    # (_tenant_guard) emits a 'tenant_isolation_breach' pipeline_step on any
    # cross-tenant leak; surface it as a critical trigger via the VT-202 path.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id FROM pipeline_steps
            WHERE tenant_id = %s
              AND step_kind = 'tenant_isolation_breach'
              AND started_at > now() - interval '5 minutes'
            ORDER BY started_at DESC LIMIT 10
            """,
            (str(tenant_id),),
        )
        breaches = cur.fetchall()
    for r in breaches:
        rd = dict(r) if not isinstance(r, dict) else r
        run_id = UUID(str(rd["run_id"]))
        triggers.append(_make_trigger(
            tenant_id, "tenant_isolation_breach",
            f"P0 tenant-isolation breach on run {run_id}",
            run_id=run_id,
            payload={"severity_class": "P0"},
        ))

    # VT-79 Detector-3 — DSR request-rate anomaly (fixed Phase-1 threshold).
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM dsr_tickets "
            "WHERE tenant_id = %s "
            f"  AND acknowledged_at > now() - interval '{_DSR_RATE_WINDOW_HOURS} hours'",
            (str(tenant_id),),
        )
        draw = cur.fetchone()
    if draw is not None:
        dcount = int((dict(draw) if not isinstance(draw, dict) else draw).get("n") or 0)
        if dcount > _DSR_RATE_THRESHOLD:
            triggers.append(_make_trigger(
                tenant_id, "dsr_rate_anomaly",
                f"DSR request rate {dcount} in {_DSR_RATE_WINDOW_HOURS}h "
                f"exceeds threshold ({_DSR_RATE_THRESHOLD})",
                payload={"count": dcount, "threshold": _DSR_RATE_THRESHOLD},
            ))

    return triggers


def detect_pii_in_logs(tenant_id: UUID, *, lookback_hours: int = 24) -> list[Trigger]:
    """VT-79 Detector-5 — scan recent pipeline_steps payloads for unredacted PII.

    CL-390 regression catcher: bodies/phones must be redacted at the persistence
    boundary (VT-144). Any payload that STILL matches a PII pattern is a leak →
    critical. Reuses the alert pii_scrub patterns (one PII-pattern source).

    VT-379: the scan was envelope-only — ``pipeline_steps.error`` (jsonb, the
    three direct-INSERT writers: error_router / self_evaluate_gate / collapse)
    plus ``decision_rationale`` (text) and ``tool_calls`` (jsonb) were never
    swept, so an exception string carrying a phone or a customer name landed
    in an UNswept column. All four free-text-bearing columns are now scanned
    in the same pass (find_pii operates on the stringified blob, so jsonb /
    text are uniformly co-scannable). ``input_envelope`` / ``output_envelope``
    remain in the blob — the redacting writer covers them, but Detector-5 is
    the backstop, not the gate.

    The detection logic ships now (+ canary); the nightly DBOS.scheduled
    registration is a fast-follow (VT-305) — same app_version posture as VT-304.
    """
    from orchestrator.alerts.pii_scrub import find_pii

    pool = get_pool()
    triggers: list[Trigger] = []
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, run_id, input_envelope, output_envelope,
                   error, decision_rationale, tool_calls
            FROM pipeline_steps
            WHERE tenant_id = %s
              AND started_at > now() - make_interval(hours => %s)
            ORDER BY started_at DESC LIMIT 1000
            """,
            (str(tenant_id), lookback_hours),
        )
        rows = cur.fetchall()
    for r in rows:
        rd = dict(r) if not isinstance(r, dict) else r
        # VT-379: scan envelopes AND the three previously-unswept free-text
        # columns (error / decision_rationale / tool_calls). NULLs stringify
        # to "None" which find_pii treats as clean, so unset columns no-op.
        blob = " ".join(
            str(rd.get(col))
            for col in (
                "input_envelope",
                "output_envelope",
                "error",
                "decision_rationale",
                "tool_calls",
            )
        )
        matches = find_pii(blob)
        if matches:
            triggers.append(_make_trigger(
                tenant_id, "pii_in_log",
                f"Unredacted PII in pipeline_step {rd.get('id')} "
                f"(kinds: {sorted(set(matches))})",
                run_id=UUID(str(rd["run_id"])) if rd.get("run_id") else None,
                payload={"step_id": str(rd.get("id")), "pii_kinds": sorted(set(matches))},
            ))
    return triggers


def all_active_tenant_ids() -> list[UUID]:
    """Tenants with at least one terminal pipeline_run in last 30 days."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT tenant_id FROM pipeline_runs "
            "WHERE started_at > now() - interval '30 days'"
        )
        rows = cur.fetchall()
    out: list[UUID] = []
    for r in rows:
        rd = dict(r) if not isinstance(r, dict) else r
        out.append(UUID(str(rd["tenant_id"])))
    return out


__all__ = [
    "Severity",
    "Trigger",
    "TriggerKind",
    "all_active_tenant_ids",
    "detect_critical_for_run",
    "detect_pii_in_logs",
    "detect_slow_triggers",
    "severity_for",
]
