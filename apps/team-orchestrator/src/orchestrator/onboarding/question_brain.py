"""VT-366 Gap-2b — the onboarding QUESTION-BRAIN.

``compose_onboarding_questions`` decides the ORDERED, MINIMAL set of onboarding questions for an
owner, given (a) their business_type, (b) what the Auto-Discovery draft (2a ``get_draft``) already
found, and (c) what they've already answered. It is NOT a fixed script — an LLM reasons about which
required fields a business of this type still needs. Order: **confirm-the-draft questions FIRST**
(the owner confirms/corrects discovered fields → 2a ``confirm_draft``; unconfirmed draft is never
fact), then only the genuinely-missing fields. Bilingual (EN+HI). CL-390: business context only,
never third-party PII.

Gap-3-pace-able: this PRODUCES the ordered set + per-question metadata; Gap 3's guided journey PACES
delivery (one part at a time). The deterministic skeleton (confirm-first, exclude-known, min-cap) is
unit-testable without the LLM (inject ``llm_fn``); the gap REASONING + phrasing is the live canary.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

_GAP_MODEL = "claude-haiku-4-5-20251001"
_MAX_GAPS = 6  # minimal — never a 20-question dump

# Draft fields worth an explicit owner confirm (the discovered identity). Bilingual templates below.
_CONFIRMABLE = ("category", "city", "about")


@dataclass(frozen=True)
class Question:
    field: str
    kind: str  # "confirm" (verify a discovered draft field) | "gap" (a genuinely-missing field)
    prompt_en: str
    prompt_hi: str
    draft_value: Any = None


def _confirm_question(field: str, value: Any) -> Question:
    v = value
    templates = {
        "category": (f"We found you're a {v} — is that right?", f"हमें पता चला आप {v} हैं — क्या यह सही है?"),
        "city": (f"And you're based in {v} — correct?", f"और आप {v} में हैं — क्या यह सही है?"),
        "about": (f"Here's how we'd describe your business: \"{v}\". Does that look right?",
                  f"हम आपके व्यापार को ऐसे बताएंगे: \"{v}\"। क्या यह ठीक है?"),
    }
    en, hi = templates.get(field, (f"We found {field}: {v} — correct?", f"हमें {field} मिला: {v} — सही है?"))
    return Question(field=field, kind="confirm", prompt_en=en, prompt_hi=hi, draft_value=value)


def compose_onboarding_questions(
    business_type: str,
    draft: dict[str, Any] | None,
    answered: list[str] | None = None,
    *,
    llm_fn: Callable[[str, dict[str, Any], list[str]], list[dict[str, Any]]] | None = None,
) -> list[Question]:
    """Return the ordered, minimal question set. ``draft`` is 2a ``get_draft(tenant)`` output
    ({attributes, provenance}) or None; ``answered`` are fields the owner already gave."""
    draft_attrs = dict((draft or {}).get("attributes", {}))
    answered_set = set(answered or [])

    # 1. Confirm-the-draft questions FIRST — only for discovered fields the owner hasn't answered.
    confirms = [
        _confirm_question(f, draft_attrs[f])
        for f in _CONFIRMABLE
        if f in draft_attrs and draft_attrs[f] not in (None, "", []) and f not in answered_set
    ]

    # 2. Gaps — the LLM reasons which required fields THIS business_type still needs, excluding what's
    #    already known (drafted or answered). It returns bilingual question objects.
    known = set(draft_attrs) | answered_set
    try:
        raw = (llm_fn or _llm_compose_gaps)(business_type, draft_attrs, sorted(answered_set))
    except Exception:  # noqa: BLE001 — gap reasoning is best-effort; the confirm questions still stand
        logger.warning("question_brain: gap source raised business_type=%s — confirms only", business_type)
        raw = []
    gaps: list[Question] = []
    seen: set[str] = set()
    for g in raw or []:
        field = (g.get("field") or "").strip()
        if not field or field in known or field in seen:
            continue
        seen.add(field)
        gaps.append(
            Question(
                field=field,
                kind="gap",
                prompt_en=g.get("prompt_en") or f"Could you tell us your {field}?",
                prompt_hi=g.get("prompt_hi") or f"क्या आप अपना {field} बता सकते हैं?",
            )
        )
        if len(gaps) >= _MAX_GAPS:
            break

    return confirms + gaps


def _llm_compose_gaps(
    business_type: str, draft_attrs: dict[str, Any], answered: list[str]
) -> list[dict[str, Any]]:
    """Ask Haiku which required onboarding fields a business of ``business_type`` STILL needs (given
    what's already known), as bilingual question objects. Returns [] on any failure (the confirm
    questions still stand). CL-390: business context only — never ask for third-party PII."""
    from anthropic import Anthropic

    known = sorted(set(draft_attrs) | set(answered))
    prompt = (
        f"A small Indian business is onboarding to an AI assistant. business_type: {business_type}. "
        f"We ALREADY know these fields (do NOT ask about them again): {known or 'none'}. "
        "List the MINIMAL set of additional business-context fields this specific business_type still "
        "needs for the assistant to help it (e.g. products/services, operating_hours, typical_customer, "
        "price_range, peak_days) — reason about what THIS type needs, not a fixed script. "
        "Ask ONLY about the business itself; NEVER ask for any customer's or third party's personal "
        "details (CL-390). Return at most 6, ordered most-important-first, as JSON: a list of objects "
        '{"field": "<snake_case>", "prompt_en": "<short question>", "prompt_hi": "<Hindi question>"}. '
        "JSON array only, no prose."
    )
    try:
        resp = Anthropic().messages.create(
            model=_GAP_MODEL,
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text if resp.content else "[]"
        start, end = raw.find("["), raw.rfind("]")
        data = json.loads(raw[start : end + 1]) if start != -1 and end != -1 else []
        return data if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001 — LLM/parse fragile; the confirm questions still stand
        logger.warning("question_brain: gap compose failed business_type=%s (%s)", business_type, type(exc).__name__)
        return []
