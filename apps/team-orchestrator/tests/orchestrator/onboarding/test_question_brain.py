"""VT-366 Gap-2b — onboarding question-brain. Deterministic skeleton (confirm-first, exclude-known,
min-cap, bilingual) tested with an injected llm_fn; the live gap REASONING is the canary."""

from __future__ import annotations

from orchestrator.onboarding.question_brain import (
    _MAX_GAPS,
    Question,
    _confirm_question,
    compose_onboarding_questions,
)

_DRAFT = {"attributes": {"category": "bookstore", "city": "Bengaluru", "rating": 4.5}, "provenance": {}}


def _gaps(*fields):
    return lambda bt, da, ans: [{"field": f, "prompt_en": f"{f}?", "prompt_hi": f"{f}?"} for f in fields]


def test_confirm_questions_lead_then_gaps():
    qs = compose_onboarding_questions("apparel", _DRAFT, answered=[], llm_fn=_gaps("operating_hours"))
    kinds = [q.kind for q in qs]
    assert kinds[0] == "confirm"  # confirm-the-draft first
    assert "gap" in kinds
    # all confirms precede all gaps
    assert kinds == sorted(kinds, key=lambda k: 0 if k == "confirm" else 1)


def test_excludes_answered_and_drafted_from_gaps_and_confirms():
    # city is answered → no city confirm; category is drafted → not asked as a gap.
    qs = compose_onboarding_questions("apparel", _DRAFT, answered=["city"], llm_fn=_gaps("category", "price_range"))
    fields = [(q.kind, q.field) for q in qs]
    assert ("confirm", "city") not in fields  # answered → not re-confirmed
    assert all(not (k == "gap" and f == "category") for k, f in fields)  # drafted → not a gap
    assert ("gap", "price_range") in fields  # genuine gap kept


def test_minimal_cap_no_dump():
    many = _gaps(*[f"f{i}" for i in range(20)])
    qs = compose_onboarding_questions("services", {"attributes": {}}, answered=[], llm_fn=many)
    assert sum(q.kind == "gap" for q in qs) <= _MAX_GAPS


def test_all_questions_bilingual():
    qs = compose_onboarding_questions("restaurant", _DRAFT, answered=[], llm_fn=_gaps("operating_hours", "cuisine"))
    assert qs and all(q.prompt_en and q.prompt_hi for q in qs)


def test_gap_dedup():
    # VT-693: LLM-invented field names collapse onto canonical keys ('hours' →
    # 'operating_hours') BEFORE dedup, so synonym-renamed repeats can never reach the owner.
    qs = compose_onboarding_questions("apparel", {"attributes": {}}, answered=[], llm_fn=_gaps("hours", "hours", "size_range"))
    gap_fields = [q.field for q in qs if q.kind == "gap"]
    # VT-696: no draft → no web-presence ask (it is draft-gated); the dedup contract holds.
    assert gap_fields == ["operating_hours", "size_range"]


def test_gap_synonym_repeat_and_draft_coverage_suppressed():
    """VT-693 pins: (a) a synonym of an ANSWERED field never re-asks ('products_services' after
    'about'); (b) a field the discovery payload covers never asks ('about' with a GST
    nature_of_business in the draft); (c) with a draft the residual caps at 3."""
    draft = {"attributes": {"nature_of_business": "Business Intelligence", "legal_name": "X Pvt Ltd"}}
    qs = compose_onboarding_questions(
        "services", draft, answered=["operating_hours"],
        llm_fn=_gaps("products_services", "main_services", "hours", "typical_customer",
                     "price_range", "peak_days", "team_size"),
    )
    gap_fields = [q.field for q in qs if q.kind == "gap"]
    assert "about" not in gap_fields, "draft nature_of_business covers products/services"
    assert "operating_hours" not in gap_fields, "answered field's synonym must not re-ask"
    assert len(gap_fields) <= 3, "draft present → residual budget is 3"


def test_llm_failure_falls_back_to_confirms_only():
    def boom(bt, da, ans):
        raise RuntimeError("llm down")

    qs = compose_onboarding_questions("apparel", _DRAFT, answered=[], llm_fn=boom)
    # the confirm questions still stand; no crash. VT-696: the deterministic web-presence
    # capture survives an LLM outage (it never depends on the gap LLM).
    assert qs and all(q.kind == "confirm" or q.field == "web_presence" for q in qs)
    assert any(q.kind == "confirm" for q in qs)


