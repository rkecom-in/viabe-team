"""VT-648 — LLM-primary send-intent classifier + thin deterministic hard-stop veto (the MONEY GATE).

Fazal STANDING CL-2026-07-15-no-lists-for-undefined-possibilities: do NOT hardcode/manage a list of
the ways a human phrases "send" — that is an INFINITE set that always lags reality (the F1 keyword
interim was REJECTED by its own adversarial verify: a keyword matcher cannot tell ``bhej du`` = "send
it" from ``kya bhej du`` = "should I send?"). The LLM DECIDES intent; deterministic code only VETOES
in the SAFE direction (force reject/hold, NEVER approve).

Fail-safe asymmetry (the money invariant, non-negotiable): a false REJECT/HOLD just re-asks the owner
(harmless). A false APPROVE fires an irreversible unconsented customer campaign (catastrophic).
Therefore EVERY uncertain path — low confidence, any veto, an ungrounded cue, an LLM/JSON error, an
empty reply, no owner-inputs consent — resolves to ``None`` (re-ask), NEVER to ``"approved"``.

Flag ``TEAM_SEND_INTENT_LLM`` (three states, fail-closed to ``off`` — same posture as
``manager/loop_mode.py``):
  - ``off``     — DEFAULT. Pure existing deterministic path; this module never runs; behavior is
                  byte-for-byte the pre-VT-648 ``resolve_decision_from_reply``.
  - ``shadow``  — the deterministic path STILL DECIDES; the LLM runs and its decision is LOGGED
                  alongside the deterministic one for comparison. No behavior change, no second effect.
  - ``enforce`` — the LLM + hard-stop veto DECIDE the customer-send gate.

Grounding (anti-hallucination): the LLM MUST cite a ``cited_cue`` that is a verbatim substring of the
owner reply. An ungrounded cue → treated as a failure → ``None`` (re-ask).

This module keeps every heavy import (the LLM seam, pydantic, pre_filter_gate) LOCAL so importing it
is dep-less-smoke safe, and reuses the EXISTING positional negation-binding primitive from
``approval_reply`` — it introduces NO positive send-word list.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Literal, get_args

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------------------------------
# Flag — TEAM_SEND_INTENT_LLM (three-state, fail-closed to 'off'), mirrors manager/loop_mode.py.
# ---------------------------------------------------------------------------------------------------
SendIntentMode = Literal["off", "shadow", "enforce"]
_VALID_MODES: tuple[SendIntentMode, ...] = get_args(SendIntentMode)
_ENV_VAR = "TEAM_SEND_INTENT_LLM"


def get_send_intent_mode() -> SendIntentMode:
    """Read ``TEAM_SEND_INTENT_LLM`` — fail-closed to ``'off'`` on anything unrecognized (unset,
    empty, a typo, or a not-yet-supported value). A mode flip must not change behavior mid-turn; the
    caller reads this ONCE per turn (a single turn resolves the reply exactly once)."""
    raw = os.environ.get(_ENV_VAR, "off").strip().lower()
    if raw in _VALID_MODES:
        return raw  # narrowed str -> SendIntentMode via the tuple-of-literals membership check
    return "off"


def is_send_intent_off(mode: SendIntentMode | None = None) -> bool:
    return (mode if mode is not None else get_send_intent_mode()) == "off"


def is_send_intent_shadow(mode: SendIntentMode | None = None) -> bool:
    return (mode if mode is not None else get_send_intent_mode()) == "shadow"


def is_send_intent_enforce(mode: SendIntentMode | None = None) -> bool:
    return (mode if mode is not None else get_send_intent_mode()) == "enforce"


# ---------------------------------------------------------------------------------------------------
# Tuning constants.
# ---------------------------------------------------------------------------------------------------
# The send-intent classifier runs on the "complex" tier (TEAM_MODEL_COMPLEX; default claude-sonnet-5)
# — the SAME manager gate-classifier tier as ``manager/triage.py``. Chosen (not invented): triage is
# the manager's primary intent classifier, and send-intent disambiguation (``bhej du`` vs ``kya bhej
# du``) is exactly the nuanced Hinglish intent read the classifier ("classifier"/haiku) tier proved
# too weak for. A stronger tier LOWERS the risk of a CONFIDENT mis-approve — the only failure the
# money invariant cannot tolerate; every other error direction fails safe to None.
_SEND_INTENT_TIER = "complex"

# Approve floor: an ``approve`` only stands at/above this confidence — below it, HOLD (re-ask). The
# prompt instructs the model to keep approve >= 0.8; this code floor is set slightly lower (0.7) so a
# borderline-but-committed clear send is not double-penalized, while a hedged low-confidence approve
# still falls to hold. NEVER relax this above the prompt threshold.
_SEND_INTENT_APPROVE_MIN_CONFIDENCE = 0.7

_MAX_REPLY_CHARS = 4000  # transmit guard (mirrors ClassifyOwnerMessageInput.text max_length)

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "send_intent_v1.md"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# Markdown code-fence stripper — the model may wrap the JSON in a ```json … ``` fence despite the
# prompt asking for bare JSON. NARROW: unwraps only a recognised outer fence, never field VALUES
# (mirrors classify_owner_message._CODE_FENCE_RE).
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?[ \t]*\n?(?P<body>.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE
)

SendDecision = Literal["approve", "reject", "hold"]


class SendIntentResult:
    """The LLM's structured send-intent verdict. Plain object (no pydantic at module import — kept
    dep-less-smoke safe); validated in ``_parse_result``."""

    __slots__ = ("decision", "cited_cue", "confidence", "grounded")

    def __init__(self, decision: SendDecision, cited_cue: str, confidence: float, grounded: bool):
        self.decision = decision
        self.cited_cue = cited_cue
        self.confidence = confidence
        self.grounded = grounded

    def __repr__(self) -> str:  # PII-safe: cited_cue is a fragment of the owner reply — NEVER logged
        return (
            f"SendIntentResult(decision={self.decision!r}, confidence={self.confidence:.2f}, "
            f"grounded={self.grounded})"
        )


def _normalize(text: str) -> str:
    """NFC + casefold + collapse-whitespace + strip. The SAME NFC/apostrophe posture as
    ``approval_reply`` (VT-329-safe), plus whitespace collapse so the grounding substring check
    tolerates trivial spacing/case variance without letting a genuine hallucination through."""
    n = unicodedata.normalize("NFC", (text or "")).casefold().replace("'", "").replace("’", "")
    return re.sub(r"\s+", " ", n).strip()


def _is_grounded(cited_cue: str, reply_text: str) -> bool:
    """True iff ``cited_cue`` is a verbatim substring of the owner reply (both NFC/casefold/
    whitespace-normalized). A non-empty cue that is NOT present is a hallucination → not grounded."""
    cue = _normalize(cited_cue)
    if not cue:
        return False
    return cue in _normalize(reply_text)


def _parse_result(raw: str, reply_text: str) -> SendIntentResult | None:
    """Fence-strip + json.loads + validate the model envelope. Returns None (money-safe) on ANY
    malformed / non-conforming output — never raises into the decision path."""
    stripped = raw.strip()
    match = _CODE_FENCE_RE.match(stripped)
    if match is not None:
        stripped = match.group("body").strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        logger.warning("send_intent: model returned non-JSON (money-safe hold)")
        return None
    if not isinstance(parsed, dict):
        return None
    decision = parsed.get("decision")
    if decision not in get_args(SendDecision):
        return None
    cited_cue = parsed.get("cited_cue")
    if not isinstance(cited_cue, str):
        cited_cue = ""
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    grounded = _is_grounded(cited_cue, reply_text)
    return SendIntentResult(
        decision=decision, cited_cue=cited_cue, confidence=confidence, grounded=grounded
    )


def classify_send_intent(
    text: str,
    *,
    tenant_id: str,
    text_call: object | None = None,
    consent_check: object | None = None,
) -> SendIntentResult | None:
    """LLM-primary send-intent read. Returns a validated ``SendIntentResult`` or ``None`` (money-safe)
    on: no owner-inputs consent, an empty/oversized reply, an LLM/transport error, malformed JSON, a
    non-conforming envelope. NEVER raises into the caller — every failure is a money-safe ``None``.

    ``consent_check`` (default the owner_inputs gate) mirrors ``classify_owner_message``: the raw body
    transmits to a sub-processor, so it is gated fail-closed on the owner_inputs consent basis.
    ``text_call`` defaults to the multi-provider seam ``structured_text_call``; tests inject a stub.
    """
    body = (text or "").strip()
    if not body or len(body) > _MAX_REPLY_CHARS:
        return None

    # Consent gate — BEFORE any transmit (CL-425/CL-390). Fail-closed on a bad tenant_id / check
    # error / no consent → None (re-ask; the body is never sent).
    if consent_check is None:
        try:
            from orchestrator.memory.l0_writer import _owner_inputs_enabled

            consent_check = _owner_inputs_enabled
        except Exception:  # noqa: BLE001 — cannot resolve the gate → fail-closed
            logger.info("send_intent: consent gate unresolvable; holding (fail-closed)")
            return None
    try:
        from uuid import UUID

        allowed = consent_check(UUID(str(tenant_id)))  # type: ignore[operator]
    except Exception:  # noqa: BLE001 — any failure checking consent → fail-closed hold
        logger.info("send_intent: consent check failed; holding (fail-closed)")
        return None
    if not allowed:
        logger.info("send_intent: owner_inputs disabled; holding (no transmit)")
        return None

    if text_call is None:
        try:
            from orchestrator.llm.structured import structured_text_call

            text_call = structured_text_call
        except Exception:  # noqa: BLE001 — seam import failure → money-safe hold
            logger.warning("send_intent: LLM seam import failed; holding (money-safe)")
            return None

    try:
        raw = text_call(  # type: ignore[operator]
            _SEND_INTENT_TIER,
            system=_SYSTEM_PROMPT,
            user=body,
            max_tokens=200,
            agent="classify_send_intent",
            call_site="classify_send_intent",
            tenant_id=tenant_id,
        )
    except Exception:  # noqa: BLE001 — ANY LLM/transport error → money-safe hold, never approve
        logger.warning("send_intent: LLM call failed; holding (money-safe)", exc_info=True)
        return None

    return _parse_result(str(raw or ""), body)


# ---------------------------------------------------------------------------------------------------
# Thin deterministic hard-stop VETO — fires ONLY in the SAFE direction (force reject/hold, never
# approve). NO positive send-word list (CL-2026-07-15): it reuses the EXISTING positional negation-
# binding primitive from approval_reply (a negation bound to the send verb), plus opt-out/DSR.
# ---------------------------------------------------------------------------------------------------
VetoResult = Literal["rejected", "hold"]


def send_intent_hard_stop(text: str, *, tenant_id: str | None = None) -> VetoResult | None:
    """Deterministic veto. Returns:
      - ``"rejected"`` — a NEGATION is bound to the send verb ("mat bhejo", "don't send", "मत भेजो").
        The clearest stop an owner can type must never ride on the LLM (Pillar-7 asymmetry).
      - ``"hold"``     — an opt-out / DSR / global send-stop appears in the reply: never resolve THAT
        as a customer send; re-ask (belt-and-suspenders — the runner also guards this upstream).
      - ``None``       — no hard stop; the LLM decides.

    It can NEVER return an approval. Reuses ``_adjacent_to_negation(_EXPLICIT_SEND, _NEGATION)`` — the
    same positional binding the deterministic classifier uses — so "mat bhejo" rejects but "wait mat
    karo, bhej do" (negation binds the HOLD, not the send) is NOT vetoed here (the LLM reads it)."""
    from orchestrator.owner_inputs.approval_reply import (
        _EXPLICIT_SEND,
        _NEGATION,
        _adjacent_to_negation,
    )

    normalized = (
        unicodedata.normalize("NFC", (text or "").strip().casefold())
        .replace("'", "")
        .replace("’", "")
    )
    token_list = [t for t in re.split(r"[\s,.!?;:।/\\-]+", normalized) if t]
    if _adjacent_to_negation(token_list, _EXPLICIT_SEND, _NEGATION):
        return "rejected"

    # Opt-out / DSR / global send-stop: never resolve as a send. Fail-soft (a matcher import/eval
    # error must not crash the money gate — degrade to "no veto", the LLM still reads it money-safe).
    try:
        from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

        if matches_opt_out_or_dsr(text or ""):
            return "hold"
    except Exception:  # noqa: BLE001
        logger.warning("send_intent: opt-out veto check failed (degrading to LLM read)", exc_info=True)
    return None


