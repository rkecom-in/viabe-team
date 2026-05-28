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

import httpx

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
) -> bool:
    """POST an email via Resend API.

    Resend accepts ``from``, ``to`` (string or list), ``subject``, and
    one of ``html`` / ``text``. Returns True on 2xx.
    """
    if not api_key or not from_addr or not to_addr:
        logger.warning(
            "send_resend_email: missing api_key/from/to; skip send"
        )
        return False
    try:
        async with httpx.AsyncClient(timeout=_RESEND_TIMEOUT_S) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": from_addr,
                    "to": [to_addr],
                    "subject": subject,
                    "html": html,
                },
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


__all__ = ["send_telegram", "send_resend_email"]
