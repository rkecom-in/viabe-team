"""VT-83 — weekly-approval reply intake (APPROVE / REJECT), deterministic fast-path.

The owner replies to a `team_weekly_approval` campaign request. Owner approval is
Pillar-7-AUTHORITATIVE: an LLM misreading a Hindi/Hinglish "no" into an approval would
send a campaign the owner REJECTED — customers messaged against the owner's will. So we
classify the UNAMBIGUOUS replies DETERMINISTICALLY here (deterministic-first rigor) and
only fall through to the Haiku classifier for genuinely ambiguous text. A clear
deterministic signal MUST win over the LLM.

Safety asymmetry (Pillar 7): a false REJECT just doesn't send (the owner re-approves);
a false APPROVE sends a rejected campaign. So ANY negation -> NOT an approval. In
particular a negated send-verb ("don't send", "मत भेजो", "mat bhejo") is a REJECT: here
"don't send" is a clear, actionable rejection (a negated decision, not an ambiguous one).

VT-329-safe: NFC-normalize, strip apostrophes (so "don't" -> "dont" matches the negation
set), and split on whitespace/punctuation ONLY — NEVER an ASCII `\\b` or `[^\\w]`, which
shatter Devanagari clusters (matras are not `\\w`).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

ApprovalDecision = Literal["approved", "rejected", "defer"]

# Affirmations — rarely negated; their presence alongside a negation is a CONTRADICTION
# (-> ambiguous -> Haiku), not a reject.
_STRONG_APPROVE = {"yes", "approve", "approved", "ok", "okay", "sure", "haan", "हाँ", "जी"}
# Negatable approve verbs — "send it" approves, "don't send" / "मत भेजो" rejects.
# VT-615: the resume classifier missed bare "bhej do" / "seedha bhej do" (only "haan bhej do"
# resolved, via _STRONG_APPROVE) — "bhejo"/"भेजो" is the "send!" imperative but the very common
# "bhej do" / "भेज दो" ("send it") tokenizes to {"bhej","do"} and matched nothing. Add the "bhej"
# stem + one-word forms. A NEGATED send ("mat bhej do", "मत भेज दो", "nahi bhejna") still REJECTS:
# _NEGATION wins at the has_neg branch below BEFORE this set is consulted (Pillar-7 asymmetry holds).
_APPROVE_VERB = {
    "send", "go", "proceed", "theek", "ठीक", "thik",
    "bhejo", "भेजो", "bhej", "भेज", "bhejdo", "bhejde", "bhejdena",
}
# Explicit rejections (non-negation words).
_REJECT_KW = {"reject", "skip", "stop", "cancel"}
# VT-334 — owner asks to decide LATER (extends the window 48h, max 2, then rejected). EN +
# Hindi + Hinglish. Two shapes:
#   - BARE temporal tokens that mean "later" on their own: "later", "baad (mein)", "बाद (में)".
#   - The NEXT-family ("next", "agle", "अगले") ONLY as the bigram "next WEEK" — bare "next"/"अगले"
#     over-triggers on APPROVING replies ("approve the NEXT campaign", "अगले campaign bhejo"),
#     which would silently delay an explicit approval (Cowork #377 bounce). So next-family defers
#     only when immediately followed by a week-word (incl. the नुक़ता variant हफ़्ते / हफ्ते).
# KNOWN LIMITATION (accepted, Cowork 20260606T103500Z): no negation handling — "send now instead
# of later" classifies defer. That is the FAIL-SAFE direction (delay + re-ask, never an
# unconsented send); VT-329 may revisit. Precedence is reject > defer > approve.
_DEFER_BARE = {"later", "baad", "बाद"}
_DEFER_NEXT = {"next", "agle", "अगले", "अगला"}
_DEFER_WEEK = {"week", "hafte", "hafta", "हफ़्ते", "हफ्ते"}
# T17 (§2 judge x2, sr_no_actual_send 2026-07-11) — a TEMPORAL HOLD negates the TIMING, not the
# draft: "ruk jao, abhi mat bhejna" ("hold on, don't send NOW") / "don't send yet" / "not now"
# was classified REJECT (the bare-negation branch), cancelling the whole draft the owner only
# paused — the decline closure then read as ignoring their actual instruction. Negation + one of
# these NOW/YET tokens (with NO explicit reject keyword and NO finality token) classifies DEFER:
# the approval stays pending (nothing sends — same money-safety as reject), the window extends,
# and the ask re-surfaces (VT-334 machinery, _MAX_DEFERS-bounded so it can never nag forever).
_TEMPORAL_HOLD = {"abhi", "अभी", "now", "yet", "filhal", "filhaal", "फिलहाल", "फ़िलहाल"}
# Finality tokens defeat the temporal read: "don't send now or EVER" / "kabhi nahi" is a REJECT.
# ("never" is already a _NEGATION member; listing it here keeps the reject read explicit.)
_FINALITY = {"ever", "never", "kabhi", "कभी"}
# Cluster-1 (full-77 §2 judge 3/3, sr_consequential_bulk_send 2026-07-12) — a NEGATED HOLD-word is a
# PROCEED, not a reject: "wait mat karo, bhej do" ("don't wait, send it") negates the *waiting*, not
# the send. The bare-negation branch read `mat` and REJECTED, false-declining an armed campaign the
# owner was urgently pushing to SEND. Distinct from _TEMPORAL_HOLD ("abhi mat bhejna" = don't send
# NOW -> DEFER): there the SEND is negated; here the HOLD is. The two are told apart POSITIONALLY
# (`_adjacent_to_negation`), never by bag-of-words — "mat bhejo ruk jao" (don't send, stop) has the
# same token set but the negation binds the send verb, so it must stay a REJECT (money asymmetry).
_NEGATED_HOLD = {"wait", "ruk", "ruko", "ruko", "roko", "rukna", "रुक", "रुको", "रोको", "hold"}
# Cluster-1b (full-77 §2, sr_owner_cannot_bypass_approval 2026-07-12) — a long free-text sentence is
# not a button-press. A 25-token standing-permission ask ("aap ko baar baar permission ... zaroorat
# NAHI hai, aage se khud decide karke bhej diya karo") carries an INCIDENTAL negation (`nahi` binds
# "zaroorat", not the send) and was DETERMINISTICALLY REJECTED, false-declining a campaign the owner
# never declined. Legit approve/reject/defer replies top out ~10-11 tokens; anything longer routes to
# the reasoning layer (return None — money-safe: None NEVER auto-approves, leaves the row paused).
_MAX_DECISION_TOKENS = 12


def _adjacent_to_negation(token_list: list[str], target_set: set[str], neg_set: set[str]) -> bool:
    """True iff some token in ``target_set`` has an IMMEDIATE neighbor (prev or next token) in
    ``neg_set``. Positional negation-binding: "mat bhejo" (neg before send) binds the send verb;
    "wait mat" (neg after hold) binds the hold-word. Bag-of-words membership cannot tell these
    apart — adjacency can. Devanagari-safe (operates on already-tokenized/NFC-normalized tokens)."""
    for i, t in enumerate(token_list):
        if t in target_set and (
            (i > 0 and token_list[i - 1] in neg_set)
            or (i + 1 < len(token_list) and token_list[i + 1] in neg_set)
        ):
            return True
    return False
# Hedges — a qualified reply ("maybe ok", "perhaps", "शायद") is NOT a clear decision;
# defer to the Haiku classifier (+ its confidence gate) rather than fire deterministically.
# VT-633 — the LATIN-script Hinglish hedges were missing: "shayad theek hai" ("maybe it's ok")
# tokenized to a bare approve-verb hit ("theek") and classified APPROVED — a hedged non-decision
# one step from an unconsented send. "dekhte" ("dekhte hain" = "let's see") is the other common
# defer-flavored hedge; both now push the reply to the Haiku classifier, never a deterministic
# approve. (Devanagari शायद was already here; the transliterations weren't.)
_HEDGE = {
    "maybe", "perhaps", "might", "possibly", "guess", "probably",
    "शायद", "shayad", "shaayad", "sayad", "dekhte", "देखते",
}
# Vague RESUME / continue references — "do what you were saying", "that same thing", "carry on".
# Money-safety (Pillar 7, official §2 2026-07-10): a reply whose ONLY affirmative signal is a
# generic ack ("ok"/"theek") wrapped in a back-reference to prior context is NOT an unambiguous
# decision on the SPECIFIC pending action. The m_conversation_interruption breaker: "ok theek hai,
# chalo jo pehle bol raha tha wahi karo" ("ok fine, do what you were saying before") tokenized to a
# bare approve-hit ("ok"/"theek") and DETERMINISTICALLY APPROVED — auto-sending an un-approved
# winback. Mirrors the _HEDGE guard: push such a reply to None so it never deterministically approves
# (for a customer-send, resolve_decision_from_reply then re-asks rather than sends). An EXPLICIT send
# verb OVERRIDES ("chalo bhej do" = "come on, send it" is a real approval).
_VAGUE_RESUME = {"wahi", "wahin", "continue", "resume"}
_RESUME_BACKREF = (
    "bol raha tha", "bol rahe the", "keh raha tha", "keh rahe the",
    "bata raha tha", "what you were saying", "what i said before",
    "carry on", "as before", "jo pehle",
)
# The SEND-specific verbs (subset of _APPROVE_VERB) — an explicit send instruction overrides a
# vague-resume back-reference. Excludes the weak/generic proceed words (theek/go/proceed/ok).
_EXPLICIT_SEND = {"send", "bhejo", "भेजो", "bhej", "भेज", "bhejdo", "bhejde", "bhejdena"}
# Contrastive conjunctions — a GENUINE two-clause contradiction ("yes BUT don't send the
# discount one") defers to Haiku. A BARE negation of an approve-word ("do not approve")
# is NOT a contradiction — it is a deterministic reject (Cowork VT-83 #345 bounce).
_CONTRAST = {"but", "however", "though", "lekin", "par", "मगर", "लेकिन", "पर"}
# Negations (EN contractions collapse after apostrophe-strip; EN + HI + Hinglish).
_NEGATION = {
    "no",
    "not",
    "never",
    "nah",
    "dont",
    "wont",
    "cant",
    "doesnt",
    "didnt",
    "नहीं",
    "ना",
    "न",
    "मत",
    "nahi",
    "nahin",
    "mat",
}


def classify_approval_reply(body: str) -> ApprovalDecision | None:
    """Deterministic APPROVE/REJECT on a CLEAR reply, else None (-> Haiku fallback; never
    auto-approve on uncertainty — Pillar 7).

    - explicit reject word, or a negation WITHOUT a strong affirmation -> rejected
      (covers "no", "नहीं", "skip", "don't send", "मत भेजो", "mat bhejo").
    - strong affirmation or an approve-verb, with NO negation/reject -> approved.
    - a negation AND a strong affirmation together (contradiction), or both an approve and
      a reject signal, or neither -> None (ambiguous; let the Haiku classifier read it).
    - any '?' -> None (a question is not a decision).
    """
    normalized = (
        unicodedata.normalize("NFC", (body or "").strip().casefold())
        .replace("'", "")
        .replace("’", "")
    )
    if "?" in normalized:
        return None
    token_list = [t for t in re.split(r"[\s,.!?;:।/\\-]+", normalized) if t]
    tokens = set(token_list)

    # Cluster-1b — a paragraph is not a button-press. A long free-text reply (a standing-permission
    # ask, a rambling instruction) carries incidental approve/negation tokens that the bag-of-words
    # matcher misreads; route anything longer than a clear approve/reject/defer to the reasoning
    # layer (None — money-safe, never auto-approves, leaves any armed row paused). Placed FIRST so no
    # incidental token below fires deterministically on a long message.
    if len(token_list) > _MAX_DECISION_TOKENS:
        return None

    # A hedged reply ("maybe ok") is not authoritative — defer to Haiku (Pillar 7).
    if tokens & _HEDGE:
        return None

    has_neg = bool(tokens & _NEGATION)
    has_reject_kw = bool(tokens & _REJECT_KW)
    has_strong = bool(tokens & _STRONG_APPROVE)
    has_verb = bool(tokens & _APPROVE_VERB)
    has_contrast = bool(tokens & _CONTRAST)
    has_explicit_send = bool(tokens & _EXPLICIT_SEND)
    # Positional negation-binding (cluster-1): is the negation on the SEND verb ("mat bhejo" = don't
    # send -> stop) or on a HOLD word ("wait mat" = don't wait -> proceed)?
    negated_send = _adjacent_to_negation(token_list, _EXPLICIT_SEND, _NEGATION)
    negated_hold = _adjacent_to_negation(token_list, _NEGATED_HOLD, _NEGATION)
    # Bare temporal defer, OR the next+week bigram (adjacent tokens) — never bare next/अगले.
    has_week_bigram = any(
        token_list[i] in _DEFER_NEXT and token_list[i + 1] in _DEFER_WEEK
        for i in range(len(token_list) - 1)
    )
    has_defer = bool(tokens & _DEFER_BARE) or has_week_bigram

    # GENUINE two-clause contradiction ONLY (a contrastive conjunction joins an
    # affirmation + a negation: "yes BUT don't send the discount one") -> ambiguous ->
    # Haiku. A BARE negation of an approve-word ("do not approve", "not ok", "won't
    # approve") is NOT a contradiction — it falls through to the deterministic reject
    # below. Pillar 7: the clearest rejection an owner can type must never ride on the LLM.
    if has_neg and has_strong and has_contrast:
        return None

    # A negation (incl. negating an approve-word: "do not approve") or an explicit reject
    # word -> REJECT. The negation binds the approval — never send a negated reply.
    if has_neg or has_reject_kw:
        # Cluster-1 — a negated HOLD-word with an un-negated explicit send verb ("wait mat karo, bhej
        # do" = "don't wait, send it") is NOT a reject: the negation binds the WAITING, not the send,
        # so the bare-negation reject below would false-DECLINE a campaign the owner is pushing to
        # send. But it is ALSO not a deterministic APPROVE: dev §2 validation (sr_consequential_bulk,
        # 2026-07-12) proved that auto-approving an impatient "jaldi karo, bhej do" AUTO-SENT a
        # consequential batch on impulse pressure — a money_action breaker. Money asymmetry: the
        # money-safe resolution is NEITHER reject NOR approve -> return None, so the turn falls to the
        # brain to RE-CONFIRM the send explicitly (no false decline, no impulse auto-send). Fires only
        # when the send verb is not itself adjacent-negated ("mat bhejo ruk jao" stays a reject) and
        # there is no temporal-hold ("abhi" -> defer below) or finality token.
        if (
            has_neg
            and not has_reject_kw
            and negated_hold
            and has_explicit_send
            and not negated_send
            and not (tokens & _TEMPORAL_HOLD)
            and not (tokens & _FINALITY)
        ):
            return None
        # T17 — temporal hold: "abhi mat bhejna" / "don't send now" / "not yet" pauses the
        # send, it does not decline the draft. DEFER (still no send; window extends; re-asks;
        # _MAX_DEFERS-bounded). Never fires on an explicit reject keyword ("no, cancel it now")
        # or a finality token ("don't send now or ever", "kabhi nahi") — those stay REJECT.
        # A contradictory "no, send it now" also lands here → defer → RE-ASK, which is safer
        # than either deterministic misread of a self-contradicting reply.
        if (
            has_neg
            and not has_reject_kw
            and bool(tokens & _TEMPORAL_HOLD)
            and not (tokens & _FINALITY)
        ):
            return "defer"
        return "rejected"

    # VT-334 — defer beats approve (reject > defer > approve): "ok but later" defers the timing
    # rather than sending now. A negation above already won as reject, so defer is only reached
    # on a non-negated reply.
    if has_defer:
        return "defer"

    # Vague-resume guard (money-safety): a "do what you were saying / continue / that same thing"
    # back-reference whose only affirmative signal is a generic ack ("ok"/"theek") is NOT an
    # unambiguous approval of the SPECIFIC pending action — return None (Haiku layer / re-ask). An
    # EXPLICIT send verb present overrides ("chalo bhej do" approves). Reached only on a non-negated,
    # non-reject, non-defer reply, so it never weakens a reject (Pillar-7 asymmetry holds).
    has_resume = bool(tokens & _VAGUE_RESUME) or any(p in normalized for p in _RESUME_BACKREF)
    if has_resume and not has_explicit_send:
        return None

    if has_strong or has_verb:
        return "approved"
    return None


def is_resume_cue(body: str) -> bool:
    """T8 — True iff the reply is a bare "proceed / do what you were saying / continue" cue with NO
    explicit send verb, negation, reject, or question.

    This is the SAME condition ``classify_approval_reply`` treats as a vague resume (it returns
    None there, for money-safety — T5): an EXPLICIT send verb ("chalo bhej do") is an approval, a
    negation/reject is a stop, a "?" is a question — none of those are a resume cue. The runner
    (T8) uses this to RE-SURFACE an already-armed approval instead of letting the turn fall through
    to new_task and spawn a COMPETING plan. Kept in lockstep with the classifier's normalization +
    guards above so the two never diverge."""
    normalized = (
        unicodedata.normalize("NFC", (body or "").strip().casefold())
        .replace("'", "")
        .replace("’", "")
    )
    if "?" in normalized:
        return False
    tokens = {t for t in re.split(r"[\s,.!?;:।/\\-]+", normalized) if t}
    if tokens & _NEGATION or tokens & _REJECT_KW:
        return False
    has_resume = bool(tokens & _VAGUE_RESUME) or any(p in normalized for p in _RESUME_BACKREF)
    has_explicit_send = bool(tokens & _EXPLICIT_SEND)
    return has_resume and not has_explicit_send
