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
Additional whole-message-swap clusters share the same shape (tight past/present-STATE matcher +
fail-closed DB/state fact-check): cluster-2a fabricated customer DEBT, cluster-2b fabricated
SPEND/boost completion, cluster-3c (VT-655) fabricated CAMPAIGN DRAFT/APPROVAL (fact = a
``campaigns`` row), and cluster-3d (VT-654) premature ONBOARDING-COMPLETE (fact = the deterministic
``conductor.profile_collection_complete``, gated on an ACTIVE journey; swap = the real pending
question). The onboarding cluster is the conductor's "NEVER self-declare complete" invariant made
DETERMINISTIC at the send boundary — the conductor reply reaches the owner through this same gate
(``onboarding_conductor`` routes to END -> ``dispatch._maybe_send_manager_reply`` -> the gate).
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

# VT-657 — a send-STATE completion ("your campaign has gone out", "the offer went out") claims the
# send happened, but it carries NO "sent" token, so the subject+verb bigrams above missed it:
# "your campaign has gone out to everyone" with zero real sends slipped through as a Tier-1
# fabrication (j02). PAST-TENSE send-state bigrams ONLY — the base/future "go out" ("it'll go out
# once you approve") and present-continuous "going out" are deliberately excluded; a preceding
# negation ("hasn't gone out yet") or future/ability marker exempts the occurrence, and a "went out
# OF …" (stock/business) is not a send. The load-bearing gate is still ``send_fact_exists``.
_SEND_STATE_BIGRAMS = {("gone", "out"), ("went", "out")}
# Auxiliary negatives the base _NEGATION set (below) omits — an apostrophe-stripped "hasn't"/"isn't"
# adjacent BEFORE the phrase is a denial, never a completed-state claim.
_SEND_STATE_NEG_AUX = frozenset(
    {"hasnt", "havent", "hadnt", "isnt", "wasnt", "arent", "werent"}
)

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

# ── shared future/conditional guard (VT-654/655 clusters) ─────────────────────────────────────────
# The campaign-draft (cluster-3c) and onboarding-complete (cluster-3d) matchers below key on a
# PAST/PRESENT-STATE claim ("your plan is ready", "onboarding is complete") — a FUTURE or CONDITIONAL
# framing of the same phrase ("once you approve, your plan is ready to send", "I'll set up your
# assistant after this") is an HONEST statement, never a fabrication, and must pass clean. Mirrors the
# _COMPLETION_BIGRAMS future-vs-past discipline: a future/conditional marker appearing BEFORE the
# claim phrase (positional, sentence-scoped) exempts that occurrence. Tight set — apostrophes are
# already stripped by ``_tokenize`` ("I'll" -> "ill", "you'll" -> "youll").
_FUTURE_CONDITIONAL_MARKERS = {
    # future / conditional
    "once", "when", "after", "will", "ill", "well", "youll", "wed", "soon", "then",
    "jab", "baad", "phir", "hone", "milte", "milne", "karne", "karunga", "karungi", "dunga", "dungi",
    # proposal / modal ("shall I draft…", "want me to…", "should I set up…", "let me…")
    "shall", "should", "want", "can", "could", "may", "let", "lets", "chahiye", "karu", "karun",
    # incompleteness framing ("to FINISH setting up…", "BEFORE you're all set…") — the setup is
    # explicitly NOT-yet-done, so the phrase names a GOAL, not a completed state. NB "finished"
    # (a real past completion) is deliberately NOT here, only the infinitive "finish".
    "finish", "finishing", "before",
}

