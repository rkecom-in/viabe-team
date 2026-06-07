"""VT-279 — VTR escalation route classifier (deterministic-first, Pillar 1).

CL-426 three-way routing: when the agent is uncertain, split the escalation —
- a **business-KNOWLEDGE gap** ("how does this work?", a process/policy question) → the **VTR**;
- an **authority / preference / customer-specific** decision (approvals, pricing, refunds,
  always-never, anything needing customer identity) → the **OWNER** (Pillar 7).

DETERMINISTIC, boundary-safe, NFC, EN + Devanagari + Hinglish — reuses the VT-329 `keyword_match`
helper (so the apostrophe/matra/negation lessons carry over; "approve" never fires inside
"disapprove"). The LLM is fallback-only and is NOT wired here (Pillar 1 + cost; the conservative
default below is the safe Phase-1 behaviour) — `classify_escalation_route` exposes the deterministic
verdict + a `confident` flag so a caller MAY add an LLM tie-breaker for the ambiguous case later.

FAIL-SAFE (the whole point): authority/identity must NEVER be routed to the VTR. So OWNER signals
WIN over VTR signals, an identity (phone) present forces OWNER (respects VT-281 — the VTR can't see
PII), and the **ambiguous default is OWNER** (the owner can handle anything; mis-routing TO the VTR
is the harm). Over-routing to the owner is safe; under-protecting the VTR boundary is not.
"""

from __future__ import annotations

import re
from typing import Literal

from orchestrator.keyword_match import boundary_patterns, contains_any, nfc

Route = Literal["vtr", "owner"]

# OWNER = authority / preference / customer-specific (Pillar 7). Checked FIRST; these WIN.
_OWNER_KEYWORDS = [
    "approve", "approval", "approved", "reject", "decline",
    "price", "pricing", "discount", "rate", "charge", "fee",
    "refund", "cancel", "cancellation",
    "always", "never", "preference", "prefer",
    "block", "exclude", "blacklist", "ban",
    # Hinglish / Devanagari authority cues
    "daam", "kimat", "kitne", "chhoot", "mना", "मना", "कीमत", "दाम", "छूट", "वापसी",
]

# VTR = business-knowledge gap (process / policy / how-to). Only reached if NO owner signal fired.
_VTR_KEYWORDS = [
    "how do", "how does", "how to", "how can", "what is", "what are", "why does",
    "which", "unclear", "not sure", "don't know", "dont know", "unsure", "clarify",
    "policy", "process", "procedure", "workflow", "documentation", "guide",
    "kaise", "kya", "कैसे", "क्या", "नीति", "प्रक्रिया",
]

_OWNER_PATTERNS = boundary_patterns(_OWNER_KEYWORDS)
_VTR_PATTERNS = boundary_patterns(_VTR_KEYWORDS)

# Identity present → OWNER (VT-281: the VTR must never receive raw customer identity). A loose
# phone-ish detector (E.164 / 10-digit Indian mobile / long digit run); conservative — a false
# "identity present" only over-routes to the owner, which is the safe direction.
_PHONE_RE = re.compile(r"(?:\+?\d[\s-]?){10,}")


def classify_escalation_route(text: str | None, *, kind: str | None = None) -> tuple[Route, str]:
    """Return ``(route, reason)`` for an escalation's uncertainty text (+ optional kind).

    Deterministic precedence: identity/phone → OWNER; any OWNER (authority) signal → OWNER; else any
    VTR (knowledge-gap) signal → VTR; else the conservative default → OWNER. ``reason`` is a short
    machine tag for the audit/digest (never raw PII)."""
    body = nfc(text or "")

    # 1. Identity present → OWNER (never route customer identity to the VTR; VT-281).
    if _PHONE_RE.search(body):
        return "owner", "identity_present"

    # 2. Authority / preference / customer-specific → OWNER (Pillar 7). Wins over a knowledge cue.
    if contains_any(body, _OWNER_PATTERNS):
        return "owner", "authority_signal"

    # 3. Pure business-knowledge gap → VTR.
    if contains_any(body, _VTR_PATTERNS):
        return "vtr", "knowledge_gap"

    # 4. Ambiguous → OWNER (fail-safe: the owner can handle anything; mis-routing to the VTR is the
    #    harm). The `confident=False` case a future LLM tie-breaker would refine.
    return "owner", "ambiguous_default"


def is_confident(reason: str) -> bool:
    """True when the deterministic verdict came from a signal (not the ambiguous default) — the
    hook a future LLM fallback would gate on."""
    return reason != "ambiguous_default"
