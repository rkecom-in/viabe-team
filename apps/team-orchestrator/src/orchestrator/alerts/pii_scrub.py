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
# VT-502: canonical UUID (tenant_id / run_id). A UUID is NOT PII — it is an
# actionable handle the operator opens in the Ops Console — but a synthetic id
# like ``f0000bcd-0000-4000-8000-00000000beef`` carries an 8-zero run that the
# bare-digit rule would mangle to ``…-[REDACTED:digits]beef``. We protect the
# whole UUID from digit-redaction (stash → scrub → restore).
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def scrub_pii(text: str) -> str:
    """Strip likely-PII patterns from an alert message.

    Returns a redacted string. Idempotent (running scrub_pii twice on
    the same text returns the same result).

    VT-502: canonical UUIDs (tenant_id / run_id) are exempted — they are not PII
    and stay readable so the alert remains actionable in the Ops Console.
    """
    if not text:
        return text
    # Stash UUIDs behind NUL-delimited placeholders so the digit-run rule can't
    # touch them, then restore. The placeholder (\x00U<i>\x00) carries no 7+ digit
    # run / E.164 / SID shape, so no scrub rule matches it. Idempotent: a second
    # pass re-finds the restored UUIDs and re-stashes identically.
    stash: dict[str, str] = {}

    def _hold(m: re.Match[str]) -> str:
        key = f"\x00U{len(stash)}\x00"
        stash[key] = m.group(0)
        return key

    text = _UUID_RE.sub(_hold, text)
    text = _E164_RE.sub("[REDACTED:phone]", text)
    text = _TWILIO_SID_RE.sub("[REDACTED:sid]", text)
    text = _DIGIT_RUN_RE.sub("[REDACTED:digits]", text)
    for key, val in stash.items():
        text = text.replace(key, val)
    return text


def find_pii(text: str) -> list[str]:
    """VT-79 Detector-5: return the PII-pattern kinds present in ``text``.

    The detection counterpart of ``scrub_pii`` — reuses the SAME patterns (one
    PII-pattern source, Pillar 8). Empty list = clean. Already-scrubbed text
    (``[REDACTED:...]``) returns clean, so a redacted payload never false-fires.
    Twilio SIDs are intentionally NOT flagged: they are an allowed provenance
    handle (CL-390), not PII.
    """
    if not text:
        return []
    # Mask the allowed Twilio-SID shape first so its hex digits don't trip the
    # phone / digit-run patterns.
    masked = _TWILIO_SID_RE.sub("[SID]", text)
    found: list[str] = []
    if _E164_RE.search(masked):
        found.append("phone")
    if _DIGIT_RUN_RE.search(masked):
        found.append("digits")
    return found


__all__ = ["find_pii", "scrub_pii"]