# ── cluster-3c (VT-655, j02 restaurant_festival_marketing) — fabricated CAMPAIGN DRAFT/APPROVAL ──────
# The brain claimed a campaign was DRAFTED and/or reviewed/APPROVED when NO such draft exists. Blind
# judge: "No campaign draft/offer content was ever actually shown to the owner… the assistant invents
# that a concrete plan existed and was reviewed/approved." The authoritative store is the ``campaigns``
# table (collapse.py's ``collapse_campaign_plan`` INSERTs one row per proposed CampaignPlan; status
# progresses proposed -> approved/rejected -> sent/failed). So the fact = a ``campaigns`` row exists
# for this tenant. TIGHT past/present-STATE phrases only (future/proposal is _FUTURE_CONDITIONAL-
# guarded per sentence, so "shall I draft…" / "I'll draft…" / "want me to draft…" never trip). A missed
# phrasing just means the honest message passes (safe); the fact-check is the load-bearing gate.
_CAMPAIGN_CLAIM_PHRASES = frozenset(
    {
        # English — draft/plan EXISTS
        "drafted the campaign", "drafted a campaign", "drafted your campaign",
        "drafted the offer", "drafted the plan", "ive drafted the campaign",
        "i drafted the campaign", "prepared the campaign", "prepared your campaign",
        "put together the campaign", "put together a campaign", "put together your campaign",
        "campaign is ready", "your campaign is ready", "the campaign is ready",
        "plan is ready", "your plan is ready", "the plan is ready",
        "draft is ready", "offer is ready", "the offer is ready",
        "campaign is drafted", "plan is drafted", "offer is drafted",
        # English — reviewed/APPROVED
        "campaign is approved", "plan is approved", "reviewed and approved",
        "already approved the campaign",
        # Hinglish (romanized)
        "campaign taiyaar hai", "campaign tayaar hai", "plan taiyaar hai", "plan tayaar hai",
        "draft taiyaar hai", "offer taiyaar hai", "campaign ready hai", "plan ready hai",
        "campaign draft kar diya", "campaign bana diya", "plan bana diya",
        "campaign taiyaar kar diya", "campaign approve ho gaya", "campaign approve kar diya",
        # Devanagari
        "कैंपेन तैयार है", "प्लान तैयार है", "ड्राफ्ट तैयार है",
        "कैंपेन ड्राफ्ट कर दिया", "कैंपेन बना दिया", "कैंपेन अप्रूव हो गया",
    }
)
# A campaign draft is a PERSISTENT artifact (not a momentary "just sent" action), so the fact window
# is deliberately WIDER than _FACT_WINDOW_MINUTES — a real plan the owner is still discussing may be
# an hour+ old within one conversation. 24h scopes "a plan exists for this recent conversation" while
# still bounded, and strictly REDUCES false-positives (Tier-2) vs a tight 15-min window without
# weakening the target catch: the j02 fabrication tenant has ZERO campaigns in ANY window.
_CAMPAIGN_FACT_WINDOW_MINUTES = 24 * 60

# The adjacency-based phrase set above MISSES a draft-EXISTS claim when a word intervenes between the
# verb and the noun ("I've drafted the Diwali offer for you" — "drafted the offer" is not contiguous).
# This VERB+NOUN combo closes that class at HIGH PRECISION: a PAST-TENSE draft verb (never the base/
# future form — "drafted" yes, "draft" no) CO-PRESENT with a CAMPAIGN-SPECIFIC noun in one sentence,
# with the SAME future/proposal positional guard (a marker before the verb exempts it, so "I'll draft"
# has no "drafted" token and "want me to put together" is marker-exempted). The combo noun set is
# deliberately campaign-SPECIFIC (campaign/offer/promo) — the generic "plan"/"draft" are excluded here
# (they stay in the state-phrase set) so "made a note of your plan" / "reviewed the draft" never trip.
_CAMPAIGN_DRAFT_VERB_TOKENS = {
    # English past-tense completions
    "drafted", "prepared", "readied", "made",
    # Hinglish past "made/prepared" (bare-verb forms; the "bana diya" compound stays a phrase above)
    "banaya", "banayi", "banai",
    # Devanagari past "made"
    "बनाया", "बनाई",
}
_CAMPAIGN_COMBO_NOUN_TOKENS = {
    "campaign", "campaigns", "offer", "offers", "promo", "promotion",
    "कैंपेन", "ऑफर", "ऑफ़र",
}

