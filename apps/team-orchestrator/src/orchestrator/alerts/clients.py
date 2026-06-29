"""VT-202 — httpx-direct Telegram + Resend clients.

Cowork-locked architecture (2026-05-28): no SDKs. ``httpx.AsyncClient``
against vendor APIs directly. Keeps the dep surface minimal and the
retry semantics in our control.

Both clients return ``True`` on 2xx, ``False`` on anything else
(including network exceptions). Callers — typically ``dispatch.py``
— interpret False as "leave the row for the next scheduler tick to
retry".
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)

_TELEGRAM_TIMEOUT_S = 10.0
_RESEND_TIMEOUT_S = 15.0


async def send_telegram(
    bot_token: str, chat_id: str, text: str
) -> bool:
    """POST a message to the Telegram Bot API.

    Returns True on 2xx; False on any failure (network, 4xx, 5xx).
    Caller persists False as "telegram_sent_at stays NULL"; next
    scheduler tick retries.
    """
    if not bot_token or not chat_id:
        logger.warning("send_telegram: missing bot_token or chat_id; skip send")
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=_TELEGRAM_TIMEOUT_S) as client:
            resp = await client.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "send_telegram: HTTP error chat=%s: %s", chat_id, repr(exc)
        )
        return False
    if 200 <= resp.status_code < 300:
        return True
    logger.warning(
        "send_telegram: non-2xx chat=%s status=%d body=%s",
        chat_id, resp.status_code, resp.text[:200],
    )
    return False


async def send_resend_email(
    api_key: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    html: str,
    attachments: list[dict] | None = None,
) -> bool:
    """POST an email via Resend API.

    Resend accepts ``from``, ``to`` (string or list), ``subject``, and
    one of ``html`` / ``text``. ``attachments`` (optional, VT-86) is a list of
    ``{"filename": str, "content": <base64 str>}`` dicts per the Resend API.
    Returns True on 2xx.
    """
    if not api_key or not from_addr or not to_addr:
        logger.warning(
            "send_resend_email: missing api_key/from/to; skip send"
        )
        return False
    payload: dict = {
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "html": html,
    }
    if attachments:
        payload["attachments"] = attachments
    try:
        async with httpx.AsyncClient(timeout=_RESEND_TIMEOUT_S) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        logger.warning("send_resend_email: HTTP error: %s", repr(exc))
        return False
    if 200 <= resp.status_code < 300:
        return True
    logger.warning(
        "send_resend_email: non-2xx status=%d body=%s",
        resp.status_code, resp.text[:200],
    )
    return False


def alert_is_dev_routed(tenant_id: UUID | str | None = None) -> bool:
    """VT-502: True when this ops alert must route to the DEV bot, NEVER the
    ViabeOps OPS (PROD ops) channel. Reuses the VT-489 ``is_dev_routed`` gate
    (no reinvention) so EVERY ``alert_fazal`` caller is gated the same way the
    volume_spike/dispatch path already is:

      - tenant-scoped alert (a tenant_id is known — support-bot escalation,
        escalation-SLA, template-error) → dev-routed iff
        ``is_dev_routed(tenant_id)`` = an explicit canary tenant
        (``TEAM_CANARY_TENANT_IDS``) OR a non-prod env (``EXPECTED_ENV != prod``).
      - global alert (``tenant_id`` None — VTR digest, billing ingress,
        dead-letter backstop, audit-chain break) → dev-routed iff the env is
        non-prod (the env arm only).

    PROD IS UNAFFECTED: on ``EXPECTED_ENV=prod`` a real (non-canary) tenant's
    alert — and every global ops alert — returns False → the OPS channel, exactly
    as before. The new behaviour only ever DOWNGRADES a dev/canary alert to the
    DEV bot, so the bogus re-drive tenant (``f0000bcd-…-beef``) can never page
    PROD ops. FAIL TOWARD OPS (return False) on any routing error — a real prod
    page is never silently suppressed.
    """
    try:
        if tenant_id is not None:
            from orchestrator.alerts.dispatch import is_dev_routed

            return is_dev_routed(tenant_id)
        from orchestrator.utils.dev_send_guard import is_prod_env

        return not is_prod_env()
    except Exception:  # noqa: BLE001 — never suppress a real ops page on a routing error
        logger.exception("alert routing check failed; defaulting to the OPS channel")
        return False


def alert_fazal(text: str, tenant_id: UUID | str | None = None) -> None:
    """Best-effort sync Telegram alert to the ops channel. Never raises into the
    caller's path. Loop-safe: if an event loop is already running (an async caller),
    ``asyncio.run`` would raise — so the send is off-loaded to a worker thread
    rather than silently dropped (an ops alert must not vanish in async contexts).

    Relocated to ``alerts.clients`` (VT-365): this is a generic ops-alert
    helper — not tied to any one billing path. Shared by the support-bot,
    template-error, email-deliverability, VTR-digest, escalation-SLA, and
    subscribe/billing-ingress paths.

    VT-502 — dev-aware routing (reuses the VT-489 gate via ``alert_is_dev_routed``):
    when the alert is dev-routed (a canary tenant OR a non-prod env), it goes to
    the DEV bot, NEVER the ViabeOps OPS channel — so a bogus/canary re-drive
    tenant's escalation (and any dev-env alert) never pages PROD ops. Pass
    ``tenant_id`` for a tenant-scoped alert so a canary tenant is dev-routed even
    on prod; omit it for a global ops alert (env-arm routing). On prod with a real
    (non-canary) tenant — or a global alert — it stays the OPS channel, unchanged.
    """
    import asyncio
    import threading

    if alert_is_dev_routed(tenant_id):
        bot_env, chat_env = "TELEGRAM_DEV_BOT_TOKEN", "TELEGRAM_DEV_CHAT_ID"
    else:
        bot_env, chat_env = "TELEGRAM_OPS_BOT_TOKEN", "TELEGRAM_OPS_CHAT_ID"

    def _run() -> None:
        try:
            asyncio.run(
                send_telegram(
                    os.environ.get(bot_env, ""),
                    os.environ.get(chat_env, ""),
                    text,
                )
            )
        except Exception:  # noqa: BLE001 — alert is best-effort, never blocks the caller
            logger.exception("alert_fazal: Telegram alert failed (best-effort)")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _run()  # no running loop — safe to asyncio.run inline
        return
    # A loop is already running; off-thread the send so asyncio.run can't raise.
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=10)


__all__ = ["send_telegram", "send_resend_email", "alert_fazal", "alert_is_dev_routed"]
