"""VT-202 — write-then-dispatch alert pipeline.

Insert ``tenant_alerts`` row FIRST, then fire Telegram + email.
HTTP failure → row stays in DB with NULL sent timestamps; the 5-min
scheduler retries on its next tick. Idempotent: a non-NULL
sent timestamp blocks re-send.

Channel routing (Cowork-locked):
- Canary tenant (``TEAM_CANARY_TENANT_IDS`` env) → DEV bot only;
  never email; never OPS bot
- Real ops traffic → critical = OPS bot + email immediately;
  warning = batched into hourly digest unless 3+ in 5 min (force-immediate)

Dedup: same ``(tenant_id, trigger_kind)`` firing within 5-min sliding
window → only persist + dispatch once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

from orchestrator.alerts.clients import send_resend_email, send_telegram
from orchestrator.alerts.pii_scrub import scrub_pii
from orchestrator.alerts.triggers import Trigger
from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)

_DEDUP_WINDOW_S = 5 * 60
_WARNING_BURST_THRESHOLD = 3  # 3+ warnings in 5 min → force-immediate
_WARNING_BURST_WINDOW_S = 5 * 60


def _canary_tenant_ids() -> frozenset[str]:
    """Parse ``TEAM_CANARY_TENANT_IDS`` env. Empty when unset."""
    raw = os.environ.get("TEAM_CANARY_TENANT_IDS", "")
    if not raw.strip():
        return frozenset()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return frozenset(parts)


def is_canary_tenant(tenant_id: UUID) -> bool:
    """Per Cowork CORRECTION-1: env-var whitelist, NOT name match."""
    return str(tenant_id) in _canary_tenant_ids()


def _dedup_key(trigger: Trigger) -> str:
    return f"{trigger.tenant_id}:{trigger.trigger_kind}"


def _dedup_skip(trigger: Trigger) -> bool:
    """True if same (tenant_id, trigger_kind) fired in last 5 min."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM tenant_alerts "
            "WHERE dedup_key = %s "
            "  AND fired_at > now() - interval '5 minutes' "
            "LIMIT 1",
            (_dedup_key(trigger),),
        )
        row = cur.fetchone()
    return row is not None


def _persist_alert(trigger: Trigger, scrubbed_text: str) -> UUID:
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tenant_alerts (
                tenant_id, trigger_kind, severity, dedup_key,
                message_text, run_id, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (
                str(trigger.tenant_id), trigger.trigger_kind, trigger.severity,
                _dedup_key(trigger), scrubbed_text,
                str(trigger.run_id) if trigger.run_id else None,
                json.dumps(trigger.payload or {}),
            ),
        )
        raw = cur.fetchone()
    rd = dict(raw) if not isinstance(raw, dict) else raw
    return UUID(str(rd["id"]))


def _mark_telegram_sent(alert_id: UUID) -> None:
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(
            "UPDATE tenant_alerts SET telegram_sent_at = now() WHERE id = %s",
            (str(alert_id),),
        )


def _mark_email_sent(alert_id: UUID) -> None:
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(
            "UPDATE tenant_alerts SET email_sent_at = now() WHERE id = %s",
            (str(alert_id),),
        )


def _warning_burst_force_immediate(trigger: Trigger) -> bool:
    """3+ warnings within 5 min = force-immediate path even for warnings."""
    if trigger.severity != "warning":
        return False
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM tenant_alerts "
            "WHERE tenant_id = %s AND severity = 'warning' "
            "  AND fired_at > now() - interval '5 minutes'",
            (str(trigger.tenant_id),),
        )
        raw = cur.fetchone()
    rd = dict(raw) if not isinstance(raw, dict) else raw
    return int(rd.get("n") or 0) >= _WARNING_BURST_THRESHOLD


def _format_alert(trigger: Trigger) -> tuple[str, str]:
    """Return (telegram_text, email_subject_seed). Both go through scrub."""
    header = f"[{trigger.severity.upper()}] {trigger.trigger_kind}"
    body = trigger.message_text
    if trigger.run_id:
        body = f"{body}\nrun: {trigger.run_id}"
    body = f"{body}\ntenant: {trigger.tenant_id}"
    text = f"{header}\n{body}"
    subject = f"Viabe alert: {trigger.trigger_kind}"
    return scrub_pii(text), scrub_pii(subject)


async def _dispatch_telegram(alert_id: UUID, scrubbed_text: str, is_canary: bool) -> None:
    """Route to DEV bot for canary tenants; OPS bot otherwise."""
    if is_canary:
        bot_token = os.environ.get("TELEGRAM_DEV_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_DEV_CHAT_ID", "")
    else:
        bot_token = os.environ.get("TELEGRAM_OPS_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_OPS_CHAT_ID", "")
    ok = await send_telegram(bot_token, chat_id, scrubbed_text)
    if ok:
        _mark_telegram_sent(alert_id)


