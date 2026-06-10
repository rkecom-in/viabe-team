"""VT-366 Gap-2b — onboarding question-brain. Deterministic skeleton (confirm-first, exclude-known,
min-cap, bilingual) tested with an injected llm_fn; the live gap REASONING is the canary."""

from __future__ import annotations

from orchestrator.onboarding.question_brain import _MAX_GAPS, Question, compose_onboarding_questions

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
    qs = compose_onboarding_questions("apparel", {"attributes": {}}, answered=[], llm_fn=_gaps("hours", "hours", "size_range"))
    gap_fields = [q.field for q in qs if q.kind == "gap"]
    assert gap_fields == ["hours", "size_range"]


def test_llm_failure_falls_back_to_confirms_only():
    def boom(bt, da, ans):
        raise RuntimeError("llm down")

    qs = compose_onboarding_questions("apparel", _DRAFT, answered=[], llm_fn=boom)
    # the confirm questions still stand; no crash, no gaps
    assert qs and all(q.kind == "confirm" for q in qs)


def test_empty_draft_no_confirms():
    qs = compose_onboarding_questions("apparel", None, answered=[], llm_fn=_gaps("category", "city"))
    assert all(q.kind == "gap" for q in qs)
    assert isinstance(qs[0], Question)