# ── cluster-3d (VT-654 → VT-656, j05 b2b_onboarding_thin_discovery) — premature ONBOARDING-COMPLETE ──
# The brain falsely declared "that's everything we need… setting up your assistant now" after ONE
# message while profile discovery was NOT deterministically complete (the 'about' gap still pending),
# then step 2 asked for more — a contradiction. VT-654 caught this with an ENUMERATED completion-phrase
# list; VT-656 replaces that with a STATE-DRIVEN structural guard (``_reply_asks_a_question`` +
# ``_onboarding_journey_active`` + ``next_question_for_tenant``) — see the Layer-3d block in
# ``apply_emission_gate``. No phrase list: the authoritative completion state is known deterministically
# BEFORE the reply, so an active+incomplete turn is enforced to ask its pending question REGARDLESS of
# how the (false) completion was phrased (the no-lists whack-a-mole trap the phrase list kept re-opening).


def _tokenize(text: str) -> list[str]:
    """NFC-normalize + casefold + strip apostrophes, then split on whitespace/punct only."""
    normalized = (
        unicodedata.normalize("NFC", (text or "").strip().casefold())
        .replace("'", "")
        .replace("’", "")
    )
    return [t for t in _SPLIT_RE.split(normalized) if t]


# ── R2 (post-14-batch matcher batch) — false-positive guards on the honesty gate ────────────────
# THIS BLOCK LOOSENS the completion/spend matchers with three SENTENCE-SCOPED exemptions. Each is a
# distinct false-positive class the deterministic gate was mis-blocking as a fabrication:
#   (a) a NEGATED send/spend verb ("nahi bheja", "kharch nahi karta") is a DENIAL of the action, not
#       a claim it happened — positional binding, ported verbatim out of approval_reply.py;
#   (b) a message directed TO THE OWNER ("sent you the connect link", "aapko … bhej diya") is about a
#       link/plan the owner receives, not a customer send — voided the moment the sentence also names
#       customers (a "…40 customers reached" clause is a real customer-send claim and MUST still block);
#   (c) a SUBJECT-LESS bigram ("campaign/messages sent") preceded by an ability/future marker
#       ("I can … sent automatically once you approve") is a capability statement, not a completed act.
# Every guard is scoped to ONE sentence: a marker/exemption in sentence 1 can never exempt a bare
# claim in sentence 2 (the merge-blocking invariant). The subject-FUL "I sent"/"maine bheja" assert a
# past act outright and are NEVER owner/marker-exempted.
_NEGATION = {
    "no", "not", "never", "nah", "dont", "wont", "cant", "doesnt", "didnt",
    "नहीं", "ना", "न", "मत", "nahi", "nahin", "mat",
}
# Hinglish/Devanagari send verbs whose ADJACENT negation flips "sent" into "didn't send". English
# "sent" is deliberately excluded: a negated English completion ("not sent") can't form a completion
# bigram anyway (the prefix would be "not", not i/ive/campaign/messages), and excluding it avoids
# mis-reading a trailing new-clause "no" ("campaign sent, no issues") as binding the verb.
_SEND_CLAIM_TOKENS = {"bhej", "bheja", "भेज"}
# Subject-less completion bigrams — the only ones the ability-marker exemption (c) applies to.
_SUBJECTLESS_BIGRAMS = {("campaign", "sent"), ("messages", "sent")}
# Ability / conditional / future markers (apostrophes already stripped: "I'll" -> "ill"). Tight set.
_ABILITY_MARKERS = {"can", "will", "ill", "once", "jab", "karunga", "karungi"}
# Owner pronouns that mark a Hinglish/Devanagari send as directed TO THE OWNER.
_OWNER_DIRECTED_HINGLISH = {"aapko", "tumhe", "tumhein", "आपको", "तुम्हें"}
# VT-640 — owner-FACING artifacts the manager legitimately "sends" to the OWNER (the connect/OAuth
# link, the approval request). "the link I sent" / "the approval I sent" is a delivery to the owner,
# never a customer campaign send — but the ("i","sent") bigram over-matches these when the sentence
# has no explicit "you" ("…using the link I sent, then reply 'done'"), so exemption (b) misses them.
# These are NOT customer-send objects; a real customer send names customers or uses "sent to N".
_OWNER_ARTIFACT_TOKENS = {"link", "links", "invite", "invitation", "oauth", "approval"}
# Spend verbs whose adjacent negation flips a "spent"/"boosted" claim into a denial.
_SPEND_CLAIM_NEG_TOKENS = {"kharch", "kharcha", "spent", "paid", "boost", "boosted"}


