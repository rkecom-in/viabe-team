"""VT-85 — day-39 refund-offer reply intake (REFUND / CONTINUE / DISCUSS).

A tenant parked in ``phase=refund_offered`` (the day-39 refund OFFER) replies. This
module classifies that reply DETERMINISTICALLY (Pillar 7 — never let an LLM guess a
financial decision) and acts:

  - REFUND   -> VT-93 ``execute_refund`` (the money moves only now, on consent)
  - CONTINUE -> ``day39_continue`` (90-day suppression) + phase back to paid_active
  - DISCUSS  -> Fazal alert (TELEGRAM_OPS); he reaches out personally; stays refund_offered

An UNCLEAR / ambiguous reply returns None — the caller falls through to the normal
pipeline (so DSR / opt-out still work), and the 48h timeout sweep defaults an
un-answered offer to CONTINUE.

These are PLAIN functions (not @DBOS.step): they call @DBOS.step primitives
(apply_transition, send_template_message) + execute_refund, and a step may not call
another step — so they run in the caller's (webhook_pipeline_run) workflow context.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal
from uuid import UUID, uuid4

from orchestrator.observability.log import log_event

RefundDecision = Literal["refund", "continue", "discuss"]

# Single-token keywords (EN + HI). Whole-token match (Pillar 7: conservative; a
# substring like "refundable" must not fire). Multi-word HI phrases are keyed on
# their decisive token (जारी/रखें = continue, चर्चा = discuss). 'बात' (= "thing/
# talk") was DROPPED — too generic ("क्या बात है" = "what's up?") -> false DISCUSS.
# VT-329 (Cowork adversarial review): "jaari"/"जारी" is AMBIGUOUS in reply to a refund OFFER —
# "jaari karo" can mean "go ahead WITH THE REFUND", not "keep my plan". So continue requires the
# KEEP-BIGRAM (jaari + rakhein/rakho, जारी + रखें/रखो); bare jaari/जारी → no continue (→ None /
# re-ask). "continue" (EN) is unambiguous → single-token. Conservative beats guessing on money.
# ACCEPTED DEVIATION (Cowork conditional-merge): consequently "refund jaari karo" classifies as a
# single-category REFUND affirmative ("go ahead, issue the refund") — semantically correct (jaari
# = "go ahead", refund = the subject), not the block's reflexive None. See the test asserting it.
_REFUND_KW = {"refund", "रिफंड", "रिफ़ंड"}
_CONTINUE_EN = {"continue"}
_CONTINUE_KEEP_STEM = {"जारी", "jaari"}
_CONTINUE_KEEP_VERB = {"रखें", "रखो", "rakhein", "rakho"}
# Bare "baat" deliberately NOT added (same reason बात was dropped — "kya baat hai" = "what's up"
# → false DISCUSS).
_DISCUSS_KW = {"discuss", "चर्चा", "charcha"}

# A reply that NEGATES, QUESTIONS, or signals OPT-OUT/DSR intent is NOT a refund
# decision — a financial decision must never be guessed from a sentence that merely
# contains a keyword. Apostrophes are stripped before matching so contractions
# collapse ("don't" -> "dont", "won't" -> "wont") and the negation set catches them.
_NEGATION = {
    "not",
    "no",
    "never",
    "nah",
    "dont",
    "wont",
    "cant",
    "shouldnt",
    "wouldnt",
    "couldnt",
    "doesnt",
    "didnt",
    "isnt",
    "arent",
    "wasnt",
    "नहीं",
    "मत",
    "ना",
    "न",
    # VT-329: Hinglish (romanized Hindi) negation — "mujhe refund nahi chahiye" / "refund mat do"
    # / "refund na do" must NOT auto-refund. SHORT tokens (na/mat/naa) are SAFE here because
    # matching is token-EXACT (a whole standalone token, never a substring) and the failure
    # direction is refund-SUPPRESSION (fail-safe — a stray match yields None, the 48h CONTINUE
    # default, never a wrong refund). Do NOT "fix" this into substring matching.
    "nahi",
    "nahin",
    "naheen",
    "mat",
    "maat",
    "na",
    "naa",
    # VT-329 (Cowork adversarial review): casual spellings the subagent EXECUTED into a refund.
    # नही (no anusvara) is the commonest casual नहीं; + नहि; romanized nhi/nai/nhin; clipped mt;
    # the नको/nako variant; and the idiomatic decline "rehne/rahne do" ("let it be"). All
    # suppression-only → fail-safe (a stray hit yields None / the 48h CONTINUE default).
    "नही",
    "नहि",
    "नको",
    "nhi",
    "nai",
    "nhin",
    "mt",
    "nako",
    "rehne",
    "rahne",
    # VT-329 (Cowork conditional-merge): residual spellings one step from the block class —
    # nakko (the common romanized-Marathi "no", plausibly MORE frequent than nako), naako, nay,
    # नईं. Suppression-only → fail-safe. "refund nakko" was still executing a refund.
    "nakko",
    "naako",
    "nay",
    "नईं",
}
_INTERROGATIVE = {
    "can",
    "could",
    "would",
    "should",
    "what",
    "why",
    "how",
    "when",
    "which",
    "क्या",
    "कैसे",
    "क्यों",
    "कब",
    "कौन",
}
# Opt-out / DSR intent ALWAYS wins over a refund interpretation (DPDP) — "STOP ...
# refund ...", "delete my data and refund me" must NOT auto-refund. Any of these
# tokens -> None. (The runner gate ALSO bails on the authoritative pre_filter
# opt-out/DSR patterns before this classifier runs — belt + suspenders.)
_OPT_OUT_HINT = {
    "stop", "unsubscribe", "cancel", "quit", "remove", "delete", "erase",
    # VT-329: Hinglish opt-out — "band karo" (stop) / "roko" (stop) / "hatao" (remove). Token-exact
    # standalone; fail-safe (any hit → None, never a refund). pre_filter's opt-out gate also catches
    # these upstream — belt + suspenders.
    "band", "roko", "hatao",
}


def classify_refund_reply(body: str) -> RefundDecision | None:
    """Deterministic keyword-first classify. Returns the decision on a CLEAR,
    UNAMBIGUOUS single-category match, else None (zero matches OR more than one
    category present — never guess; the 48h timeout defaults to CONTINUE)."""
    # Strip apostrophes so contractions collapse (don't -> dont) BEFORE tokenizing,
    # so the negation set matches them — otherwise "don't" -> {don, t} and the
    # 'dont' negation token never fires (the BLOCKER: "don't refund me" -> REFUND).
    normalized = (
        unicodedata.normalize("NFC", (body or "").strip().casefold())
        .replace("'", "")
        .replace("’", "")
    )
    if "?" in normalized:
        return None  # a question is not a decision (Pillar 7 — never guess)
    # Split on whitespace + punctuation ONLY — NOT [^\w], which shatters Devanagari
    # clusters: combining vowel signs (matras ◌ा ◌ी) are not \w, so जारी would split
    # into ज / र and never match. Whitespace/punct split keeps the cluster whole.
    tokens = {t for t in re.split(r"[\s,.!?;:।/\\-]+", normalized) if t}
    if tokens & _NEGATION or tokens & _INTERROGATIVE or tokens & _OPT_OUT_HINT:
        return None  # negation / question / opt-out|DSR intent -> never guess a refund
    matched: list[RefundDecision] = []
    if tokens & _REFUND_KW:
        matched.append("refund")
    # VT-329: continue = EN "continue" OR the keep-BIGRAM (jaari/जारी + rakhein/rakho/रखें/रखो).
    # Bare jaari/जारी alone is ambiguous against a refund offer → NOT continue (re-ask).
    if tokens & _CONTINUE_EN or (tokens & _CONTINUE_KEEP_STEM and tokens & _CONTINUE_KEEP_VERB):
        matched.append("continue")
    if tokens & _DISCUSS_KW:
        matched.append("discuss")
    return matched[0] if len(matched) == 1 else None


def handle_refund_decision(
    tenant_id: UUID | str, decision: RefundDecision, message_sid: str | None
) -> None:
    """Act on a classified refund-offer reply. Audits the decision, then routes."""
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    log_event(
        event_type="day39_refund_decision",
        run_id=uuid4(),
        tenant_id=tid,
        severity="info",
        component="billing",
        payload={"tenant_id": str(tid), "decision": decision, "source": "reply"},
    )

    try:
        if decision == "refund":
            # Consent given — execute the (stubbed) refund. day39_eligibility is the
            # day-39 path's idempotency key; execute_refund flips refund_offered->
            # refunded. execute_refund swallows its OWN vendor failures (partial_failed
            # + Fazal alert); this wrapper is a backstop so an UNEXPECTED error never
            # crashes the inbound webhook pipeline.
            from orchestrator.billing.refund_executor import execute_refund

            execute_refund(tid, "day39_eligibility")
        elif decision == "continue":
            _resume_paid_active(tid, source="reply")
        else:  # discuss
            _alert_discuss(tid)
    except Exception:  # noqa: BLE001 — an action failure must not crash inbound handling
        import logging

        from orchestrator.billing.refund_executor import _alert_fazal

        logging.getLogger(__name__).exception(
            "refund-reply: handling %s failed for tenant=%s", decision, tid
        )
        _alert_fazal(
            f"VT-85 refund-reply: handling {decision!r} for tenant {tid} raised — investigate."
        )


def _resume_paid_active(tenant_id: UUID, *, source: str) -> None:
    """CONTINUE (reply OR 48h timeout): emit day39_continue (the 90-day suppression
    marker the eligibility scan keys on) + transition refund_offered -> paid_active.
    The real ARRR/fees live in the day39_refund_offered event; this is a suppression
    + lifecycle marker, so the analytics fields are 0 placeholders."""
    from orchestrator.state import new_subscriber_state
    from orchestrator.transitions import apply_transition

    log_event(
        event_type="day39_continue",
        run_id=uuid4(),
        tenant_id=tenant_id,
        severity="info",
        component="billing",
        payload={
            "tenant_id": str(tenant_id),
            "verdict": "continue",
            "arrr_paise": 0,
            "cumulative_fees_paise": 0,
            "source": f"day39_offer_continue:{source}",
        },
    )
    try:
        state = new_subscriber_state(tenant_id=tenant_id, run_id=uuid4(), phase="refund_offered")
        apply_transition(state, "day39_continue", {"reason": f"day39_offer_continue:{source}"})
    except Exception:  # noqa: BLE001 — apply_transition may fail outside a DBOS ctx (canary)
        import logging

        logging.getLogger(__name__).exception(
            "refund-reply: continue transition failed tenant=%s; day39_continue event "
            "is the suppression signal",
            tenant_id,
        )


def _alert_discuss(tenant_id: UUID) -> None:
    """DISCUSS: alert Fazal — he personally reaches out to the owner (that human
    contact IS the acknowledgment). Phase stays refund_offered until Fazal resolves.

    No automated owner ack is sent here: any owner-facing copy is Fazal-reviewed
    (Pillar 7) and there is no approved discuss-ack template yet. NEEDS-FAZAL: a
    refund_discuss_ack template (+ the SLA its copy promises) if an automated ack
    is wanted later."""
    from orchestrator.billing.refund_executor import _alert_fazal

    _alert_fazal(
        f"VT-85 refund DISCUSS — tenant {tenant_id} chose to talk. Reach out "
        f"personally (phase stays refund_offered until resolved)."
    )
