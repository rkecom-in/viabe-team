"""VT-202 — DBOS scheduled workflows.

Two scheduled bodies:

1. ``alerts_sweep_body`` @ ``*/5 * * * *`` — every 5 min, recompute
   baselines + scan for slow triggers + dispatch + retry pending sends.

2. ``daily_digest_body`` @ ``30 3 * * *`` (03:30 UTC = 09:00 IST) —
   email Fazal a summary of the previous day's signals.

Register-before-launch mirrors ``dbos_purge.register_purge_scheduler``
and ``integrations.scheduler.register_ingestion_scheduler``. Decorator
order is ``DBOS.workflow()`` BEFORE ``DBOS.scheduled(...)`` per the
VT-200 fix.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from dbos import DBOS

from orchestrator.alerts.baselines import recompute_tenant_baselines
from orchestrator.alerts.clients import send_resend_email
from orchestrator.alerts.dispatch import dispatch_alert, retry_pending_sends
from orchestrator.alerts.pii_scrub import scrub_pii
from orchestrator.alerts.triggers import (
    all_active_tenant_ids,
    detect_slow_triggers,
)
from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)

_SWEEP_CRON = "*/5 * * * *"
_DIGEST_CRON = "30 3 * * *"  # 03:30 UTC = 09:00 IST


def alerts_sweep_body(scheduled_time: datetime, actual_time: datetime) -> None:
    """Plain scheduled-function body. ``register_alert_scheduler`` decorates."""
    try:
        recompute_tenant_baselines()
        for tenant_id in all_active_tenant_ids():
            triggers = detect_slow_triggers(tenant_id)
            for trigger in triggers:
                dispatch_alert(trigger)
        retried = retry_pending_sends()
        if retried:
            logger.info("alerts: retried %d pending send(s) on tick %s",
                        retried, actual_time.isoformat())
    except Exception:  # noqa: BLE001 — scheduler must keep firing
        logger.exception("alerts sweep raised; cadence continues")


def _build_digest_html() -> tuple[str, str] | None:
    """Build (subject, html) for yesterday's digest. None if no data."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT trigger_kind, COUNT(*) AS n
            FROM tenant_alerts
            WHERE fired_at >= now() - interval '24 hours'
            GROUP BY trigger_kind
            ORDER BY n DESC
            """
        )
        kind_rows = cur.fetchall()
        # Per Cowork lock: top-5 expensive runs must NOT include
        # error.payload — projection is (run_id, business_name,
        # total_cost_paise, started_at) only.
        cur.execute(
            """
            SELECT pr.id AS run_id,
                COALESCE(t.business_name, '') AS business_name,
                pr.total_cost_paise,
                pr.started_at
            FROM pipeline_runs pr
            LEFT JOIN tenants t ON t.id = pr.tenant_id
            WHERE pr.started_at >= now() - interval '24 hours'
              AND pr.total_cost_paise IS NOT NULL
            ORDER BY pr.total_cost_paise DESC
            LIMIT 5
            """
        )
        top_rows = cur.fetchall()
    if not kind_rows and not top_rows:
        return None

    def _d(r: object) -> dict:
        return dict(r) if not isinstance(r, dict) else r

    parts: list[str] = [
        '<h2 style="font-family:sans-serif;">Viabe daily digest</h2>',
        '<h3>Last-24h alert counts</h3><ul>',
    ]
    if not kind_rows:
        parts.append('<li>(none)</li>')
    for r in kind_rows:
        rd = _d(r)
        parts.append(f"<li>{rd['trigger_kind']}: {rd['n']}</li>")
    parts.append('</ul>')
    parts.append('<h3>Top-5 expensive runs (last 24h)</h3><ol>')
    if not top_rows:
        parts.append('<li>(none)</li>')
    for r in top_rows:
        rd = _d(r)
        run_id = rd['run_id']
        biz = rd.get('business_name') or '—'
        cost = rd.get('total_cost_paise') or 0
        started = rd.get('started_at')
        parts.append(
            f"<li>run {run_id}: {biz}, {cost} paise, "
            f"started {started.isoformat() if started else '—'}</li>"
        )
    parts.append('</ol>')
    html = "\n".join(parts)
    subject = scrub_pii(f"Viabe daily digest — {datetime.now().date().isoformat()}")
    return subject, html


def daily_digest_body(scheduled_time: datetime, actual_time: datetime) -> None:
    """09:00 IST daily summary email."""
    try:
        result = _build_digest_html()
        if result is None:
            logger.info("digest: nothing to send")
            return
        subject, html = result
        api_key = os.environ.get("RESEND_API_KEY", "")
        from_addr = os.environ.get("RESEND_FROM_EMAIL", "")
        to_addr = os.environ.get("RESEND_TO_EMAIL", "")
        if not (api_key and from_addr and to_addr):
            logger.warning("digest: missing RESEND env; skipping send")
            return
        try:
            asyncio.run(send_resend_email(api_key, from_addr, to_addr, subject, html))
        except RuntimeError:
            loop = asyncio.get_event_loop()
            loop.create_task(send_resend_email(api_key, from_addr, to_addr, subject, html))
    except Exception:  # noqa: BLE001
        logger.exception("daily digest raised; cadence continues")


def register_alert_scheduler() -> None:
    """Apply @DBOS.workflow + @DBOS.scheduled to both bodies.

    Mirrors VT-210 register_ingestion_scheduler. Called from main.py
    lifespan BEFORE launch_dbos.
    """
    DBOS.workflow()(alerts_sweep_body)
    DBOS.scheduled(_SWEEP_CRON)(alerts_sweep_body)
    DBOS.workflow()(daily_digest_body)
    DBOS.scheduled(_DIGEST_CRON)(daily_digest_body)


__all__ = [
    "alerts_sweep_body",
    "daily_digest_body",
    "register_alert_scheduler",
]
