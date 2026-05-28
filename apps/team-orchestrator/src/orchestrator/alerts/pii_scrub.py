"""VT-202 — PII scrub for alert message text.

Load-bearing dispatch step (Cowork lock 2026-05-28). Every alert
message — including the email Subject — passes through ``scrub_pii``
before send.

Phase-1 rules:
- Digit sequences ≥7 chars (covers phone numbers, account IDs, etc.)
- Twilio MessageSid shapes (``SM`` / ``MK`` / ``SA`` / ``WA`` + 30+ hex)
- E.164 phone numbers (``+`` followed by 7+ digits)
- Common-name patterns (deferred to Phase-2 — false-positive rate too
  high for an automated scrubber without per-tenant context)

Tradeoff: aggressive scrubbing is cheaper than under-scrubbing for
operator-facing channels. A redacted UUID is still actionable; a
leaked phone number is a privacy incident.
"""

from __future__ import annotations

import re

# E.164: explicit `+` followed by 7-15 digits.
_E164_RE = re.compile(r"\+\d{7,15}")
# Twilio MessageSid prefix forms.
_TWILIO_SID_RE = re.compile(r"\b(SM|MK|SA|WA)[0-9a-fA-F]{30,}\b")
# Bare digit sequences ≥7 chars. Applied LAST (after E.164 + SID rules)
# so the more-specific patterns get specific redaction labels.
_DIGIT_RUN_RE = re.compile(r"\d{7,}")


def scrub_pii(text: str) -> str:
    """Strip likely-PII patterns from an alert message.

    Returns a redacted string. Idempotent (running scrub_pii twice on
    the same text returns the same result).
    """
    if not text:
        return text
    text = _E164_RE.sub("[REDACTED:phone]", text)
    text = _TWILIO_SID_RE.sub("[REDACTED:sid]", text)
    text = _DIGIT_RUN_RE.sub("[REDACTED:digits]", text)
    return text


__all__ = ["scrub_pii"]