def _adjacent_to_negation(token_list: list[str], target_set: set[str], neg_set: set[str]) -> bool:
    """True iff some token in ``target_set`` has an IMMEDIATE neighbor (prev or next token) in
    ``neg_set`` — positional negation-binding ("kharch nahi …" binds the spend verb; a non-adjacent
    "nahi, … kharch kar diya" does not). Mirrors ``owner_inputs/approval_reply.py`` verbatim;
    operates on already-tokenized/NFC-normalized tokens (Devanagari-safe)."""
    for i, t in enumerate(token_list):
        if t in target_set and (
            (i > 0 and token_list[i - 1] in neg_set)
            or (i + 1 < len(token_list) and token_list[i + 1] in neg_set)
        ):
            return True
    return False


def _sentence_is_owner_directed(tokens: list[str]) -> bool:
    """True iff the sentence addresses the OWNER: an English "sent" with "you"/"u" within the next
    ~3 tokens ("I've sent you the link"), or a Hinglish/Devanagari owner pronoun anywhere in it."""
    for i, t in enumerate(tokens):
        if t == "sent" and any(
            tokens[j] in {"you", "u"} for j in range(i + 1, min(i + 4, len(tokens)))
        ):
            return True
    return bool(set(tokens) & _OWNER_DIRECTED_HINGLISH)


def _sentence_has_blocking_completion_claim(sentence: str) -> bool:
    """True iff ONE sentence makes a NON-exempt send-completion claim (the R2 exemptions (a)/(b)/(c)
    above applied within this sentence). A "sent to N" trigram is a customer send by construction —
    never owner-exempt (only the ability-marker/negation guards can clear it)."""
    tokens = _tokenize(sentence)
    if len(tokens) < 2:
        return False
    tokset = set(tokens)
    owner_directed = _sentence_is_owner_directed(tokens)
    has_customer_ref = bool(tokset & _CUSTOMER_REF_TOKENS)
    # (a) — a Hinglish/Devanagari send verb bound to an adjacent negation is a denial, sentence-wide.
    if _adjacent_to_negation(tokens, _SEND_CLAIM_TOKENS, _NEGATION):
        return False
    for i in range(len(tokens) - 1):
        bigram = (tokens[i], tokens[i + 1])
        is_bigram = bigram in _COMPLETION_BIGRAMS
        is_trigram = (
            tokens[i] == "sent"
            and tokens[i + 1] == "to"
            and i + 2 < len(tokens)
            and tokens[i + 2].isdigit()
        )
        if not (is_bigram or is_trigram):
            continue
        # (c) — a subject-less bigram with an ability/future marker BEFORE it is a capability.
        if (
            is_bigram
            and bigram in _SUBJECTLESS_BIGRAMS
            and any(tokens[j] in _ABILITY_MARKERS for j in range(i))
        ):
            continue
        # (b) — owner-directed with no customer reference passes; the trigram never qualifies.
        if owner_directed and not has_customer_ref and not is_trigram:
            continue
        # (d) VT-640 — a subject-ful i/ive "sent" of an owner-FACING artifact (the connect link, the
        # approval) with NO customer reference is a delivery to the OWNER, not a customer send:
        # "…using the link I sent, then reply 'done'" lacks an explicit "you" so (b) misses it. A real
        # customer send names customers (has_customer_ref) or uses the "sent to N" trigram — both still
        # block. Scoped to the i/ive bigrams that over-match this; campaign/messages bigrams unchanged.
        if (
            is_bigram
            and bigram in {("i", "sent"), ("ive", "sent")}
            and not has_customer_ref
            and bool(tokset & _OWNER_ARTIFACT_TOKENS)
        ):
            continue
        return True
    return False


