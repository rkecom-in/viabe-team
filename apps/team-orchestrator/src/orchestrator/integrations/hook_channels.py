"""VT-288 — email + SMS hook channels.

Hooks drive customers to message the business on WhatsApp: a value message + a tokenised
`/r/<token>` link (durable attribution, see hook_links). Email = Resend. SMS = vendor-
AGNOSTIC injectable seam (Cowork VT-288 #2): default-targets Twilio, but the SMS vendor +
the DLT entity/template registration is a Fazal real-world decision (India-regulatory,
tied to the legal entity) — so `sms_fn` is injectable and the build/canary run against
sandbox/mock; DO NOT block on DLT registration.

The ramp governor (ramp_governor.py) gates daily send VOLUME; these are the per-message
channel seams. Both injectable for tests — no network in the canary.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

logger = logging.getLogger(__name__)

# (to, subject_or_none, body) -> provider id. Injectable for tests.
EmailFn = Callable[[str, str, str], str]
SmsFn = Callable[[str, str], str]


def build_hook_url(token: str) -> str:
    """The public `/r/<token>` URL (HOOK_BASE_URL = the public host serving the redirect)."""
    base = os.environ.get("HOOK_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("HOOK_BASE_URL not set — required for hook links (.env)")
    return f"{base}/r/{token}"


def _default_email_send(to_email: str, subject: str, body: str) -> str:
    """Resend email send (RESEND_API_KEY). DLT N/A for email."""
    import httpx

    api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("RESEND_FROM_EMAIL", "")
    if not (api_key and from_email):
        raise RuntimeError("RESEND_API_KEY / RESEND_FROM_EMAIL not set (.viabe/secrets/resend.env)")
    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"from": from_email, "to": [to_email], "subject": subject, "html": body},
        timeout=15.0,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Resend send failed: HTTP {resp.status_code} {resp.text[:200]}")
    return str(resp.json().get("id", ""))


def _default_sms_send(to_phone: str, body: str) -> str:
    """Default SMS send — vendor + DLT are a Fazal real-world setup (see module docstring).
    Unwired against live until that lands; the canary injects a fake."""
    raise NotImplementedError(
        "SMS vendor + DLT template registration is a Fazal real-world decision (VT-288 #2); "
        "inject sms_fn for tests, wire the live vendor at E2E"
    )


def send_email_hook(
    to_email: str, value_message: str, hook_url: str, *,
    subject: str = "A quick update", email_fn: EmailFn | None = None,
) -> str:
    """Send an email value-hook with the click-to-WhatsApp link."""
    send = email_fn or _default_email_send
    body = f"{value_message}<br><br>👉 <a href=\"{hook_url}\">Chat with us on WhatsApp</a>"
    return send(to_email, subject, body)


def send_sms_hook(
    to_phone: str, value_message: str, hook_url: str, *, sms_fn: SmsFn | None = None,
) -> str:
    """Send an SMS value-hook with the click-to-WhatsApp link (DLT-templated in prod)."""
    send = sms_fn or _default_sms_send
    body = f"{value_message} {hook_url}"
    return send(to_phone, body)


__all__ = ["EmailFn", "SmsFn", "build_hook_url", "send_email_hook", "send_sms_hook"]
