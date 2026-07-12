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

``apply_emission_gate`` composes two deterministic layers:
  1. Completion claim (fabricated "sent"): no claim -> pass through (the common case, zero DB
     cost); claim + fact -> pass through (true); claim + no fact -> swap the ENTIRE message for a
     bilingual honest line (pending-approval-specific when one is open, else generic).
  2. #58/T7 phantom promise (a deferred follow-up from a nonexistent team/person — an
     impossible_promise Tier-1 breaker): surgically STRIP the offending sentence(s), keeping the
     honest remainder; if that empties the message, fall back to the honest generic line. Runs on
     honest text and on a true completion claim alike.
Every block drops a ``tm_audit`` breadcrumb (the blocked text's hash only — CL-390, never the
text itself). The whole function is wrapped so it can NEVER raise: a bug in the
strip/replacement path must degrade to shipping the ORIGINAL text, not break the send outright
(the gate is a backstop layered on a working pipeline, not a new way to go silent).

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

# ── #58 (T7) — phantom-promise phrases ────────────────────────────────────────────────────
# A SECOND deterministic class: the brain's LLM-composed FAQ fallback intermittently promises a
# deferred follow-up from a team/person that does NOT exist ("I'll have the team confirm", "I'll
# follow up", "get back to you"). There is no human team behind the manager and no async
# follow-up loop, so any such promise is impossible to keep — a Tier-1 impossible_promise breaker.
# The prompt (orchestrator_agent_system.md) is the root fix (license removed + explicit
# prohibition); this is the deterministic backstop for the residual LLM variance.
#
# TIGHT by design — only unambiguously PROMISSORY constructs (agent -> owner deferred action).
# Bare "the team" / "follow-up question?" never appear here. Matched as space-delimited phrases
# against the normalized+tokenized text (apostrophes already stripped: "I'll" -> "ill").
_PHANTOM_PROMISE_PHRASES = frozenset(
    {
        # English
        "ill follow up",
        "i will follow up",
        "we will follow up",
        "follow up with you",
        "follow up shortly",
        "follow up soon",
        "and follow up",
        "get back to you",
        "ill get back",
        "will get back to you",
        "circle back",
        "ill circle back",
        "reach out to you",
        "ill reach out",
        "have the team confirm",
        "the team will confirm",
        "the team will get back",
        "ill have the team",
        "have someone confirm",
        "have someone look into",
        "ill let you know once",
        "let you know once its",
        # Hinglish (romanized)
        "follow up karunga",
        "follow up karungi",
        "team se confirm",
        "team se pata",
        "pata karke bataunga",
        "pata karke bataungi",
        "check karke bataunga",
        "check karke bataungi",
        "baad me bataunga",
        "baad mein bataunga",
    }
)

# Sentence boundaries for the surgical clause-strip (EN + Devanagari danda). The phantom promise
# is almost always a trailing clause on an otherwise-honest answer, so we drop the offending
# SENTENCE rather than the whole message (which would discard the honest content).
_SENTENCE_SPLIT_RE = re.compile(r"([.!?।]+)")

# ── cluster-2a (full-77 sr_stop_then_resume, §2 fabrication 3/3) — fabricated CUSTOMER DEBT ──────
# The brain told the owner their LAPSED customers "owe" / have "₹X overdue / payment pending". The
# cohort is lapsed BUYERS (lifetime_spend_paise + days_since_last_sale) — there is NO receivable /
# overdue / pending-payment concept anywhere in the schema, so an invented aggregate ₹ debt
# attributed to customers is a fabrication (a false "they owe you money" that could push the owner
# to dun customers who owe nothing). HIGH PRECISION by construction: fires ONLY when a debt word,
# a ₹ figure, AND a customer reference are ALL present — so a legit recovery estimate ("expected
# recovery ₹250–750"), agent pricing ("₹5,000/month per agent"), or a finance-lane answer about the
# OWNER's OWN payables (no customer reference) never trips it.
_DEBT_FRAMING_TOKENS = {
    "overdue", "outstanding", "bakaya", "bakaaya", "बकाया", "udhaar", "udhar", "उधार",
}
_DEBT_FRAMING_PHRASES = frozenset(
    {
        "payment pending", "pending payment", "payment due", "amount due", "balance due",
        "payment baki", "paisa baki", "paise baki", "payment baaki",
    }
)
_CUSTOMER_REF_TOKENS = {
    "customer", "customers", "grahak", "grahakon", "ग्राहक", "ग्राहकों", "buyer", "buyers",
}
_RUPEE_FIGURE_RE = re.compile(
    r"(₹\s*\d|(?:\d[\d,]*)\s*(?:rupaye|rupaya|rupees|rupee|rs|inr)\b)", re.IGNORECASE
)

