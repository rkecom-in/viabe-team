"""Pre-Filter Gate — Stage 1 of the two-stage filter (VT-3.8).

FULLY DETERMINISTIC (Pillar 1, revised 2026-05-12): regex / exact-match /
signature checks only. ZERO LLM calls — CI greps this file and direct_handlers/
to enforce it.

Conservative matching: a false route to the brain (a cost overhead) is
preferred over a false route to a direct handler (a privacy / UX risk). When
in doubt, route to the brain.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from dbos import DBOS

from orchestrator.types import (
    PreFilterResult,
    Reject,
    RouteToBrain,
    RouteToDirectHandler,
    Tenant,
    WebhookEvent,
)

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _load_keywords(filename: str) -> list[str]:
    data = yaml.safe_load((_CONFIG_DIR / filename).read_text())
    return [str(keyword) for keyword in data.get("keywords", [])]


# Opt-out: exact match (case-insensitive) on the whole trimmed message body.
_OPT_OUT_KEYWORDS = {kw.casefold() for kw in _load_keywords("opt_out_keywords.yaml")}

# DSR: case-insensitive, word-boundary match anywhere in the body. Word
# boundaries keep matching conservative (e.g. "my data" does not fire on
# "my database").
_DSR_PATTERNS = [
    re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
    for kw in _load_keywords("dsr_keywords.yaml")
]

# Status ping: narrow regex — matches ONLY a whole-message trivial query.
_STATUS_PING = re.compile(
    r"^\s*(hi|hello|hey|any update|any updates|कैसा चल रहा है)\s*[?!.]*\s*$",
    re.IGNORECASE,
)


def _normalize(body: str) -> str:
    """Collapse whitespace and case-fold for exact keyword comparison."""
    return " ".join(body.split()).casefold()


@DBOS.step()
def pre_filter(event: WebhookEvent, tenant: Tenant) -> PreFilterResult:
    """Deterministically route a webhook event. See the module docstring.

    `tenant` is accepted for parity with the VT-3.3 caller and future rules; the
    VT-3.8 routing rules are driven entirely by the event.
    """
    # --- Twilio status callbacks ---
    if event.message_type == "status_callback":
        state = event.status_callback_state
        if state == "failed":
            return RouteToDirectHandler(
                handler_name="template_error_handler",
                payload={"twilio_message_sid": event.twilio_message_sid},
            )
        if state in ("delivered", "read"):
            return Reject(
                reason=f"status callback '{state}' — observability only (VT-122)"
            )
        # 'undelivered' or missing — conservative: let the brain decide.
        return RouteToBrain(reason=f"status callback state '{state}' — needs review")

    # --- Inbound message body checks ---
    normalized = _normalize(event.body)

    # Rule a — opt-out keyword (exact, case-insensitive, EN + HI).
    if normalized in _OPT_OUT_KEYWORDS:
        return RouteToDirectHandler(
            handler_name="opt_out_handler", payload={"matched": normalized}
        )

    # Rule b — DSR keyword (case-insensitive word-boundary, EN + HI).
    for pattern in _DSR_PATTERNS:
        if pattern.search(event.body):
            return RouteToDirectHandler(
                handler_name="dsr_handler", payload={"matched": pattern.pattern}
            )

    # Rule f — status ping (narrow whole-message regex).
    if _STATUS_PING.match(event.body):
        return RouteToDirectHandler(handler_name="status_ping_handler")

    # Rule g — everything else needs the orchestrator-agent brain (VT-3.9).
    if event.message_type == "unknown":
        return RouteToBrain(reason="unknown message type — needs reasoning")
    return RouteToBrain(
        reason="substantive owner message — needs orchestrator-agent reasoning"
    )
