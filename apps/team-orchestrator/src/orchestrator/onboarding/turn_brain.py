"""VT-569 — the onboarding TURN-BRAIN (LLM-driven in-session conversation).

Once the WhatsApp onboarding session has started, WHAT the bot says and HOW it interprets the owner's
reply moves to an LLM so the conversation feels like ChatGPT/Claude Chat instead of a fixed
question-walker: warm, Hinglish-aware, one-thing-at-a-time, able to offer buttons when a choice
genuinely helps. Fazal (live drill, binding): "the LLM brain should make use of the session freedom
and come up with interactive responses, ensuring we are not burdening the owner with questions."

WHAT STAYS DETERMINISTIC (the durable spine, owned by ``journey``, NOT this module):
  - the ``onboarding_journey`` table (queue / cursor / answers / skipped / last_message_sid) is the
    resumability substrate — this module NEVER writes it; it only READS a snapshot of it as context.
  - FIELD PROMOTION happens ONLY via ``journey``'s confirm/record path (``confirm_draft``): this brain
    PROPOSES ``{field: value}`` extractions + which fields the owner confirmed; the deterministic layer
    validates + records them (never-assert boundary, CL-390).
  - CLAIM-GROUNDING: the brain may present ONLY facts the discovery draft actually found (enumerated
    in the prompt with provenance); it never invents a business fact, and ``extracted_answers`` may
    carry ONLY what the owner literally said this turn (extraction ≠ invention).
  - the DETERMINISTIC completion check owns "done"; ``done_hint`` here is advisory only.

FAIL-SOFT: ``compose_turn`` returns ``None`` on ANY failure (LLM error / timeout / unparseable /
empty) — the caller then falls back to the deterministic walker for that turn, so onboarding never
stalls on an LLM hiccup. Gated by ``ONBOARDING_TURN_BRAIN`` (read in ``journey``; default OFF).

Model: the house CONVERSATIONAL tier (``claude-sonnet-4-6``, the dispatch brain's routine-turn model)
— Haiku (the question-brain gap model) is too weak for free conversation; Opus is reserved for the
brain's complex reasoning and adds latency on this owner-inbound hot path. Sonnet is the right middle:
the tier the product already uses to actually talk to owners.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_TURN_MODEL = "claude-sonnet-4-6"  # house conversational tier (parity with dispatch _BRAIN_MODEL_SONNET)
_MAX_TOKENS = 1024  # a short WhatsApp reply + a small JSON envelope — never a wall of text
_TURN_TIMEOUT_S = 20.0  # bound the call — runs on the owner-inbound hot path (parity with question_brain)
_MAX_BUTTONS = 3  # Meta quick-reply hard limit (WhatsApp in-session)


@dataclass(frozen=True)
class TurnPlan:
    """The turn-brain's structured, validated decision for ONE owner reply.

    ``reply_text`` is the message to send, composed in the owner's own language (NOT a bilingual pair —
    the brain mirrors the owner's register). ``buttons`` are 0-3 quick-reply titles (capped).
    ``extracted_answers`` = ``{field: value}`` the owner supplied THIS message (fed to the deterministic
    recorders). ``mark_confirmed`` = fields the owner affirmatively confirmed (→ the promotion gate).
    ``mark_rejected`` = fields the owner said are wrong (drive the reply; never recorded). ``done_hint``
    is advisory (the deterministic check owns "done"). ``reasoning`` is a short trace for observability.
    """

    reply_text: str
    buttons: tuple[str, ...] = ()
    extracted_answers: dict[str, Any] = field(default_factory=dict)
    mark_confirmed: tuple[str, ...] = ()
    mark_rejected: tuple[str, ...] = ()
    done_hint: bool = False
    reasoning: str = ""


_SYSTEM_PROMPT = """You are the owner's Team Manager for Viabe — a warm, concise WhatsApp assistant \
helping a small Indian business owner finish setting up. You are having a natural conversation, not \
reading out a form.