def test_empty_draft_no_confirms():
    qs = compose_onboarding_questions("apparel", None, answered=[], llm_fn=_gaps("category", "city"))
    assert all(q.kind == "gap" for q in qs)
    assert isinstance(qs[0], Question)


# --------------------------------------------------------------------------- VT-475 wiring


def test_reconciled_business_type_confirm_supersedes_raw_category():
    """VT-475: when the draft carries a RECONCILED business_type, the confirm shows it (by human
    label) and the raw GBP ``category`` (the RKeCom mis-category source) is SUPPRESSED — never asked
    twice, never surfaced raw."""
    draft = {"attributes": {"business_type": "services", "category": "Telecommunications service provider"},
             "provenance": {}}
    qs = compose_onboarding_questions("services", draft, answered=[], llm_fn=_gaps())
    confirm_fields = [q.field for q in qs if q.kind == "confirm"]
    assert "business_type" in confirm_fields
    assert "category" not in confirm_fields  # raw mis-category NOT surfaced when reconciled type exists
    bt_q = next(q for q in qs if q.field == "business_type")
    # the confirm shows the human label, NOT the machine key, and NEVER the telecom mis-category text
    assert "Telecommunications" not in bt_q.prompt_en
    assert "is that right?" in bt_q.prompt_en  # confirm UX unchanged
    assert bt_q.prompt_hi  # bilingual


def test_raw_category_still_confirms_when_no_reconciled_type():
    """Back-compat: a draft with only the raw ``category`` (no reconciliation ran) still confirms it
    — the reconciled type is additive, not a hard dependency."""
    draft = {"attributes": {"category": "bookstore"}, "provenance": {}}
    qs = compose_onboarding_questions("book_stationery", draft, answered=[], llm_fn=_gaps())
    confirm_fields = [q.field for q in qs if q.kind == "confirm"]
    assert "category" in confirm_fields


# ------------------------------------------------ fabrication guard (official §2, 2026-07-10)
# A confirm-back of a DISCOVERED field must attribute the value to discovery ("We found …"), never
# flatly assert it — an unattributed "And you're based in {city} — correct?" reads as an INVENTED
# location to a blind reader (and the §2 judge), a Tier-1 fabrication trust-breaker, even though the
# value came from auto-discovery (GBP). Grounds every discovered-field confirm.
_PROVENANCE_MARKERS = ("found", "describe")


def _cites_provenance(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _PROVENANCE_MARKERS)


def test_city_confirm_cites_provenance_not_a_flat_assertion():
    q = _confirm_question("city", "Surat")
    assert q.field == "city" and q.kind == "confirm"
    assert "Surat" in q.prompt_en
    assert _cites_provenance(q.prompt_en), q.prompt_en          # grounded ("We found …")
    assert "you're based in" not in q.prompt_en.lower()          # never the old flat assertion
    assert "पता चला" in q.prompt_hi                              # Hindi mirror also grounded


def test_all_discovered_field_confirms_are_grounded():
    for field, value in (
        ("category", "hardware store"),
        ("city", "Chennai"),
        ("about", "we sell tools and building supplies"),
    ):
        q = _confirm_question(field, value)
        assert _cites_provenance(q.prompt_en), f"{field}: {q.prompt_en!r} must cite provenance"


def test_gap_suggestions_pass_through_clamped():
    """VT-694: suggestions ride the Question (most-likely first, ≤3, ≤20 chars each)."""
    def _llm(bt, da, ans):
        return [{"field": "operating_hours", "prompt_en": "Hours?", "prompt_hi": "?",
                 "suggestions_en": ["24/7 online", "10am-9pm every day and more text", "Weekdays", "Extra4"],
                 "suggestions_hi": ["२४/७"]}]

    qs = compose_onboarding_questions("services", {"attributes": {}}, answered=[], llm_fn=_llm)
    q = [x for x in qs if x.kind == "gap" and x.field == "operating_hours"][0]
    assert q.suggestions_en[0] == "24/7 online"
    assert len(q.suggestions_en) == 3, "clamped to 3"
    assert all(len(s) <= 20 for s in q.suggestions_en), "20-char button-title clamp"
    assert q.suggestions_hi == ("२४/७",)