def _sentence_has_send_state_claim(sentence: str) -> bool:
    """VT-657 — True iff ONE sentence claims a send is COMPLETE via a PAST-TENSE send-STATE phrase
    ("campaign has gone out", "the offer went out") — the fabrication class the subject+verb bigrams
    miss (no "sent" token). Exempt when: a negation is adjacent BEFORE ("hasn't gone out yet"), a
    future/ability marker precedes it in the sentence ("once approved it'll have gone out"), or it is
    a "went out OF …" (out of stock/business — not a send). Positional + sentence-scoped, mirroring
    the R2 discipline."""
    tokens = _tokenize(sentence)
    if len(tokens) < 2:
        return False
    for i in range(len(tokens) - 1):
        if (tokens[i], tokens[i + 1]) not in _SEND_STATE_BIGRAMS:
            continue
        # "went/gone out OF <x>" (out of stock/business/town) is not a send.
        if i + 2 < len(tokens) and tokens[i + 2] == "of":
            continue
        prefix = tokens[:i]
        # a negation within the two tokens BEFORE the phrase is a denial, not a completion.
        if set(prefix[-2:]) & (_NEGATION | _SEND_STATE_NEG_AUX):
            continue
        # a future / ability marker anywhere before the phrase -> not a completed state.
        if set(prefix) & (_ABILITY_MARKERS | _FUTURE_CONDITIONAL_MARKERS):
            continue
        return True
    return False


def contains_completion_claim(text: str) -> bool:
    """True iff ``text`` makes a send-COMPLETION claim (EN / Hinglish / Devanagari).

    Deliberately tight: matches adjacent-token bigrams anchored on an explicit send-completion
    phrase, plus the "sent to N" trigram (a count backs the claim), plus (VT-657) a past-tense
    send-STATE phrase ("campaign has gone out"/"went out"). Bare "done" or "sent" alone never
    matches — those are common in perfectly honest replies ("Done!", "I've sent you the plan") that
    make no claim about a customer/campaign send. R2: evaluated per SENTENCE so the owner-directed /
    ability-marker / negation exemptions can never leak across a sentence boundary.
    """
    return any(
        _sentence_has_blocking_completion_claim(s) or _sentence_has_send_state_claim(s)
        for s in _split_sentences(text or "")
    )


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
    # R2 (a) — an adjacent-negated spend verb is a DENIAL ("bina approval ke … kharch nahi karta" =
    # "I never spend without your approval"), not a fabricated completion; never block an honest
    # denial. A NON-adjacent negation ("nahi, ₹500 kharch kar diya") does NOT exempt (positional).
    if _adjacent_to_negation(tokens, _SPEND_CLAIM_NEG_TOKENS, _NEGATION):
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


def _sentence_has_phrase_non_future(
    sentence: str, phrases: frozenset[str], future_markers: set[str]
) -> bool:
    """True iff ONE sentence contains a claim ``phrase`` that is NOT future/conditional-framed.

    A phrase matches as a space-delimited token subsequence against the normalized+tokenized text
    (Devanagari-safe via ``_tokenize``). The occurrence is EXEMPT when a future/conditional marker
    precedes the phrase in the same sentence ("once you tell me X, you're all set" — the "all set"
    is conditional, not a completed state), mirroring the ability-marker-BEFORE discipline the
    completion cluster uses. Positional + sentence-scoped: a marker after the phrase ("You're all
    set — I'll message next") does NOT exempt it."""
    tokens = _tokenize(sentence)
    if not tokens:
        return False
    hay = " " + " ".join(tokens) + " "
    for phrase in phrases:
        idx = hay.find(" " + phrase + " ")
        if idx < 0:
            continue
        prefix_tokens = set(hay[:idx].split())
        if prefix_tokens & future_markers:
            continue  # future/conditional framing — an honest statement, not a completed claim
        return True
    return False


