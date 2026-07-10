"""#49 — deterministic emission speech-act gate (the Tier-1 fabrication CLASS fix).

An LLM-authored owner-facing message may NOT claim a completed send/action ("Done! Campaign
bhej diya") unless a matching DB fact backs it up. Pre-gate, a manager turn could tell the owner
a campaign went out while the real send count was zero — a Tier-1 trust-breaker, and the kind of
bug that recurs every time the emission surface changes unless it is closed at the SEND boundary,
not patched turn-by-turn.

Two thin pieces, wired at the choke points where LLM text becomes an owner send:
  - ``contains_completion_claim`` — a pure token matcher (EN + Hinglish + Devanagari), tight by
    design: bare "done"/"sent" never trips it, and future-tense "I'll send ..." is a distinct
    token from past-tense "sent" so it never false-positives ("I'll send you the approval ask
    next" passes clean). Anchored on explicit send-COMPLETION bigrams only. NFC-normalize + split
    on whitespace/punct ONLY, same discipline as ``owner_inputs/approval_reply.py`` (VT-329: an
    ASCII ``\\b``/``[^\\w]`` shatters Devanagari matras).
  - ``send_fact_exists`` — one SQL round-trip: was there a real send (``campaign_messages`` /
    the VT-418 ``send_idempotency_keys`` ledger) or a manager task that closed
    ``completed_with_effect``, in the last 15 minutes? FAIL-CLOSED: a DB read error means "no
    fact" (not "trust the claim") — a wrongly-softened honest message is Tier-2, a shipped lie is
    Tier-1.

``apply_emission_gate`` composes the two: no claim -> pass through untouched (the overwhelming
majority of turns, zero DB cost). Claim + fact -> pass through (the claim is true). Claim + no
fact -> swap the ENTIRE message for a deterministic, bilingual, honest line (pending-approval-
specific when one is open, else generic) and drop a ``tm_audit`` breadcrumb (the blocked text's
hash only — CL-390, never the text itself). The whole function is wrapped so it can NEVER raise:
a bug in the honest-replacement path must degrade to shipping the ORIGINAL text, not break the
send outright (the gate is a backstop layered on a working pipeline, not a new way to go silent).

EXEMPT by construction (not wired here): ``task_outcome.py`` / ``freeform_acks`` (D1) / approval
templates / the opt-out handler / ``dispatch._collapse_reply_body`` (VT-594) — every one of those
bodies is built from typed, deterministic fields, never free LLM prose, so there is no claim to
verify. Wired seams: ``dispatch._maybe_send_manager_reply`` (VT-593 raw/composed reply) and the
``reply_to_owner`` tool (VT-632 Step 1 manager-loop emission).
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from uuid import UUID

logger = logging.getLogger(__name__)

# How far back a real send / effect counts as "backing" a completion claim made THIS turn.
_FACT_WINDOW_MINUTES = 15

# VT-329-safe split: whitespace/punctuation ONLY, never an ASCII \b or [^\w] (Devanagari matras
# are not \w and shatter under either). Mirrors owner_inputs/approval_reply.py verbatim.
_SPLIT_RE = re.compile(r"[\s,.!?;:।/\\-]+")

# Adjacent-token bigrams that are a send-COMPLETION claim, tight by design so a legitimate
# future-tense / message-about-a-message reply never trips ("I'll send you the approval ask
# next" has no "sent" token at all; "Done!" / "sent" bare never appear here alone).
_EN_BIGRAMS = {
    ("i", "sent"),
    ("ive", "sent"),  # "I've sent" — apostrophe stripped before tokenizing, like approval_reply.py
    ("campaign", "sent"),
    ("messages", "sent"),
}
_HINGLISH_BIGRAMS = {
    ("bhej", "diya"),
    ("bhej", "di"),
    ("bhej", "diye"),
    ("maine", "bheja"),
}
_DEVANAGARI_BIGRAMS = {
    ("भेज", "दिया"),
    ("भेज", "दी"),
    ("भेज", "दिए"),
}
_COMPLETION_BIGRAMS = _EN_BIGRAMS | _HINGLISH_BIGRAMS | _DEVANAGARI_BIGRAMS


def _tokenize(text: str) -> list[str]:
    """NFC-normalize + casefold + strip apostrophes, then split on whitespace/punct only."""
    normalized = (
        unicodedata.normalize("NFC", (text or "").strip().casefold())
        .replace("'", "")
        .replace("’", "")
    )
    return [t for t in _SPLIT_RE.split(normalized) if t]


def contains_completion_claim(text: str) -> bool:
    """True iff ``text`` makes a send-COMPLETION claim (EN / Hinglish / Devanagari).

    Deliberately tight: matches adjacent-token bigrams anchored on an explicit send-completion
    phrase, plus the "sent to N" trigram (a count backs the claim). Bare "done" or "sent" alone
    never matches — those are common in perfectly honest replies ("Done!", "I've sent you the
    plan") that make no claim about a customer/campaign send.
    """
    tokens = _tokenize(text)
    for i in range(len(tokens) - 1):
        if (tokens[i], tokens[i + 1]) in _COMPLETION_BIGRAMS:
            return True
        if (
            tokens[i] == "sent"
            and tokens[i + 1] == "to"
            and i + 2 < len(tokens)
            and tokens[i + 2].isdigit()
        ):
            return True
    return False


# One round-trip: any of (a real campaign send, a real l2_send ledger hit, a manager task that
# verified an effect) inside the window counts as "the claim is true".
_FACT_SQL = """
    SELECT (
        EXISTS (
            SELECT 1 FROM campaign_messages
             WHERE tenant_id = %(tenant_id)s
               AND send_status IN ('sent', 'template_sent')
               AND created_at >= now() - interval '{window} minutes'
        )
        OR EXISTS (
            -- VT-418 l2_send driver's send ledger — the agent-draft send path has no
            -- campaign_messages row, only this idempotency-keyed record.
            SELECT 1 FROM send_idempotency_keys
             WHERE tenant_id = %(tenant_id)s
               AND send_status = 'sent'
               AND created_at >= now() - interval '{window} minutes'
        )
        OR EXISTS (
            SELECT 1 FROM manager_tasks
             WHERE tenant_id = %(tenant_id)s
               AND terminal_outcome = 'completed_with_effect'
               AND updated_at >= now() - interval '{window} minutes'
        )
    ) AS fact_exists
""".format(window=_FACT_WINDOW_MINUTES)

def send_fact_exists(tenant_id: UUID | str) -> bool:
    """Did a real send / verified effect land for this tenant in the last 15 minutes?

    FAIL-CLOSED: any read error returns ``False`` (no confirmed fact), never raises — a DB blip
    must swap the claim for the honest line, not silently trust it (a wrongly-softened honest
    message is Tier-2; a shipped fabrication is Tier-1).
    """
    try:
        from orchestrator.db.tenant_connection import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(_FACT_SQL, {"tenant_id": str(tenant_id)}).fetchone()
    except Exception:  # noqa: BLE001 — fail-closed: treat a read error as "no fact"
        logger.warning(
            "emission_gate: send-fact read failed tenant=%s — fail-closed (no fact)",
            tenant_id,
            exc_info=True,
        )
        return False
    return bool(dict(row).get("fact_exists")) if row else False


def _has_open_approval(tenant_id: UUID | str) -> bool:
    """Best-effort: is there an unresolved ``pending_approvals`` row? Picks which honest
    replacement line to use. Wrapper-layer read (VT-72/306 no-direct-tenant-db-access gate).
    Any error -> ``False`` (falls back to the generic line)."""
    try:
        from orchestrator.db.wrappers import PendingApprovalsWrapper

        return PendingApprovalsWrapper().has_open_for_tenant(tenant_id)
    except Exception:  # noqa: BLE001 — best-effort; default to the generic replacement
        logger.warning(
            "emission_gate: open-approval check failed tenant=%s — defaulting to generic reply",
            tenant_id,
            exc_info=True,
        )
        return False


# Fazal-style bilingual honest lines (EN + Hinglish, matching the register of the spec's own
# copy) — a blocked claim is replaced with one of these, never with silence and never with the
# original (possibly fabricated) text.
_REPLACEMENT_COPY: dict[str, dict[str, str]] = {
    "pending_approval": {
        "en": (
            "The draft is ready and waiting for your approval — nothing has been sent yet. "
            "Reply to the approval message to send it."
        ),
        "hi": (
            "Draft taiyaar hai aur aapki approval ka intezaar hai — abhi kuch bheja nahi gaya "
            "hai. Approval message ka jawaab dete hi main bhej dunga."
        ),
    },
    "generic": {
        "en": "I'm still working on this — I'll confirm once it's actually done.",
        "hi": "Main abhi ispar kaam kar raha hoon — poora hone par confirm kar dunga.",
    },
}


def _emit_blocked_audit(tenant_id: UUID | str, blocked_text: str) -> None:
    """tm_audit breadcrumb for a blocked claim. The blocked text is NEVER stored, only its
    sha256 hash (CL-390 — no raw free text at rest for this event). ``emit_tm_audit`` with
    ``conn=None`` is fail-soft by its own contract, so this never raises."""
    from orchestrator.observability.tm_audit import emit_tm_audit

    text_hash = hashlib.sha256(blocked_text.encode("utf-8")).hexdigest()
    emit_tm_audit(
        event_layer="does",
        event_kind="emission_claim_blocked",
        actor="team_manager",
        tenant_id=tenant_id,
        decision={"blocked_text_sha256": text_hash},
    )


def apply_emission_gate(text: str, tenant_id: UUID | str) -> str:
    """The gate: a completion claim with no backing DB fact is replaced with an honest line.

    No claim -> ``text`` unchanged (zero DB cost — the common case). Claim + a real fact ->
    unchanged (the claim is true). Claim + no fact -> the deterministic bilingual replacement
    (pending-approval-specific when one is open, else generic), plus a ``tm_audit`` breadcrumb.

    Wrapped end-to-end so this NEVER raises: any error anywhere in the replacement path (locale
    resolution, the approval check, the audit emit) logs a warning and passes the ORIGINAL text
    through — the gate must never be the reason a send breaks.
    """
    try:
        if not text or not contains_completion_claim(text):
            return text
        if send_fact_exists(tenant_id):
            return text

        from orchestrator.owner_surface.freeform_acks import resolve_owner_locale

        kind = "pending_approval" if _has_open_approval(tenant_id) else "generic"
        locale = resolve_owner_locale(tenant_id)
        variants = _REPLACEMENT_COPY[kind]
        replacement = variants.get(locale) or variants["en"]

        _emit_blocked_audit(tenant_id, text)
        return replacement
    except Exception:  # noqa: BLE001 — the gate must NEVER break a send
        logger.warning(
            "emission_gate: guard failed (fail-soft passthrough) tenant=%s",
            tenant_id,
            exc_info=True,
        )
        return text


__all__ = ["apply_emission_gate", "contains_completion_claim", "send_fact_exists"]