async def _dispatch_email(alert_id: UUID, subject: str, html_body: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY", "")
    from_addr = os.environ.get("RESEND_FROM_EMAIL", "")
    to_addr = os.environ.get("RESEND_TO_EMAIL", "")
    ok = await send_resend_email(api_key, from_addr, to_addr, subject, html_body)
    if ok:
        _mark_email_sent(alert_id)


def dispatch_alert(trigger: Trigger) -> UUID | None:
    """Write-then-dispatch one trigger.

    Returns the persisted alert's UUID, or None if dedup-suppressed.

    Callers (runner.py write-hook + scheduler sweep) treat this as
    fire-and-forget for HTTP success — failures leave row for next-tick
    retry via the scheduler.
    """
    if _dedup_skip(trigger):
        logger.info(
            "alert dedup-suppressed: tenant=%s kind=%s",
            trigger.tenant_id, trigger.trigger_kind,
        )
        return None

    text, subject = _format_alert(trigger)
    alert_id = _persist_alert(trigger, text)

    is_canary = is_canary_tenant(trigger.tenant_id)
    force_immediate = trigger.severity == "critical" or _warning_burst_force_immediate(trigger)

    # Build a tiny inline-style HTML body for the email path (criticals + bursts).
    html = (
        '<div style="font-family:sans-serif;font-size:14px;">'
        f'<h3 style="margin:0 0 8px;">{trigger.severity.upper()} — {trigger.trigger_kind}</h3>'
        f'<pre style="background:#f7f7f7;padding:8px;border-radius:4px;">{text}</pre>'
        '</div>'
    )

    async def _runner() -> None:
        await _dispatch_telegram(alert_id, text, is_canary)
        # Canary path NEVER hits real email (Cowork lock).
        if force_immediate and not is_canary:
            await _dispatch_email(alert_id, subject, html)

    try:
        asyncio.run(_runner())
    except RuntimeError:
        # Already inside an event loop (e.g. dispatched from an async
        # FastAPI handler). Fallback: schedule on running loop.
        loop = asyncio.get_event_loop()
        loop.create_task(_runner())

    return alert_id


def retry_pending_sends() -> int:
    """Resend any tenant_alerts rows with NULL sent timestamps.

    Called by the 5-min scheduler. Idempotent: telegram_sent_at /
    email_sent_at non-NULL skips the corresponding channel.
    """
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, tenant_id, trigger_kind, severity, message_text,
                payload, telegram_sent_at, email_sent_at
            FROM tenant_alerts
            WHERE (telegram_sent_at IS NULL OR email_sent_at IS NULL)
              AND fired_at > now() - interval '24 hours'
            ORDER BY fired_at DESC
            LIMIT 100
            """
        )
        rows = cur.fetchall()

    sent_count = 0
    for raw in rows:
        rd = dict(raw) if not isinstance(raw, dict) else raw
        alert_id = UUID(str(rd["id"]))
        tenant_id = UUID(str(rd["tenant_id"]))
        text = rd["message_text"]
        severity = rd["severity"]
        is_canary = is_canary_tenant(tenant_id)

        async def _retry() -> None:
            nonlocal sent_count
            if rd["telegram_sent_at"] is None:
                if is_canary:
                    bot_token = os.environ.get("TELEGRAM_DEV_BOT_TOKEN", "")
                    chat_id = os.environ.get("TELEGRAM_DEV_CHAT_ID", "")
                else:
                    bot_token = os.environ.get("TELEGRAM_OPS_BOT_TOKEN", "")
                    chat_id = os.environ.get("TELEGRAM_OPS_CHAT_ID", "")
                if await send_telegram(bot_token, chat_id, text):
                    _mark_telegram_sent(alert_id)
                    sent_count += 1
            if (
                rd["email_sent_at"] is None
                and severity == "critical"
                and not is_canary
            ):
                api_key = os.environ.get("RESEND_API_KEY", "")
                from_addr = os.environ.get("RESEND_FROM_EMAIL", "")
                to_addr = os.environ.get("RESEND_TO_EMAIL", "")
                subject = scrub_pii(f"Viabe alert retry: {rd['trigger_kind']}")
                html = f"<pre>{text}</pre>"
                if await send_resend_email(api_key, from_addr, to_addr, subject, html):
                    _mark_email_sent(alert_id)
                    sent_count += 1
        try:
            asyncio.run(_retry())
        except RuntimeError:
            loop = asyncio.get_event_loop()
            loop.create_task(_retry())

    return sent_count


__all__ = [
    "dispatch_alert",
    "is_canary_tenant",
    "retry_pending_sends",
]