Hard rules you MUST follow:
- Mirror the owner's language and register. If they write in Hindi or Hinglish, reply in Hinglish. \
Default locale: {locale}.
- Keep every reply SHORT — one or two sentences, WhatsApp-style. No markdown, no bulleted dumps.
- Do NOT burden the owner with many questions. Ask for AT MOST ONE new thing per turn (you may \
confirm one discovered fact and, if it flows naturally, ask one next thing).
- CLAIM-GROUNDING: you may ONLY state business facts that appear in DISCOVERED below. NEVER invent a \
business fact (no made-up address, category, hours, name). If unsure, ask — do not assert.
- EXTRACTION IS NOT INVENTION: put a value in extracted_answers ONLY if the owner literally stated it \
in THIS message. Never fabricate an answer or guess.
- If the owner REJECTS a discovered value (says it is wrong, or just "no"), do NOT repeat the same \
question word-for-word. Acknowledge it, then ask what the correct value is — and if DISCOVERED offers \
plausible alternatives, offer up to 3 of them as buttons.
- If the owner asks YOU a question, answer it briefly first, then gently steer back to what is still \
needed.
- BUTTONS: request quick-reply buttons ONLY when a small choice genuinely helps — e.g. Yes / No / \
Skip for a confirmation, or 2-3 concrete discovered alternatives. Never more than 3. Otherwise leave \
buttons empty and just use plain text.
- You NEVER decide onboarding is finished — a separate deterministic check owns that. Use done_hint \
only as a soft signal.

Return ONLY a single JSON object (no prose, no code fence) with exactly these keys:
  "reply_text": string — the message to send, in the owner's language;
  "buttons": array of 0-3 short button titles (empty if none);
  "extracted_answers": object mapping field name -> value the owner gave THIS message ({} if none);
  "mark_confirmed": array of field names the owner affirmatively confirmed this message;
  "mark_rejected": array of field names the owner said are wrong this message;
  "done_hint": boolean — whether it feels like nothing more is needed (advisory only);
  "reasoning": one short string explaining your choice.