def decide_send_intent_enforce(
    text: str, *, tenant_id: str, text_call: object | None = None, consent_check: object | None = None
) -> str | None:
    """ENFORCE decision for a customer-SEND approval: the hard-stop veto + LLM own the gate.

    Returns ``"approved"`` | ``"rejected"`` | ``None`` (None = HOLD / re-ask, leave the row paused).
    The money invariant is enforced structurally — the ONLY path to ``"approved"`` is a grounded,
    at-or-above-floor-confidence LLM ``approve`` with NO veto. Every other branch (veto, hold, low
    confidence, ungrounded cue, LLM error/None) returns a non-approve.
    """
    # 1. Hard-stop veto FIRST (safe direction only).
    veto = send_intent_hard_stop(text, tenant_id=tenant_id)
    if veto == "rejected":
        return "rejected"
    if veto == "hold":
        return None

    # 2. LLM decides intent (grounded, structured). Any failure returned None already.
    result = classify_send_intent(
        text, tenant_id=tenant_id, text_call=text_call, consent_check=consent_check
    )
    if result is None:
        return None

    # 3. Money invariant — approve ONLY on a grounded, confident, explicit approve.
    if result.decision == "approve":
        if result.grounded and result.confidence >= _SEND_INTENT_APPROVE_MIN_CONFIDENCE:
            return "approved"
        return None  # ungrounded or low-confidence approve → HOLD (re-ask), never send
    if result.decision == "reject":
        # A reject is safe to honor only when grounded; an ungrounded reject → hold (re-ask). Either
        # way it is a non-approve, so the money invariant holds regardless.
        return "rejected" if result.grounded else None
    return None  # "hold" → re-ask


