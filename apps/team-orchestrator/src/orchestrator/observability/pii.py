"""PII redaction at the LangSmith boundary (VT-101).

Inline scope per Cowork heads-up Option 1: redact phones, message bodies, and
named-key PII (name / email / customer_name) before any value enters
LangSmith. VT-104 (PII redactor) will subsume this; the call sites
(``redact_for_langsmith``) stay stable across the swap.

Design rule (mechanical bypass-block): redaction lives inside the
``traceable_node`` / ``traceable_tool`` decorator wrapper. To bypass, the
caller would have to replace the decorator — there is no "raw mode" flag.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

# E.164-ish phone pattern: optional +, optional country/area separators, 7-15 digits.
# Conservative: matches `+91 98765 43210`, `+1-415-555-0100`, `9876543210`.
_PHONE_RE = re.compile(r"\+?\d[\d \-]{6,18}\d")

# Keys whose values are categorically PII regardless of content.
_PII_KEYS = frozenset(
    {
        "name",
        "customer_name",
        "owner_name",
        "first_name",
        "last_name",
        "email",
        "phone",
        "phone_e164",
        "mobile",
        "address",
        "body",
        "message_body",
        "raw_body",
        # VT-102 extensions: error events carry stack traces and messages
        # that may transitively contain PII (file paths embedding usernames,
        # quoted payload fragments). Treat both as body-style hashable.
        "stack_trace",
        "stacktrace",
        "error_message",
    }
)

# Recursion guard for pathological self-referential dicts.
_MAX_DEPTH = 32


def _salt() -> str:
    salt = os.environ.get("TEAM_PHONE_HASH_SALT", "")
    if not salt:
        # Stable zero-knowledge fallback so tests + dev don't crash; production
        # MUST set the salt (orchestrator init checks env at boot).
        return "vt-101-fallback-salt-not-for-prod"
    return salt


def _hash_body(body: str) -> str:
    """SHA256[:16] of body with salt. Mirrors ``utils.phone_token.hash_phone`` shape."""
    digest = hashlib.sha256(f"{_salt()}:body:{body}".encode()).hexdigest()
    return f"body_tok_{digest[:16]}"


def _hash_phone_inline(phone: str) -> str:
    digest = hashlib.sha256(f"{_salt()}:phone:{phone}".encode()).hexdigest()
    return f"phone_tok_{digest[:16]}"


def _redact_str(value: str) -> str:
    """Replace phone-number-shaped substrings with hashed tokens."""

    def _sub(match: re.Match[str]) -> str:
        phone = match.group(0)
        if sum(c.isdigit() for c in phone) < 7:
            # Too few digits to be a phone (e.g., "12-3" timestamps); keep as is.
            return phone
        return _hash_phone_inline(re.sub(r"[ \-]", "", phone))

    return _PHONE_RE.sub(_sub, value)


def redact_for_langsmith(value: Any, _depth: int = 0) -> Any:
    """Return a PII-safe copy of ``value`` suitable for sending to LangSmith.

    - ``str``: phone-shaped substrings hashed in place.
    - ``dict``: each key in ``_PII_KEYS`` has its value tokenized (phones via
      :func:`_hash_phone_inline`, bodies via :func:`_hash_body`, names/emails
      replaced with ``<redacted:type>`` tokens). Other keys recurse.
    - ``list`` / ``tuple``: recurses element-wise.
    - Other types: returned unchanged.

    Never raises. Bypass requires replacing the decorator wrapper at the
    call site (see ``observability/langsmith.py``).
    """
    if _depth > _MAX_DEPTH:
        return "<redaction-depth-exceeded>"

    if isinstance(value, str):
        return _redact_str(value)

    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            key_str = str(k).lower()
            if key_str in _PII_KEYS:
                out[k] = _redact_pii_value(key_str, v)
            else:
                out[k] = redact_for_langsmith(v, _depth + 1)
        return out

    if isinstance(value, (list, tuple)):
        redacted = [redact_for_langsmith(item, _depth + 1) for item in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted

    return value


def _redact_pii_value(key_lower: str, value: Any) -> str:
    """Tokenize a value at a known-PII key."""
    if value is None:
        return "<redacted:none>"
    if key_lower in {"phone", "phone_e164", "mobile"}:
        return _hash_phone_inline(str(value))
    if key_lower in {"body", "message_body", "raw_body", "stack_trace", "stacktrace", "error_message"}:
        return _hash_body(str(value))
    if key_lower in {"email"}:
        return "<redacted:email>"
    # name / address / etc — token with length hint, no content.
    text = str(value)
    return f"<redacted:{key_lower}:len={len(text)}>"


# VT-102: alias for call-site clarity at non-LangSmith sinks (pipeline_log).
# Both sinks share one redactor so future VT-104 replaces the implementation
# in exactly one place.
redact_for_log = redact_for_langsmith


__all__ = ["redact_for_langsmith", "redact_for_log"]
