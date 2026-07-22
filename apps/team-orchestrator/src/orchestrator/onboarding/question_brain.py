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
# VT-693 (Fazal 2026-07-22, first-customer screenshots: "endless questions"): once ONLINE
# discovery has produced a draft, the residual the owner is asked shrinks hard — the
# intelligence exhausts sources first, asks only what genuinely remains.
_MAX_GAPS_WITH_DRAFT = 3

# VT-693 — canonical-field alias collapse (deterministic synonym belt). The gap LLM invents a
# fresh snake_case name per turn ("products_services" one turn, "main_services" the next), so
# name-keyed dedup let REPEATS reach the owner (the measured screenshot: products/services
# asked twice in different words). Finite alias map onto the canonical question fields — an
# enum collapse, not open-language matching (the no-keyword-lists rule allows exactly this).
_FIELD_ALIASES: dict[str, str] = {
    "products": "about", "services": "about", "products_services": "about",
    "products_or_services": "about", "main_services": "about", "services_offered": "about",
    "offerings": "about", "main_products": "about", "products_offered": "about",
    "business_activities": "about", "main_activities": "about", "what_you_sell": "about",
    "nature_of_business": "about",
    "hours": "operating_hours", "timings": "operating_hours", "business_hours": "operating_hours",
    "opening_hours": "operating_hours", "working_hours": "operating_hours",
    "customers": "typical_customer", "typical_customers": "typical_customer",
    "target_customers": "typical_customer", "customer_type": "typical_customer",
    "target_audience": "typical_customer", "customer_segments": "typical_customer",
    "pricing": "price_range", "pricing_model": "price_range",
    "prices": "price_range", "typical_price_range": "price_range",
    "location": "city", "area": "city", "locality": "city",
}


def _canonical_field(name: str) -> str:
    n = (name or "").strip().lower()
    return _FIELD_ALIASES.get(n, n)


def covered_by_draft(draft_attrs: dict[str, Any]) -> set[str]:
    """VT-693 — canonical question-fields the DISCOVERY payload already answers, so the gap
    composer never re-asks them: a GST ``nature_of_business`` covers 'about'; a registered
    ``principal_address`` covers 'city'; a legal/trade name covers 'business_name'."""
    covered: set[str] = set()
    if draft_attrs.get("nature_of_business"):
        covered.add("about")
    if draft_attrs.get("principal_address"):
        covered.add("city")
    if draft_attrs.get("legal_name") or draft_attrs.get("trade_name"):
        covered.add("business_name")
    return covered
_COMPOSE_TIMEOUT_S = 20.0  # bound the gap-compose LLM call (runs on the owner-inbound hot path)

# Draft fields worth an explicit owner confirm (the discovered identity). Bilingual templates below.
# VT-475: ``business_type`` (the RECONCILED Viabe-taxonomy type, auto_discovery_sources) is the
# business-type confirm — it SUPERSEDES the raw GBP ``category`` (which was the RKeCom mis-category
# source). When a reconciled business_type is present we confirm THAT, not the raw category (see
# ``_business_type_confirm`` below); ``category`` remains the fallback when no reconciliation ran.
_CONFIRMABLE = ("business_type", "category", "city", "about")


@dataclass(frozen=True)
class Question:
    field: str
    kind: str  # "confirm" (verify a discovered draft field) | "gap" (a genuinely-missing field)
    prompt_en: str
    prompt_hi: str
    draft_value: Any = None


def _business_type_label(key: Any) -> tuple[str, str]:
    """Resolve a reconciled taxonomy KEY ('services') to its human (en, hi) label for the confirm —
    the owner sees "Local services", not the machine key. Falls back to the key itself off-taxonomy
    or if the taxonomy can't load (fail-soft; the confirm step still lets them correct it)."""
    try:
        from orchestrator.onboarding.business_type_reconcile import taxonomy_label

        return taxonomy_label(str(key))
    except Exception:  # noqa: BLE001 — label lookup is cosmetic; never break the question set
        s = str(key)
        return s, s