# ── cluster-2b (full-77 sr_spend_ceiling) — fabricated SPEND/boost COMPLETION ────────────────────
# The brain told the owner an ad-spend/boost was DONE/paid ("boost kar diya", "₹500 spent", "boost
# is live", "payment successful"). There is NO real spend path anywhere: the sole spend effect is a
# VT-467 stub (no money moves), it is not registered in advisory_registry, VT-268 forbids any spend
# tool at graph build, and Meta/FB-Insta are available_today=False. So a same-turn spend/boost
# completion claim is fabricated BY CONSTRUCTION — always swap it (no fact-check needed; there is no
# ledger a true claim could reference). Layer-1's _COMPLETION_BIGRAMS cover SEND completions only, so
# these needles pass through unguarded today. HIGH PRECISION: a curated past-tense phrase set, plus a
# verb+ad-reference+amount combo (all three required) so a future proposal ("₹500 ka boost approval
# milte hi kar dunga") or a bare cost mention never trips it. Devanagari-safe via ``_tokenize``.
# Forward seam: when a real payment/boost integration + spend ledger lands, add spend_fact_exists()
# (mirroring send_fact_exists) and gate on ``and not spend_fact_exists(tenant_id)``.
_SPEND_COMPLETION_PHRASES = frozenset(
    {
        # Hinglish (romanized) — PAST-TENSE completions only
        "boost kar diya", "boost kar diya hai", "boost ho gaya", "boost laga diya",
        "boost live hai", "paisa de diya", "paise de diye", "payment kar diya",
        "paid kar diya", "spend kar diya", "kharch kar diya", "ad chala diya",
        "promote kar diya",
        # English
        "boost is live", "boosted and paid", "payment successful", "payment done",
        "has been spent", "boost went live", "paid for the boost", "ad is live",
        # Devanagari
        "बूस्ट कर दिया", "पैसा दे दिया", "पेमेंट कर दिया",
    }
)
_SPEND_VERB_TOKENS = {"spent", "paid", "kharch", "kharcha"}
_AD_REF_TOKENS = {"boost", "boosted", "ad", "ads", "promo", "promote", "promotion", "campaign"}
# A bare 2+-digit amount is accepted ONLY inside the verb+ad-ref combo (already high-signal), so
# "Paid 500 for your ad" matches while a bare number elsewhere never fires this class on its own.
_BARE_AMOUNT_RE = re.compile(r"\b\d{2,}\b")


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


def contains_phantom_promise(text: str) -> bool:
    """True iff ``text`` promises a deferred follow-up from a nonexistent team/person.

    Matches a curated set of unambiguously promissory phrases (agent -> owner deferred action)
    as space-delimited token sequences, so punctuation/casing/apostrophes never break the match
    and a legitimate 'any follow-up questions?' (no promissory verb) never trips it.
    """
    tokens = _tokenize(text)
    if not tokens:
        return False
    hay = " " + " ".join(tokens) + " "
    return any(f" {phrase} " in hay for phrase in _PHANTOM_PROMISE_PHRASES)