def _sentence_has_draft_verb_claim(sentence: str) -> bool:
    """True iff ONE sentence co-locates a PAST-TENSE draft verb (``_CAMPAIGN_DRAFT_VERB_TOKENS``) with
    a campaign-specific noun (``_CAMPAIGN_COMBO_NOUN_TOKENS``), the verb NOT preceded by a future/
    proposal marker. Catches the intervening-word class the adjacency phrases miss ("I've drafted the
    Diwali offer for you") without loosening: the verbs are past-tense only (so "I'll draft" / "shall
    I draft" carry no matching token), and a marker before the verb ("once I've prepared…") exempts."""
    tokens = _tokenize(sentence)
    if len(tokens) < 2 or not (set(tokens) & _CAMPAIGN_COMBO_NOUN_TOKENS):
        return False
    for i, t in enumerate(tokens):
        if t in _CAMPAIGN_DRAFT_VERB_TOKENS and not (set(tokens[:i]) & _FUTURE_CONDITIONAL_MARKERS):
            return True
    return False


def contains_campaign_draft_claim(text: str) -> bool:
    """True iff ``text`` claims a campaign DRAFT/APPROVAL already EXISTS ("your plan is ready", "I've
    drafted the offer", "I've drafted the Diwali offer for you", "the campaign is approved",
    Hinglish/Devanagari). Two high-precision paths, both future/proposal-guarded per sentence: (a) the
    adjacency STATE phrases; (b) a PAST-tense draft verb + a campaign-specific noun co-present (the
    intervening-word form). A future proposal ("shall I draft…" / "I'll draft…" / "want me to put it
    together…") never trips either. The fact-check (``campaign_draft_fact_exists``) is the load-bearing
    gate; a missed phrasing just passes clean."""
    return any(
        _sentence_has_phrase_non_future(s, _CAMPAIGN_CLAIM_PHRASES, _FUTURE_CONDITIONAL_MARKERS)
        or _sentence_has_draft_verb_claim(s)
        for s in _split_sentences(text or "")
    )


def _reply_asks_a_question(text: str) -> bool:
    """STRUCTURAL (not phrase-matched) test: does the owner-facing reply ADVANCE the turn by asking a
    question? True iff the reply contains an interrogative marker (ASCII ``?`` or fullwidth ``？``;
    Hindi/Devanagari questions use ``?`` too). This is the OVER-FIRE guard for the onboarding Layer-3d
    guard: a reply that already asks something (the good conductor case — it is soliciting the next
    field, so it CANNOT read as "done") is passed through untouched and never degraded / re-asked. A
    reply with NO question during an active+incomplete journey reads as a premature stop (the bug, in
    ANY phrasing) and is swapped for the deterministic pending question. Deliberately over-fire-SAFE:
    a stray ``?`` biases toward pass-through (never degrade a reply that asked), the task's priority."""
    return "?" in (text or "") or "？" in (text or "")


def campaign_draft_fact_exists(tenant_id: UUID | str) -> bool:
    """Did a real campaign draft/plan land for this tenant in the campaign fact window?

    FAIL-CLOSED: any read error returns ``False`` (no confirmed fact), never raises — a DB blip must
    swap the fabricated "your plan is ready" for the honest line, not silently trust it (a
    wrongly-softened honest message is Tier-2; a shipped fabrication is Tier-1). Mirrors
    ``send_fact_exists`` exactly. The ``campaigns`` read goes through ``CampaignsWrapper`` (the
    direct-tenant-DB-access lint requires wrapper-layer SQL — VT-655)."""
    try:
        from orchestrator.db.wrappers import CampaignsWrapper

        return CampaignsWrapper().has_any_since(
            tenant_id, within_minutes=_CAMPAIGN_FACT_WINDOW_MINUTES
        )
    except Exception:  # noqa: BLE001 — fail-closed: treat a read error as "no fact"
        logger.warning(
            "emission_gate: campaign-draft fact read failed tenant=%s — fail-closed (no fact)",
            tenant_id,
            exc_info=True,
        )
        return False