def _confirm_question(field: str, value: Any) -> Question:
    v = value
    if field == "business_type":
        # VT-475 — confirm the RECONCILED type by its human label (not the machine key, not the raw
        # mis-categorized GBP category). The confirm UX is UNCHANGED ("is that right?", never asserted)
        # — only the GUESS shown is the reconciled one.
        en_label, hi_label = _business_type_label(value)
        return Question(
            field="business_type",
            kind="confirm",
            prompt_en=f"We found you're a {en_label} business — is that right?",
            prompt_hi=f"हमें पता चला आप {hi_label} का व्यापार करते हैं — क्या यह सही है?",
            draft_value=value,
        )
    templates = {
        "category": (f"We found you're a {v} — is that right?", f"हमें पता चला आप {v} हैं — क्या यह सही है?"),
        # Cite the discovered SOURCE ("We found …"), matching the business_type/category confirms.
        # The prior copy "And you're based in {v} — correct?" asserted the city with NO provenance,
        # so a blind reader (and the §2 judge, 2026-07-10) saw an INVENTED location — a fabrication
        # trust-breaker — even though the value came from auto-discovery (GBP). Grounding it as a
        # discovered fact keeps the useful verify-the-city step without reading as a fabrication.
        "city": (f"We found your shop is in {v} — is that right?",
                 f"हमें पता चला आपकी दुकान {v} में है — क्या यह सही है?"),
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
    #    VT-475: when a RECONCILED ``business_type`` is present it is the business-type confirm; the raw
    #    GBP ``category`` (the RKeCom mis-category source) is then SUPPRESSED — we never surface the raw
    #    field. ``category`` only confirms when no reconciliation produced a business_type.
    confirmable = [
        f for f in _CONFIRMABLE
        if f in draft_attrs and draft_attrs[f] not in (None, "", []) and f not in answered_set
    ]
    if "business_type" in confirmable and "category" in confirmable:
        confirmable.remove("category")
    confirms = [_confirm_question(f, draft_attrs[f]) for f in confirmable]

    # 2. Gaps — the LLM reasons which required fields THIS business_type still needs, excluding what's
    #    already known (drafted or answered). It returns bilingual question objects.
    #    VT-693: 'known' is CANONICALIZED (alias collapse) and unioned with the fields the
    #    discovery payload semantically covers — a synonym-renamed repeat can never survive the
    #    dedup, and a fact discovery already fetched is never asked. With a non-empty draft the
    #    residual budget also tightens (_MAX_GAPS_WITH_DRAFT).
    known = (
        {_canonical_field(f) for f in draft_attrs}
        | {_canonical_field(f) for f in answered_set}
        | covered_by_draft(draft_attrs)
    )
    max_gaps = _MAX_GAPS_WITH_DRAFT if draft_attrs else _MAX_GAPS
    try:
        raw = (llm_fn or _llm_compose_gaps)(business_type, draft_attrs, sorted(answered_set))
    except Exception:  # noqa: BLE001 — gap reasoning is best-effort; the confirm questions still stand
        logger.warning("question_brain: gap source raised business_type=%s — confirms only", business_type)
        raw = []
    gaps: list[Question] = []
    seen: set[str] = set()
    for g in raw or []:
        field = _canonical_field((g.get("field") or "").strip())
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
        if len(gaps) >= max_gaps:
            break

    return confirms + gaps


def _llm_compose_gaps(
    business_type: str, draft_attrs: dict[str, Any], answered: list[str]
) -> list[dict[str, Any]]:
    """Ask Haiku which required onboarding fields a business of ``business_type`` STILL needs (given
    what's already known), as bilingual question objects. Returns [] on any failure (the confirm
    questions still stand). CL-390: business context only — never ask for third-party PII."""
    from anthropic import Anthropic

    # VT-693: give the model the VALUES, not just opaque field names — semantic coverage is
    # what stops "what do you sell?" after a GST nature_of_business already answered it. Values
    # are business-level context only (the draft never holds third-party PII) and truncated.
    known_with_values = {
        k: str(v)[:80] for k, v in sorted(draft_attrs.items()) if v not in (None, "", [])
    }
    prompt = (
        f"A small Indian business is onboarding to an AI assistant. business_type: {business_type}. "
        f"We ALREADY know (from online discovery + the owner's own answers) — do NOT ask about any "
        f"of this again, INCLUDING rephrasings or synonyms of it: {known_with_values or 'none'}; "
        f"owner already answered fields: {sorted(answered) or 'none'}. "
        "List the MINIMAL set of genuinely-missing business-context fields this specific "
        "business_type still needs (e.g. operating_hours, typical_customer, price_range, peak_days) "
        "— reason about what THIS type needs, not a fixed script. If nothing meaningful is missing, "
        "return []. Ask ONLY about the business itself; NEVER ask for any customer's or third "
        "party's personal details (CL-390). Return at most 3, ordered most-important-first, as "
        'JSON: a list of objects {"field": "<snake_case>", "prompt_en": "<short question>", '
        '"prompt_hi": "<Hindi question>"}. JSON array only, no prose.'
    )
    try:
        resp = Anthropic().messages.create(
            model=_GAP_MODEL,
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
            timeout=_COMPOSE_TIMEOUT_S,  # VT-367: bound the call — this runs on the owner-inbound hot
            # path (the journey pending-fill branch); a hang must degrade to [] (confirms/opener), not
            # stall the webhook pipeline. try/except catches the timeout exception → [].
        )
        raw = resp.content[0].text if resp.content else "[]"
        start, end = raw.find("["), raw.rfind("]")
        data = json.loads(raw[start : end + 1]) if start != -1 and end != -1 else []
        return data if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001 — LLM/parse fragile; the confirm questions still stand
        logger.warning("question_brain: gap compose failed business_type=%s (%s)", business_type, type(exc).__name__)
        return []