"""


def _fmt_discovered(draft_attrs: dict[str, Any], provenance: dict[str, Any] | None) -> str:
    """Enumerate the discovered draft facts (with provenance) — the ONLY facts the brain may state."""
    prov = provenance or {}
    lines: list[str] = []
    for k, v in (draft_attrs or {}).items():
        if v in (None, "", []):
            continue
        src = (prov.get(k) or {}).get("source") if isinstance(prov.get(k), dict) else None
        reasoning = (prov.get(k) or {}).get("reasoning") if isinstance(prov.get(k), dict) else None
        tag = f" (source: {src}" + (f"; {reasoning}" if reasoning else "") + ")" if src else ""
        lines.append(f"- {k}: {v}{tag}")
    return "\n".join(lines) if lines else "(nothing discovered yet)"


def _fmt_still_needed(objective: list[dict[str, Any]]) -> str:
    """The remaining fields to collect (queue tail), with kind + the deterministic prompt as a hint."""
    if not objective:
        return "(nothing outstanding — do not ask for anything new)"
    lines: list[str] = []
    for q in objective:
        kind = q.get("kind", "gap")
        fieldname = q.get("field", "")
        dv = q.get("draft_value")
        hint = q.get("prompt_en") or ""
        dv_txt = f" (discovered guess: {dv})" if kind == "confirm" and dv not in (None, "") else ""
        lines.append(f"- {fieldname} [{kind}]{dv_txt} — e.g. \"{hint}\"")
    return "\n".join(lines)


def _objective_from_state(journey_state: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """(what we last asked, the remaining objective set) derived from the journey snapshot.

    ``current`` = the queue entry at the cursor (the question the owner is now answering; None at a
    fresh start / past the end). ``objective`` = the un-answered, un-skipped tail from the cursor —
    the still-needed fields the brain composes against (no fixed playbook, just the bounded set)."""
    queue = list(journey_state.get("question_queue") or [])
    cursor = int(journey_state.get("cursor") or 0)
    answers = dict(journey_state.get("answers") or {})
    skipped = set(journey_state.get("skipped") or [])
    current = queue[cursor] if 0 <= cursor < len(queue) else None
    objective = [
        q
        for q in queue[cursor:]
        if q.get("field") not in answers and q.get("field") not in skipped
    ]
    return current, objective


def _build_prompts(
    journey_state: dict[str, Any],
    draft_attrs: dict[str, Any],
    owner_message: str,
    *,
    locale: str,
    provenance: dict[str, Any] | None,
    is_start: bool,
) -> tuple[str, str]:
    """Assemble (system, user) prompts. Pure — unit-testable without the LLM."""
    current, objective = _objective_from_state(journey_state)
    answers = dict(journey_state.get("answers") or {})

    if is_start:
        asked = (
            "(this is the owner's FIRST message — greet them ONCE, warmly, then open with the single "
            "most important outstanding item conversationally. Do not stack questions.)"
        )
    elif current is not None:
        asked = current.get("prompt_en") or f"(we last asked about: {current.get('field')})"
    else:
        asked = "(no specific question is pending)"

    # ``.replace`` (not ``.format``) — the system prompt contains literal JSON braces ({} / {field: value}).
    system = _SYSTEM_PROMPT.replace("{locale}", locale or "en")
    user = (
        "DISCOVERED (facts found from public sources — the ONLY facts you may state):\n"
        f"{_fmt_discovered(draft_attrs, provenance)}\n\n"
        "STILL NEEDED (collect these, conversationally, at most one new ask per turn):\n"
        f"{_fmt_still_needed(objective)}\n\n"
        "WHAT YOU LAST ASKED:\n"
        f"{asked}\n\n"
        "ALREADY COLLECTED (do not re-ask):\n"
        f"{json.dumps(answers, ensure_ascii=False) if answers else '(nothing yet)'}\n\n"
        "OWNER'S MESSAGE:\n"
        f"{(owner_message or '').strip() or '(empty)'}"
    )
    return system, user


def _invoke_llm(system_prompt: str, user_prompt: str) -> str:
    """The single LLM call (lazy anthropic import — keeps module import dep-less for the smoke suite).
    Separated so tests monkeypatch THIS and the prompt-build + parse path stay pure + deterministic."""
    from anthropic import Anthropic

    resp = Anthropic().messages.create(
        model=_TURN_MODEL,
        max_tokens=_MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        timeout=_TURN_TIMEOUT_S,
    )
    return resp.content[0].text if resp.content else ""


def _coerce_str_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v).strip() for v in value if str(v).strip())


def _parse_turn_plan(raw: str) -> TurnPlan | None:
    """Coerce the LLM's raw text into a validated ``TurnPlan``, or ``None`` if unusable (→ fallback).

    HARD validation (the never-trust-the-LLM boundary): ``reply_text`` is required (empty → None);
    ``buttons`` are hard-capped at 3 (Meta limit); ``extracted_answers`` values are stringified +
    empties dropped. Taxonomy validation of an extracted business_type happens at the PROMOTION gate
    in ``journey`` (this only structures; it never asserts a fact)."""
    if not raw:
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(raw[start : end + 1])
    except Exception:  # noqa: BLE001 — LLM/JSON fragile; unparseable → fallback to the walker
        logger.warning("turn_brain: could not parse turn-plan JSON — falling back")
        return None
    if not isinstance(obj, dict):
        return None

    reply_text = str(obj.get("reply_text") or "").strip()
    if not reply_text:
        return None  # no message to send → treat as a failure, fall back to the deterministic walker

    raw_extracted = obj.get("extracted_answers") or {}
    extracted: dict[str, Any] = {}
    if isinstance(raw_extracted, dict):
        for k, v in raw_extracted.items():
            key = str(k).strip()
            val = "" if v is None else str(v).strip()
            if key and val:
                extracted[key] = val

    return TurnPlan(
        reply_text=reply_text,
        buttons=_coerce_str_list(obj.get("buttons"))[:_MAX_BUTTONS],
        extracted_answers=extracted,
        mark_confirmed=_coerce_str_list(obj.get("mark_confirmed")),
        mark_rejected=_coerce_str_list(obj.get("mark_rejected")),
        done_hint=bool(obj.get("done_hint")),
        reasoning=str(obj.get("reasoning") or "").strip(),
    )


def compose_turn(
    journey_state: dict[str, Any],
    draft_attrs: dict[str, Any],
    owner_message: str,
    *,
    locale: str = "en",
    provenance: dict[str, Any] | None = None,
    is_start: bool = False,
) -> TurnPlan | None:
    """Compose ONE conversational onboarding turn. Returns a validated ``TurnPlan`` or ``None``.

    ``None`` is the fail-soft signal (LLM error / timeout / unparseable / empty reply) — the caller
    then runs the deterministic walker for this turn, so onboarding never stalls. This function is
    PURE of side effects: it reads the journey snapshot + draft as context and PROPOSES a plan; the
    deterministic layer in ``journey`` validates, records, and advances the durable spine.
    """
    try:
        system, user = _build_prompts(
            journey_state, draft_attrs, owner_message,
            locale=locale, provenance=provenance, is_start=is_start,
        )
        raw = _invoke_llm(system, user)
        return _parse_turn_plan(raw)
    except Exception as exc:  # noqa: BLE001 — hot path: any failure degrades to the walker, never stalls
        logger.warning("turn_brain: compose_turn failed (%s) — falling back to walker", type(exc).__name__)
        return None


__all__ = ["TurnPlan", "compose_turn"]
