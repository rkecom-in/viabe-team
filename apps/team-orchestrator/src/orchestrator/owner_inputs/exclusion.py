"""VT-84 — owner exclusion handler.

Owner says "exclude customer 98765" / "don't message Rajesh again" / "customer X ko
exclude karo". We extract a customer (phone-exact wins; else fuzzy name), then set
opt_out_status='owner_excluded' — ONLY from 'subscribed' (a consumer opt-out ALWAYS wins;
never downgrade a legal opt-out). On an AMBIGUOUS name (multiple fuzzy matches) we NEVER
auto-pick (Pillar 7) — we ask for the phone number; the owner's phone reply re-resolves
deterministically. VT-329-safe parsing (NFC + whitespace/punct split, no Devanagari `\\b`).

# NEEDS-FAZAL: the response copy (Pillar 7 — owner-facing words). The LOGIC lands now.
"""

from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple
from uuid import UUID

from orchestrator.db.wrappers import CustomersWrapper
from orchestrator.owner_inputs.customer_lookup import resolve_customer

# A phone-like run of 10-14 digits (optionally +, spaces, dashes).
_PHONE_RE = re.compile(r"(\+?\d[\d\s\-]{8,13}\d)")
# Tokens stripped before treating the remainder as a candidate name.
_KEYWORDS = {
    "exclude",
    "excluded",
    "message",
    "msg",
    "customer",
    "dont",
    "don",
    "do",
    "not",
    "again",
    "stop",
    "ko",
    "karo",
    "mat",
    "bhejo",
    "please",
    "the",
    "to",
    "my",
    "is",
    "him",
    "her",
    "he",
    "she",
    "they",
    "angry",
    "naraz",
    "number",
    "phone",
    "remove",
    "from",
    "campaigns",
    "campaign",
    "anymore",
    "and",
    "a",
    "an",
}


class ExclusionResult(NamedTuple):
    action: str  # excluded | already_excluded | ambiguous | not_found | needs_identifier
    customer_id: UUID | None
    response_text: str


def _extract_phone(body: str) -> str | None:
    """India-centric (Phase 1): a bare 10-digit / 0-prefixed / 91-prefixed number
    normalizes to +91 E.164. A non-Indian number simply won't match a customer
    (-> not_found; safe — never a wrong exclusion)."""
    m = _PHONE_RE.search(body or "")
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "+91" + digits[1:]
    return ("+" + digits) if digits else None


def _extract_name(body: str) -> str | None:
    # Strip apostrophes so "don't" collapses to "dont" and matches the keyword set
    # (else "don't message Rajesh" leaks "don't" into the candidate name).
    norm = unicodedata.normalize("NFC", (body or "").strip()).replace("'", "").replace("’", "")
    tokens = [t for t in re.split(r"[\s,.!?;:।/\\-]+", norm) if t]
    candidate = [t for t in tokens if t.casefold() not in _KEYWORDS and not t.isdigit()]
    return " ".join(candidate).strip() or None


def handle_exclusion(tenant_id: UUID | str, body: str) -> ExclusionResult:
    """Resolve + exclude the customer named in the owner's message. Pure of sends — the
    caller (stage-2 router) delivers ``response_text``."""
    phone = _extract_phone(body)
    name = None if phone else _extract_name(body)
    if not phone and not name:
        return ExclusionResult(
            "needs_identifier",
            None,
            "Which customer should I exclude? Please share their phone number.",
        )

    match = resolve_customer(tenant_id, phone_e164=phone, name=name)
    if match.ambiguous:
        # Multiple fuzzy name matches — NEVER auto-pick (Pillar 7). Ask for the phone;
        # the owner's number reply re-resolves via phone-exact. (A formal clarifying_flow
        # row with reply-tracking is a possible refinement.)
        return ExclusionResult(
            "ambiguous",
            None,
            f"I found more than one customer matching '{name}'. Please reply with their "
            "phone number so I exclude the right one.",
        )
    if match.customer_id is None:
        return ExclusionResult(
            "not_found",
            None,
            "I couldn't find that customer. Please share their phone number.",
        )

    updated = CustomersWrapper().set_owner_excluded(tenant_id, match.customer_id)
    if updated:
        return ExclusionResult(
            "excluded",
            match.customer_id,
            "Done — that customer is now excluded from future campaigns.",
        )
    # 0 rows updated: already opted_out / blocked / excluded — consumer opt-out preserved.
    return ExclusionResult(
        "already_excluded",
        match.customer_id,
        "That customer is already excluded from your campaigns.",
    )