def _onboarding_journey_active(tenant_id: UUID | str) -> bool:
    """Best-effort PRECONDITION for the onboarding-complete cluster: is the tenant IN an active
    onboarding journey? Reuses ``journey.is_active`` (a cheap PK lookup, itself fail-open). Any error
    -> ``False`` — a non-onboarding reply (or a journey-read blip) must NOT have its "all set" swapped
    for an onboarding continuation (that would be a Tier-2 false-positive; the load-bearing honesty
    fact-check lives in ``profile_collection_complete`` and only runs once this precondition holds)."""
    try:
        from orchestrator.onboarding.journey import is_active

        return bool(is_active(tenant_id))
    except Exception:  # noqa: BLE001 — a journey-read error must never swap a non-onboarding reply
        logger.warning(
            "emission_gate: onboarding journey-active check failed tenant=%s — treating inactive",
            tenant_id,
            exc_info=True,
        )
        return False


def _onboarding_incomplete_swap(tenant_id: UUID | str, locale: str) -> str | None:
    """Resolve the honest onboarding SWAP against the DETERMINISTIC completion check (VT-656: called
    on a question-LESS reply inside an active journey — see Layer 3d). Returns the pending-question
    text when the profile is NOT deterministically complete, or ``None`` (pass the reply through)
    when it IS complete.

    ONE call to ``conductor.next_question_for_tenant`` yields BOTH signals: a ``None`` next-question
    means the registry-bounded set is satisfied (profile complete -> the claim is true -> ``None`` =
    pass through); a real next-question means a gap remains (the completion claim is premature -> SWAP
    for the honest continuation that asks that pending question). FAIL-CLOSED: a read error inside an
    active journey means we cannot confirm completeness, so we still swap (the generic honest
    continuation) rather than ship the unverifiable "all set"."""
    try:
        from orchestrator.onboarding.conductor import next_question_for_tenant

        decision = next_question_for_tenant(tenant_id)
        q = decision.next_question
        if q is None:
            return None  # profile IS deterministically complete — the claim is true, pass through
        prompt = (q.prompt_hi if locale == "hi" else q.prompt_en) or ""
        prompt = prompt.strip()
        if prompt:
            return prompt  # the honest next question — ask it instead of falsely declaring "done"
    except Exception:  # noqa: BLE001 — fail-closed below
        logger.warning(
            "emission_gate: onboarding completion fact-check failed tenant=%s — fail-closed swap",
            tenant_id,
            exc_info=True,
        )
    variants = _REPLACEMENT_COPY["onboarding_incomplete"]
    return variants.get(locale) or variants["en"]


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
    # R2 (d) — the fabricated-customer-DEBT block (Layer 3) gets its OWN schema-truth answer, NOT the
    # task-framed "still working"/"haven't started" stall: there is no receivable / due / overdue
    # concept in the schema (lapsed BUYERS, purchase history only), so the honest reply states what
    # IS and ISN'T tracked. This is a SUBSTANTIVE answer (it answers the owner), so it is deliberately
    # NOT part of INTERIM_REPLACEMENT_MARKERS below.
    "receivables": {
        "en": "I don't track customer payments or dues — I only see their purchase history.",
        "hi": (
            "Main customers ke payment ya udhaar track nahi karta — main sirf unki purchase "
            "history dekh sakta hoon."
        ),
    },
    # cluster-3c (VT-655) — a claimed campaign draft/approval with NO backing ``campaigns`` row. This
    # is a SUBSTANTIVE answer (it tells the owner the true state AND offers the real next step), so it
    # is deliberately NOT in INTERIM_REPLACEMENT_MARKERS.
    "campaign_not_drafted": {
        "en": "I haven't drafted that yet — want me to put it together now?",
        "hi": "Maine abhi tak wo draft nahi kiya — bataiye, main abhi bana dun?",
    },
    # cluster-3d (VT-654) — a premature onboarding-COMPLETE claim while profile discovery is not
    # deterministically done. The primary swap is the ACTUAL pending question (from
    # ``next_question_for_tenant``); this generic honest continuation is only the FAIL-CLOSED fallback
    # when that read errors inside an active journey. A SUBSTANTIVE answer -> not an interim stall.
    "onboarding_incomplete": {
        "en": (
            "Before I finish setting up, I need a little more from you — could you tell me a bit "
            "more about your business?"
        ),
        "hi": (
            "Setup poora karne se pehle mujhe thodi aur jaankari chahiye — apne business ke baare "
            "mein thoda aur bata sakte hain?"
        ),
    },
}