def shadow_log_send_intent(
    text: str,
    *,
    tenant_id: str,
    deterministic_decision: str | None,
    text_call: object | None = None,
    consent_check: object | None = None,
) -> None:
    """SHADOW mode: run the LLM + veto over the SAME reply and LOG what enforce WOULD have decided,
    alongside the deterministic decision that is ACTUALLY being returned. No behavior change, no
    second effect. PII-safe (CL-390): the raw reply body and the cited cue fragment are NEVER logged
    — only the two decision verbs, the confidence, and grounded/veto booleans. Fail-soft: a shadow
    error must never affect the live deterministic decision."""
    try:
        veto = send_intent_hard_stop(text, tenant_id=tenant_id)
        result = None
        if veto is None:
            result = classify_send_intent(
                text, tenant_id=tenant_id, text_call=text_call, consent_check=consent_check
            )
        would_enforce = decide_send_intent_enforce(
            text, tenant_id=tenant_id, text_call=text_call, consent_check=consent_check
        )
        agree = (deterministic_decision or None) == (would_enforce or None)
        logger.info(
            "send_intent SHADOW tenant=%s deterministic=%s llm_would=%s agree=%s veto=%s "
            "llm_decision=%s confidence=%s grounded=%s",
            tenant_id,
            deterministic_decision,
            would_enforce,
            agree,
            veto,
            getattr(result, "decision", None),
            round(getattr(result, "confidence", 0.0), 3) if result is not None else None,
            getattr(result, "grounded", None),
        )
    except Exception:  # noqa: BLE001 — shadow is observational only; never affects the live decision
        logger.warning("send_intent: shadow eval failed (fail-soft, no live effect)", exc_info=True)


__all__ = [
    "SendDecision",
    "SendIntentMode",
    "SendIntentResult",
    "classify_send_intent",
    "decide_send_intent_enforce",
    "get_send_intent_mode",
    "is_send_intent_enforce",
    "is_send_intent_off",
    "is_send_intent_shadow",
    "send_intent_hard_stop",
    "shadow_log_send_intent",
]
