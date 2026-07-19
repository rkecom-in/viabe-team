"""VT-202 — write-then-dispatch alert pipeline.

Insert ``tenant_alerts`` row FIRST, then fire Telegram + email.
HTTP failure → row stays in DB with NULL sent timestamps; the 5-min
scheduler retries on its next tick. Idempotent: a non-NULL
sent timestamp blocks re-send.

Channel routing (Cowork-locked):
- Dev-routed (VT-489 ``is_dev_routed``): canary tenant
  (``TEAM_CANARY_TENANT_IDS`` env) OR non-prod env (``EXPECTED_ENV != prod``,
  VT-362 sentinel) → DEV bot only; never email; never the ViabeOps OPS bot.
  A dev/test volume_spike (e.g. the 63211ce5 re-drive burst) thus never pages
  Fazal. PROD is unaffected — on ``EXPECTED_ENV=prod`` this is canary-only.
- Real ops traffic (prod, non-canary) → critical = OPS bot + email immediately;
  warning = batched into hourly digest unless 3+ in 5 min (force-immediate)

Dedup: same ``(tenant_id, trigger_kind)`` firing within 5-min sliding
window → only persist + dispatch once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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


def is_dev_routed(tenant_id: UUID) -> bool:
    """VT-489 (c): True when this alert must NOT page a real person on the ViabeOps
    OPS channel — route to the DEV bot (or suppress email) instead.

    An alert is dev-routed when EITHER:
      - the tenant is an explicit canary (``TEAM_CANARY_TENANT_IDS`` — existing
        Cowork lock), OR
      - the environment is NOT prod (``EXPECTED_ENV != prod``, the VT-362 sentinel
        via ``dev_send_guard.is_prod_env``). On dev/CI a volume_spike is dev/test
        traffic (e.g. the 63211ce5 re-drive burst) and must never page Fazal.

    PROD IS UNAFFECTED: on ``EXPECTED_ENV=prod`` this is True only for explicit
    canary tenants — exactly today's behaviour — so a real prod volume spike still
    pages the OPS channel + email. The env arm is the new, fail-safe addition:
    it only ever DOWNGRADES dev alerts to the dev bot; it never touches prod.
    """
    if is_canary_tenant(tenant_id):
        return True
    from orchestrator.utils.dev_send_guard import is_prod_env

    return not is_prod_env()


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


async def _dispatch_telegram(
    alert_id: UUID, scrubbed_text: str, is_dev: bool, tenant_id: UUID | None = None
) -> None:
    """Route to DEV bot for dev-routed alerts; OPS bot + assigned-VTR fan-out otherwise.

    VT-298 (Cowork DECISION 2 = BOTH): non-dev alerts go to the OPS chat (unchanged,
    retry-tracked via telegram_sent_at) AND are pushed to each assigned VTR's VERIFIED
    Telegram chat (best-effort immediate; Fazal: "report to VTR on Telegram immediately").

    VT-489 (c): ``is_dev`` = canary tenant OR non-prod env (``is_dev_routed``). A
    dev-routed alert stays DEV-bot-only — NEVER the ViabeOps OPS channel, NEVER real
    VTR chats — so dev/test volume never pages a real person. On prod this is the
    prior canary-only behaviour (PROD OPS paging intact).
    """
    if is_dev:
        bot_token = os.environ.get("TELEGRAM_DEV_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_DEV_CHAT_ID", "")
        ok = await send_telegram(bot_token, chat_id, scrubbed_text)
        if ok:
            _mark_telegram_sent(alert_id)
        return

    bot_token = os.environ.get("TELEGRAM_OPS_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_OPS_CHAT_ID", "")
    ok = await send_telegram(bot_token, chat_id, scrubbed_text)
    if ok:
        _mark_telegram_sent(alert_id)

    # VT-298: also push to the assigned VTR(s) for this tenant (verified chats only).
    if tenant_id is not None:
        from orchestrator.alerts.vtr_routing import resolve_assigned_vtr_chat_ids

        try:
            vtr_chats = resolve_assigned_vtr_chat_ids(tenant_id)
        except Exception:  # noqa: BLE001 — VTR fan-out must never break the OPS path
            logger.warning("VT-298: assigned-VTR resolution failed tenant=%s", tenant_id)
            vtr_chats = []
        for vtr_chat in vtr_chats:
            # Same OPS bot; the VTR's own chat. Best-effort (the OPS channel is the
            # retry-tracked durable one; per-recipient retry is a follow-up).
            await send_telegram(bot_token, vtr_chat, scrubbed_text)


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

    # VT-489 (c): dev-routed = canary tenant OR non-prod env. Dev-routed alerts go
    # to the DEV bot only, never the ViabeOps OPS channel + never real email — so a
    # dev/test volume_spike (e.g. 63211ce5 re-drive) never pages Fazal. PROD is
    # unaffected: on EXPECTED_ENV=prod this equals the prior canary-only behaviour.
    is_dev = is_dev_routed(trigger.tenant_id)
    force_immediate = trigger.severity == "critical" or _warning_burst_force_immediate(trigger)

    # Build a tiny inline-style HTML body for the email path (criticals + bursts).
    html = (
        '<div style="font-family:sans-serif;font-size:14px;">'
        f'<h3 style="margin:0 0 8px;">{trigger.severity.upper()} — {trigger.trigger_kind}</h3>'
        f'<pre style="background:#f7f7f7;padding:8px;border-radius:4px;">{text}</pre>'
        '</div>'
    )

    async def _runner() -> None:
        await _dispatch_telegram(alert_id, text, is_dev, trigger.tenant_id)
        # Dev-routed path NEVER hits real email (Cowork canary lock + VT-489 env arm).
        if force_immediate and not is_dev:
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
        # VT-489 (c): retry routing mirrors first-send routing — dev-routed (canary
        # OR non-prod env) retries via the DEV bot, never the OPS channel/email.
        is_dev = is_dev_routed(tenant_id)

        async def _retry() -> None:
            nonlocal sent_count
            if rd["telegram_sent_at"] is None:
                if is_dev:
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
                and not is_dev
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
    "is_dev_routed",
    "retry_pending_sends",
]