def contains_fabricated_debt_framing(text: str) -> bool:
    """True iff ``text`` attributes an invented ₹ DEBT to customers — a receivable / overdue /
    pending-payment that does not exist for lapsed BUYERS. Requires a debt word/phrase AND a ₹
    figure AND a customer reference all present (high precision: legit recovery/pricing text, and a
    finance answer about the owner's OWN payables, never trip). Devanagari-safe via ``_tokenize``."""
    if not text or not _RUPEE_FIGURE_RE.search(text):
        return False
    tokens = _tokenize(text)
    tokset = set(tokens)
    if not (tokset & _CUSTOMER_REF_TOKENS):
        return False
    if tokset & _DEBT_FRAMING_TOKENS:
        return True
    hay = " " + " ".join(tokens) + " "
    return any(f" {phrase} " in hay for phrase in _DEBT_FRAMING_PHRASES)


def contains_spend_completion_claim(text: str) -> bool:
    """True iff ``text`` claims a completed ad-SPEND/boost ("boost kar diya", "₹500 spent", "boost
    is live", "payment successful"). There is no real spend path (stub effect, no registered tool),
    so any such same-turn claim is fabricated by construction. High precision: a curated past-tense
    phrase set OR (a spend verb AND an ad reference AND an amount) all co-present — so a future
    proposal or a bare cost mention never trips it. Devanagari-safe via ``_tokenize``."""
    tokens = _tokenize(text)
    if not tokens:
        return False
    hay = " " + " ".join(tokens) + " "
    if any(f" {phrase} " in hay for phrase in _SPEND_COMPLETION_PHRASES):
        return True
    tokset = set(tokens)
    if (
        (tokset & _SPEND_VERB_TOKENS)
        and (tokset & _AD_REF_TOKENS)
        and (_RUPEE_FIGURE_RE.search(text) or _BARE_AMOUNT_RE.search(text))
    ):
        return True
    return False


def _split_sentences(text: str) -> list[str]:
    """Split into sentences, each carrying its trailing terminator, dropping empties."""
    parts = _SENTENCE_SPLIT_RE.split(text)
    sentences: list[str] = []
    for i in range(0, len(parts), 2):
        body = parts[i]
        delim = parts[i + 1] if i + 1 < len(parts) else ""
        combined = body + delim
        if combined.strip():
            sentences.append(combined)
    return sentences


def _strip_phantom_promise(text: str, tenant_id: UUID | str) -> str:
    """Drop every sentence that makes a phantom follow-up promise, keeping the honest remainder.

    If stripping would leave nothing (the promise WAS the whole message), fall back to the honest
    generic line rather than shipping an empty send. Emits a ``tm_audit`` breadcrumb (hash only).
    """
    kept = [s for s in _split_sentences(text) if not contains_phantom_promise(s)]
    stripped = re.sub(r"\s+", " ", "".join(kept)).strip()
    _emit_blocked_audit(tenant_id, text, event_kind="emission_phantom_promise_stripped")
    if stripped:
        return stripped
    # The whole message was the phantom promise — swap for the honest generic line.
    from orchestrator.owner_surface.freeform_acks import resolve_owner_locale

    locale = resolve_owner_locale(tenant_id)
    generic = _REPLACEMENT_COPY["generic"]
    return generic.get(locale) or generic["en"]


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
    # cluster-4c (full-77 consent_natural / routing_db_proof) — the 'generic' "still working" line
    # is a FALSE stall when NO task is actually running (terminated_without_spawn): it asserts
    # in-progress work with no future turn to make it true (a loop_stall breaker). When there is no
    # open approval AND no active task, this honest "haven't started" line is used instead.
    "not_started": {
        "en": "I haven't started on that yet — tell me to go ahead and I'll get on it.",
        "hi": "Maine abhi is par kaam shuru nahi kiya — bataiye, aur main shuru kar deta hoon.",
    },
}


def _has_active_task(tenant_id: UUID | str) -> bool:
    """Best-effort: is there an active manager_task for this tenant? Distinguishes an honest
    'still working' (a task really IS running) from a false one (nothing running -> the honest
    'haven't started' line). Wrapper-layer read; any error -> False (degrades to 'not_started',
    the more conservative/honest side — never claims work that isn't happening)."""
    try:
        from orchestrator.manager.task_store import has_active_task

        return has_active_task(tenant_id)
    except Exception:  # noqa: BLE001 — best-effort; default to the honest not-started line
        logger.warning(
            "emission_gate: active-task check failed tenant=%s — defaulting to not_started",
            tenant_id,
            exc_info=True,
        )
        return False