# R3 — the gate-swap REPLACEMENT lines that are interim STALLS ("still working" generic / "haven't
# started" not_started), NOT substantive answers. ``owner_surface/task_outcome._is_substantive_owner_
# reply`` excludes these (lowercased substring match) so a gate-blocked stall the brain emitted can
# never count as "the spawning turn was answered" and suppress the honest DF6 async closure — a
# fabrication the gate swapped must not ALSO silence the eventual truthful notice. ``pending_approval``
# and ``receivables`` ARE real answers to the owner, so they are deliberately EXCLUDED from this set.
INTERIM_REPLACEMENT_MARKERS: tuple[str, ...] = tuple(
    line.lower()
    for kind in ("generic", "not_started")
    for line in _REPLACEMENT_COPY[kind].values()
)


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

            # R2 (d) — the honest receivables line (schema truth), NOT the task-framed stall.
            variants = _REPLACEMENT_COPY["receivables"]
            replacement = variants.get(resolve_owner_locale(tenant_id)) or variants["en"]
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

        # Layer 3c — fabricated CAMPAIGN DRAFT/APPROVAL (VT-655, j02): the brain claimed a campaign was
        # drafted/reviewed/approved with NO backing ``campaigns`` row. Claim + no fact -> honest swap
        # ("I haven't drafted that yet — want me to put it together now?"); claim + a real draft row ->
        # pass through unchanged (the claim is true).
        if contains_campaign_draft_claim(text) and not campaign_draft_fact_exists(tenant_id):
            from orchestrator.owner_surface.freeform_acks import resolve_owner_locale

            variants = _REPLACEMENT_COPY["campaign_not_drafted"]
            replacement = variants.get(resolve_owner_locale(tenant_id)) or variants["en"]
            _emit_blocked_audit(tenant_id, text, event_kind="emission_campaign_draft_blocked")
            return replacement

        # Layer 3d — premature ONBOARDING-COMPLETE (VT-654 → VT-656, j05): STATE-DRIVEN, not phrase-
        # matched. The authoritative completion state is known deterministically BEFORE the reply
        # (``next_question_for_tenant``). INVARIANT: while the onboarding journey is ACTIVE and profile
        # collection is INCOMPLETE (a pending question remains), the owner-facing reply MUST ask that
        # pending question and MUST NOT read as "done". Trigger = active journey AND the reply does NOT
        # already ask a question (``_reply_asks_a_question`` — structural, over-fire guard): a reply that
        # already asks something is the good conductor turn (soliciting the next field, cannot read as
        # done) and is passed through untouched; a question-LESS reply during active+incomplete onboarding
        # reads as a premature stop/completion in ANY phrasing (the whack-a-mole a phrase list can't win)
        # and is swapped for the deterministic pending question. ``_onboarding_incomplete_swap`` returns
        # None when the profile is deterministically COMPLETE (next_question None) — a true "all set" then
        # falls through untouched; a NON-onboarding reply (journey inactive) never reaches the swap.
        if _onboarding_journey_active(tenant_id) and not _reply_asks_a_question(text):
            from orchestrator.owner_surface.freeform_acks import resolve_owner_locale

            swap = _onboarding_incomplete_swap(tenant_id, resolve_owner_locale(tenant_id))
            if swap is not None:
                _emit_blocked_audit(
                    tenant_id, text, event_kind="emission_onboarding_incomplete_blocked"
                )
                return swap

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
    "INTERIM_REPLACEMENT_MARKERS",
    "apply_emission_gate",
    "campaign_draft_fact_exists",
    "contains_campaign_draft_claim",
    "contains_completion_claim",
    "contains_fabricated_debt_framing",
    "contains_phantom_promise",
    "contains_spend_completion_claim",
    "send_fact_exists",
]
