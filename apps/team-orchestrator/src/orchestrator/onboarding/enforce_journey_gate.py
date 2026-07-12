"""T14 — the ENFORCE-mode deterministic journey gate (narrow, speech-act-aware).

Two measured failure modes bound this design (§2 blind judge, onboarding_privacy_skeptic x3):
- VT-609's enforce BYPASS (no gate at all): the brain spawned onboarding_conductor 0/4 turns, so
  the kickoff, the owner's volunteered profile fields, and the setup-status ask all completed
  SILENT → D1 "I'm on it" → ignored_speech_act + loop_stall, fields never recorded (1/2/2).
- The raw VT-367 walker consuming EVERY turn (dcc402f): the deterministic script
  (ONBOARDING_TURN_BRAIN off) IGNORES a question ("why do you need my details?") and re-presents
  the next scripted prompt, and the post-profile flow pitches a Shopify connection at a hardware
  shop — ignored_speech_act + fabrication, WORSE (3/4/3).

So in enforce this gate consumes ONLY the turns the deterministic walker is provably right about:

  A. the exact "Complete Setup" kickoff button → the walker (lazy-start: profile card + first
     question). Token-exact match — zero prose risk.
  B. a setup-STATUS ask ("are we set up now?") → an honest deterministic status composed from the
     journey row itself (remaining count + the pending question, or a plain "profile is set up") —
     never a capability pitch, never a platform assumption.
  C. a NON-interrogative turn while a question is IN-FLIGHT → the walker (records the volunteered
     answer, advances the cursor, presents the next question — the one job it demonstrably does
     well: the volunteered "Sharma Hardware / tools / Karol Bagh" turn recorded correctly).
  D. everything else — questions, post-completion chatter — → None → the brain answers with
     conversational context (the measured-good path for the privacy question).

Legacy/shadow keep the full walker (runner branches on mode). FAIL-OPEN like the legacy gate:
any error → None → the normal pipeline runs. Opt-out/DSR: rule B short-circuits explicitly;
rules A/C inherit ``maybe_handle_journey_reply``'s own internal short-circuit.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# The team_welcome4 quick-reply button text (signup.py) — the journey's kickoff trigger.
_KICKOFF_TOKEN = "complete setup"

# Interrogative lead tokens (EN + Hinglish). A turn is a QUESTION when it carries "?" anywhere or
# opens with one of these — questions route to the brain, never the scripted walker.
_INTERROGATIVE_LEADS = frozenset({
    "why", "what", "whats", "how", "when", "where", "who", "which",
    "can", "could", "would", "will", "should", "do", "does", "did", "is", "are", "am",
    "kya", "kyu", "kyun", "kaise", "kab", "kaun", "kahan", "kidhar",
    # DF7(c) — "how much / how long"-lead Hinglish interrogatives ("kitna time lagega", "kitni der").
    # MANDATORY alongside the duration-cue exclusion below: an ACTIVE journey keeps the confirm live,
    # so a duration question that no longer matches the status-ask must still route to the brain.
    "kitna", "kitni", "kitne",
})

# A setup-status ask: setup-ish token + status-ish cue. Matches "are we set up now?",
# "is the setup done?", "setup ho gaya?" — NOT "why do you need details?" (no setup token).
_SETUP_TOKEN_RE = re.compile(r"\bset\s?up\b|\bsetup\b", re.IGNORECASE)
_STATUS_CUE_RE = re.compile(
    r"\bare we\b|\bis (it|the|everything|my)\b|\bam i\b|\bdone\b|\bcomplete(d)?\b|\bready\b"
    r"|\bnow\b|\bho gaya\b|\bhogaya\b",
    re.IGNORECASE,
)
# DF7(c) — a DURATION question ("how long / how much time will setup take?") carries a setup token AND
# a status cue (e.g. "complete") yet is NOT a status ask — it asks HOW LONG, which the brain answers.
# Exclude it so it falls through to the interrogative rule (→ brain) instead of a canned status line.
_DURATION_CUE_RE = re.compile(
    r"how long|how much time|kitna time|kitna samay|kitni der|kab tak", re.IGNORECASE
)

# DF7(d) — a REMAINING-NEEDS ask ("what else do you need", "aur kuch chahiye"): an interrogative the
# gate OWNS while a journey is active — it is answered honestly from the row (remaining count + the
# pending question), not sent to the brain. Kept narrow (these phrasings only) — NOT interrogative-anywhere.
_REMAINING_NEEDS_RE = re.compile(
    r"\bwhat else\b|\banything else\b|\bwhat more\b|\bwhat other\b"
    r"|aur kuch|kuch aur|और कुछ|कुछ और",
    re.IGNORECASE,
)


def _norm(body: str) -> str:
    return unicodedata.normalize("NFC", (body or "").strip().casefold())


def _is_kickoff(body: str) -> bool:
    return _norm(body) == _KICKOFF_TOKEN


def _is_interrogative(body: str) -> bool:
    text = _norm(body)
    if "?" in text:
        return True
    tokens = [t for t in re.split(r"[\s,.!;:।/\\-]+", text) if t]
    return bool(tokens) and tokens[0] in _INTERROGATIVE_LEADS


def _is_setup_status_ask(body: str) -> bool:
    text = _norm(body)
    if _DURATION_CUE_RE.search(text):
        return False  # DF7(c) — a DURATION ask ("kitna time lagega setup complete…") is not a status ask
    return bool(_SETUP_TOKEN_RE.search(text)) and bool(_STATUS_CUE_RE.search(text))


def _is_remaining_needs_ask(body: str) -> bool:
    """DF7(d) — "what else do you need" / "aur kuch chahiye" (and close variants)."""
    return bool(_REMAINING_NEEDS_RE.search(_norm(body)))


def _current_question(g: dict[str, Any]) -> dict[str, Any] | None:
    """The in-flight question (the queue entry at the cursor), or None past the end / empty queue."""
    queue = list(g.get("question_queue") or [])
    cursor = int(g.get("cursor") or 0)
    return queue[cursor] if 0 <= cursor < len(queue) else None


def _is_confirm_contradiction(body: str) -> bool:
    """DF7(b) — a NON-bare contradiction to a confirm question: carries a negation token but is NOT
    itself a bare "no" (a bare no keeps the walker's good ``_reprompt_after_no``). Such a rich correction
    ("nahi bhai, hum footwear nahi bechte, hum leather bags bechte hain") must reach the brain, not be
    recorded verbatim as the field value by the walker."""
    from orchestrator.onboarding.journey import _NO, _tokens

    toks = _tokens(body)
    return bool(toks & _NO) and not (toks <= _NO)


def _compose_status_answer(g: dict[str, Any]) -> dict[str, str]:
    """Honest, journey-row-grounded status. Never claims readiness beyond the row; never pitches
    a platform (the measured Shopify-assumption fabrication)."""
    if g.get("status") == "active":
        queue = list(g.get("question_queue") or [])
        cursor = int(g.get("cursor") or 0)
        remaining = max(len(queue) - cursor, 0)
        q = queue[cursor] if 0 <= cursor < len(queue) else None
        prompt_en = (q or {}).get("prompt_en", "")
        prompt_hi = (q or {}).get("prompt_hi", "")
        if remaining > 0 and prompt_en:
            return {
                "en": (
                    f"Not quite yet — {remaining} quick detail(s) to go. {prompt_en}"
                ).strip(),
                "hi": (
                    f"अभी थोड़ा बाकी है — {remaining} छोटी जानकारी और। {prompt_hi or prompt_en}"
                ).strip(),
            }
        return {
            "en": "Almost — I'm finishing your profile setup now.",
            "hi": "बस हो ही गया — आपका profile setup पूरा कर रहा हूँ।",
        }
    # completed / anything terminal: state the profile fact only; OFFER (never assume) next steps.
    return {
        "en": (
            "Yes — your business profile is set up. If you'd like, I can help connect "
            "your sales data next."
        ),
        "hi": (
            "हाँ — आपका business profile set हो गया है। चाहें तो अगले कदम में मैं आपका "
            "sales data connect करने में मदद कर सकता हूँ।"
        ),
    }


def _answer_status_from_row(
    tenant_id: UUID | str, text: str, recipient: str | None, g: dict[str, Any], routed_kind: str
) -> dict[str, Any] | None:
    """Send an HONEST, journey-row-grounded answer (rule B setup-status + DF7(d) remaining-needs) and
    return the routed result — else None (fall through the whole gate). Opt-out/DSR ALWAYS wins (→ None,
    to pre_filter); no recipient → None (fall through rather than go silent). Otherwise compose from the
    row (``_compose_status_answer``) + send via the freeform ack seam. Never pitches a platform."""
    from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

    if matches_opt_out_or_dsr(text):
        return None  # DPDP opt-out/DSR always wins — never consume it here
    if not recipient:
        return None  # no recipient to answer → fall through rather than go silent
    from orchestrator.owner_surface.freeform_acks import resolve_owner_locale, send_freeform_ack

    answer = _compose_status_answer(g)
    locale = resolve_owner_locale(tenant_id)
    send_freeform_ack(tenant_id, recipient, answer.get(locale) or answer["en"])
    logger.info(
        "enforce_journey_gate: %s answered deterministically tenant=%s", routed_kind, tenant_id
    )
    return {"done": g.get("status") != "active", "routed_kind": routed_kind}


def _compose_connect_offer(tenant_id: UUID | str, body: str) -> dict[str, str]:
    """DF4 / CD1 (Fazal-binding) — an HONEST post-profile connect offer. A one-tap Shopify OAuth link
    ONLY when the owner has ALREADY named their store domain in-window (they've clearly chosen Shopify,
    so surfacing the link is honest — not an assumption); OTHERWISE a two-option MENU (connect Shopify
    OR share a Google Sheet). NEVER a single-pick Shopify pitch on an owner who never named a store
    (assuming Shopify = fabrication). Both branches are fail-soft — any error degrades to the menu."""
    domain: str | None = None
    try:
        from orchestrator.onboarding.journey import _recent_shop_domain

        domain = _recent_shop_domain(tenant_id, current_body=body)
    except Exception:  # noqa: BLE001 — a courtesy scan; never break the offer
        logger.warning("enforce_journey_gate: shop-domain scan failed (fail-soft)", exc_info=True)
    if domain:
        try:
            from orchestrator.onboarding.shopify_onboarding import start_shopify_setup

            link = start_shopify_setup(tenant_id, domain).get("authorize_url")
            if link:
                return {
                    "en": (
                        f"Great — I found your store {domain}. Tap this secure link to connect "
                        f"(one tap, nothing to copy-paste), then reply 'done':\n{link}"
                    ),
                    "hi": (
                        f"बढ़िया — आपका store {domain} मिल गया। जोड़ने के लिए बस यह सुरक्षित लिंक टैप करें "
                        f"(एक टैप, कुछ copy-paste नहीं), फिर 'done' लिखें:\n{link}"
                    ),
                }
        except Exception:  # noqa: BLE001 — mint failure → fall back to the honest menu (never fabricate)
            logger.warning(
                "enforce_journey_gate: start_shopify_setup failed — falling back to the connect menu",
                exc_info=True,
            )
    return {
        "en": (
            "Happy to connect your sales data. Two easy ways: if you're on Shopify, share your store "
            "address (like yourstore.myshopify.com) and I'll send a one-tap link — or share a Google "
            "Sheet of your sales. Which works for you?"
        ),
        "hi": (
            "आपका sales data connect करने में खुशी होगी। दो आसान तरीके: अगर आप Shopify पर हैं तो अपना "
            "store address (जैसे yourstore.myshopify.com) भेजें, मैं एक-टैप link भेज दूँगा — या अपनी "
            "sales की Google Sheet share करें। आपके लिए क्या ठीक रहेगा?"
        ),
    }


def _maybe_post_profile_connect(
    tenant_id: UUID | str, text: str, recipient: str | None, g: dict[str, Any]
) -> dict[str, Any] | None:
    """DF4 — the POST-PROFILE CONNECT BEAT for a COMPLETED journey still paced in the post-profile flow
    (``__flow__`` = ready_asked / deferred). In enforce mode the walker's paced-flow machine does not
    run, so a clear connect signal here otherwise falls to the async triage stall (the pack-wide
    answer-in-turn root). Consume ONLY the two CLEAR signals — an AFFIRM to the readiness ask, or a
    connect-intent that resumes a DEFERRED flow — and answer IN-TURN with an HONEST connect offer.
    Everything else (questions, declines, chatter) → None → the brain.

    Deliberately NARROW: it does NOT delegate the full ``_maybe_handle_post_profile_flow`` machine
    (whose single-pick Shopify pitch is the measured fabrication). ready_asked uses the deterministic
    AFFIRM FLOOR (``_is_affirm and not _is_decline``) — NOT ``_resolve_readiness_intent`` (whose
    ambiguous→affirm mapping would hijack a question like "why do you need my data?"). deferred uses
    ``_resolve_deferred_intent`` (ambiguous→False, safe)."""
    from orchestrator.onboarding.journey import (
        _FLOW_DEFERRED,
        _FLOW_KEY,
        _FLOW_READY_ASKED,
        _is_affirm,
        _is_decline,
        _resolve_deferred_intent,
    )

    flow = (g.get("answers") or {}).get(_FLOW_KEY)
    if flow == _FLOW_READY_ASKED:
        wants_connect = _is_affirm(text) and not _is_decline(text)
    elif flow == _FLOW_DEFERRED:
        wants_connect = _resolve_deferred_intent(text)
    else:
        return None  # previewed / integration:* / plan_kicked — not a beat this narrow gate owns → brain
    if not wants_connect:
        return None

    # Opt-out/DSR ALWAYS wins — never consume it as a connect signal.
    from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

    if matches_opt_out_or_dsr(text):
        return None

    # An integration handoff already in flight (a LIVE connector-resume step) → DEFER to the downstream
    # connector resume gate (runner), which owns those turns (domain capture, the 'done' DB re-check).
    try:
        from orchestrator.onboarding.shopify_onboarding import has_live_resume

        if has_live_resume(tenant_id):
            return None
    except Exception:  # noqa: BLE001 — a state read must never block; assume no live resume → offer
        logger.warning("enforce_journey_gate: has_live_resume check failed (fail-soft)", exc_info=True)

    if not recipient:
        return None  # nothing to answer to → fall through rather than go silent

    from orchestrator.owner_surface.freeform_acks import resolve_owner_locale, send_freeform_ack

    offer = _compose_connect_offer(tenant_id, text)
    locale = resolve_owner_locale(tenant_id)
    send_freeform_ack(tenant_id, recipient, offer.get(locale) or offer["en"])
    logger.info(
        "enforce_journey_gate: post-profile connect offer sent tenant=%s flow=%s", tenant_id, flow
    )
    return {"done": False, "routed_kind": "journey_connect_offer"}


def maybe_handle_enforce_journey_turn(
    tenant_id: UUID | str, body: str, message_sid: str | None, recipient: str | None
) -> dict[str, Any] | None:
    """Enforce-mode journey gate. Returns a result dict when it handled the turn (caller
    short-circuits the brain), else None. FAIL-OPEN — any error → None."""
    try:
        from orchestrator.onboarding.journey import get_journey, maybe_handle_journey_reply

        text = body or ""
        kickoff = _is_kickoff(text)
        g = get_journey(tenant_id)
        if g is None and not kickoff:
            return None  # no journey row + not the kickoff button → not an onboarding turn

        # A — the kickoff button, FIRST: "Complete Setup" carries both a setup token and a
        # status cue, so it would false-match the status-ask classifier below.
        if kickoff:
            return maybe_handle_journey_reply(tenant_id, text, message_sid, recipient)

        # B — setup-status ask: answer honestly from the row (a question, but a JOURNEY-status
        # question this gate owns; checked before the interrogative fall-through).
        if g is not None and _is_setup_status_ask(text):
            return _answer_status_from_row(tenant_id, text, recipient, g, "journey_status")

        # DF7(d) — REMAINING-NEEDS ask ("what else do you need" / "aur kuch chahiye"): answered from the
        # row (remaining count + pending prompt), BEFORE the interrogative rule D. The ask is
        # interrogative, but it's a JOURNEY-status question this gate owns (like rule B). Active only.
        if g is not None and g.get("status") == "active" and _is_remaining_needs_ask(text):
            return _answer_status_from_row(tenant_id, text, recipient, g, "journey_remaining_needs")

        # D — questions (other than the status/remaining asks above) go to the brain: the deterministic
        # walker ignores them (the measured privacy-question regression). DF7(c) routes a DURATION ask
        # here too (its "kitna"-lead was added to the interrogative set).
        if _is_interrogative(text):
            return None

        # C — a non-interrogative turn while the journey is active. DF7(b): when the in-flight question
        # is a CONFIRM and the turn is a NON-bare contradiction ("nahi bhai, hum footwear nahi bechte,
        # hum leather bags bechte hain"), route to the brain — the walker would otherwise record the
        # whole sentence as the field value. A bare "no" is EXCLUDED (keeps the walker's good
        # _reprompt_after_no). Otherwise the walker records the volunteered/direct answer and advances.
        if g is not None and g.get("status") == "active":
            cur = _current_question(g)
            if cur is not None and cur.get("kind") == "confirm" and _is_confirm_contradiction(text):
                return None
            return maybe_handle_journey_reply(tenant_id, text, message_sid, recipient)

        # DF4 — POST-PROFILE CONNECT BEAT: a COMPLETED journey still paced in the post-profile flow
        # (ready_asked / deferred). A clear connect signal is answered IN-TURN with an honest offer;
        # anything else → None (the brain). This is the beat that, unhandled, fell to the async stall.
        if g is not None and g.get("status") == "complete":
            result = _maybe_post_profile_connect(tenant_id, text, recipient, g)
            if result is not None:
                return result

        # Post-completion non-question chatter → the brain (the post-profile flow's scripted
        # pitch is what fabricated the Shopify assumption — never run it in enforce).
        return None
    except Exception:  # noqa: BLE001 — fail-open: a gate error must never block owner inbound
        logger.exception(
            "enforce_journey_gate: maybe_handle_enforce_journey_turn failed (fail-open) tenant=%s",
            tenant_id,
        )
        return None


__all__ = ["maybe_handle_enforce_journey_turn"]
