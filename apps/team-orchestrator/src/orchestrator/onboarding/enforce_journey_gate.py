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
})

# A setup-status ask: setup-ish token + status-ish cue. Matches "are we set up now?",
# "is the setup done?", "setup ho gaya?" — NOT "why do you need details?" (no setup token).
_SETUP_TOKEN_RE = re.compile(r"\bset\s?up\b|\bsetup\b", re.IGNORECASE)
_STATUS_CUE_RE = re.compile(
    r"\bare we\b|\bis (it|the|everything|my)\b|\bam i\b|\bdone\b|\bcomplete(d)?\b|\bready\b"
    r"|\bnow\b|\bho gaya\b|\bhogaya\b",
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
    return bool(_SETUP_TOKEN_RE.search(text)) and bool(_STATUS_CUE_RE.search(text))


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
            from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

            if matches_opt_out_or_dsr(text):
                return None  # DPDP opt-out/DSR always wins — never consume it here
            if recipient:
                from orchestrator.owner_surface.freeform_acks import (
                    resolve_owner_locale,
                    send_freeform_ack,
                )

                answer = _compose_status_answer(g)
                locale = resolve_owner_locale(tenant_id)
                send_freeform_ack(
                    tenant_id, recipient, answer.get(locale) or answer["en"]
                )
                logger.info(
                    "enforce_journey_gate: setup-status answered deterministically tenant=%s",
                    tenant_id,
                )
                return {"done": g.get("status") != "active", "routed_kind": "journey_status"}
            return None  # no recipient to answer → fall through rather than go silent

        # D — questions (other than the status ask above) go to the brain: the deterministic
        # walker ignores them (the measured privacy-question regression).
        if _is_interrogative(text):
            return None

        # C — a non-interrogative turn while the journey is active (a volunteered/direct answer
        # to the in-flight question) → the walker records it and presents the next question.
        if g is not None and g.get("status") == "active":
            return maybe_handle_journey_reply(tenant_id, text, message_sid, recipient)

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