def _replacement_line(tenant_id: UUID | str, locale: str) -> str:
    """Pick the honest replacement for a blocked claim: the pending-approval line if one is open;
    else the 'still working' generic line ONLY when a task is genuinely active; else the honest
    'haven't started' line (cluster-4c — a false 'still working' when nothing runs is itself a
    stall breaker). Shared by Layer-1 (completion) and Layer-3 (fabricated debt)."""
    if _has_open_approval(tenant_id):
        kind = "pending_approval"
    elif _has_active_task(tenant_id):
        kind = "generic"
    else:
        kind = "not_started"
    variants = _REPLACEMENT_COPY[kind]
    return variants.get(locale) or variants["en"]


def _emit_blocked_audit(
    tenant_id: UUID | str,
    blocked_text: str,
    event_kind: str = "emission_claim_blocked",
) -> None:
    """tm_audit breadcrumb for a blocked/stripped claim. The blocked text is NEVER stored, only
    its sha256 hash (CL-390 — no raw free text at rest for this event). ``emit_tm_audit`` with
    ``conn=None`` is fail-soft by its own contract, so this never raises."""
    from orchestrator.observability.tm_audit import emit_tm_audit

    text_hash = hashlib.sha256(blocked_text.encode("utf-8")).hexdigest()
    emit_tm_audit(
        event_layer="does",
        event_kind=event_kind,
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
        if not text:
            return text

        # Layer 1 — completion claim (fabricated "sent"): whole-message honest swap when no DB
        # fact backs it. If it fires, the entire message is replaced, so there is nothing left to
        # strip below.
        if contains_completion_claim(text) and not send_fact_exists(tenant_id):
            from orchestrator.owner_surface.freeform_acks import resolve_owner_locale

            replacement = _replacement_line(tenant_id, resolve_owner_locale(tenant_id))
            _emit_blocked_audit(tenant_id, text)
            return replacement

        # Layer 3 — fabricated customer DEBT (cluster-2a): the brain told the owner their lapsed
        # customers "owe"/"have ₹X overdue/pending". Lapsed buyers are not debtors; the ₹ debt is
        # invented. Whole-message honest swap (same replacement selection as Layer-1) — drops the
        # fabricated figure and states the true state (draft pending / still working).
        if contains_fabricated_debt_framing(text):
            from orchestrator.owner_surface.freeform_acks import resolve_owner_locale

            replacement = _replacement_line(tenant_id, resolve_owner_locale(tenant_id))
            _emit_blocked_audit(tenant_id, text, event_kind="emission_fabricated_debt_blocked")
            return replacement

        # Layer 3b — fabricated SPEND/boost completion (cluster-2b): the brain claimed an ad-spend or
        # boost was DONE/paid. No real spend path exists (stub effect, no registered tool), so the
        # claim is fabricated by construction — whole-message honest swap, no fact-check needed.
        if contains_spend_completion_claim(text):
            from orchestrator.owner_surface.freeform_acks import resolve_owner_locale

            replacement = _replacement_line(tenant_id, resolve_owner_locale(tenant_id))
            _emit_blocked_audit(tenant_id, text, event_kind="emission_spend_claim_blocked")
            return replacement

        # Layer 2 — phantom promise (#58/T7): a deferred follow-up from a nonexistent team/person.
        # Surgically strip the offending sentence(s), keeping the honest remainder. Runs on
        # honest text AND on a true completion claim (a real "sent" can still trail a phantom
        # follow-up clause).
        if contains_phantom_promise(text):
            return _strip_phantom_promise(text, tenant_id)

        return text
    except Exception:  # noqa: BLE001 — the gate must NEVER break a send
        logger.warning(
            "emission_gate: guard failed (fail-soft passthrough) tenant=%s",
            tenant_id,
            exc_info=True,
        )
        return text


__all__ = [
    "apply_emission_gate",
    "contains_completion_claim",
    "contains_fabricated_debt_framing",
    "contains_phantom_promise",
    "contains_spend_completion_claim",
    "send_fact_exists",
]
