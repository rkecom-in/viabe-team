"""VT-83 — weekly-approval reply intake (APPROVE / REJECT), deterministic fast-path.

The owner replies to a `team_weekly_approval` campaign request. Owner approval is
Pillar-7-AUTHORITATIVE: an LLM misreading a Hindi/Hinglish "no" into an approval would
send a campaign the owner REJECTED — customers messaged against the owner's will. So we
classify the UNAMBIGUOUS replies DETERMINISTICALLY here (the same rigor as the VT-85
refund classifier) and only fall through to the Haiku classifier for genuinely ambiguous
text. A clear deterministic signal MUST win over the LLM.

Safety asymmetry (Pillar 7): a false REJECT just doesn't send (the owner re-approves);
a false APPROVE sends a rejected campaign. So ANY negation -> NOT an approval. In
particular a negated send-verb ("don't send", "मत भेजो", "mat bhejo") is a REJECT — unlike
the refund classifier (where a negation returns None), because here "don't send" is a
clear, actionable rejection.

VT-329-safe: NFC-normalize, strip apostrophes (so "don't" -> "dont" matches the negation
set), and split on whitespace/punctuation ONLY — NEVER an ASCII `\\b` or `[^\\w]`, which
shatter Devanagari clusters (matras are not `\\w`).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

ApprovalDecision = Literal["approved", "rejected"]

# Affirmations — rarely negated; their presence alongside a negation is a CONTRADICTION
# (-> ambiguous -> Haiku), not a reject.
_STRONG_APPROVE = {"yes", "approve", "approved", "ok", "okay", "sure", "haan", "हाँ", "जी"}
# Negatable approve verbs — "send it" approves, "don't send" / "मत भेजो" rejects.
_APPROVE_VERB = {"send", "go", "proceed", "bhejo", "भेजो", "theek", "ठीक", "thik"}
# Explicit rejections (non-negation words).
_REJECT_KW = {"reject", "skip", "stop", "cancel"}
# Hedges — a qualified reply ("maybe ok", "perhaps", "शायद") is NOT a clear decision;
# defer to the Haiku classifier (+ its confidence gate) rather than fire deterministically.
_HEDGE = {"maybe", "perhaps", "might", "possibly", "guess", "probably", "शायद"}
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
    tokens = {t for t in re.split(r"[\s,.!?;:।/\\-]+", normalized) if t}

    # A hedged reply ("maybe ok") is not authoritative — defer to Haiku (Pillar 7).
    if tokens & _HEDGE:
        return None

    has_neg = bool(tokens & _NEGATION)
    has_reject_kw = bool(tokens & _REJECT_KW)
    has_strong = bool(tokens & _STRONG_APPROVE)
    has_verb = bool(tokens & _APPROVE_VERB)
    has_contrast = bool(tokens & _CONTRAST)

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
        return "rejected"

    if has_strong or has_verb:
        return "approved"
    return None
