"""VT-696 — the honest per-JOURNEY question budget + the web-presence-first capture.

Fazal (live first-customer run, 2026-07-22): "It said last question, and then it continued to
ask more questions. This makes the owner lose trust." Root: the residual cap applied
per-RECOMPOSE, so every answered question let a fresh one slide into the window — unbounded
total. The budget is now debited by every residual the owner has ALREADY answered. And the
single highest-value capture — a website / FB / LinkedIn / IndiaMART link — asks FIRST.
"""
from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from orchestrator.onboarding.question_brain import compose_onboarding_questions  # noqa: E402
from orchestrator.onboarding.whatsapp_journey import extract_web_presence_url  # noqa: E402

_DRAFT = {"attributes": {"nature_of_business": "BI reports", "legal_name": "X Pvt Ltd"}}


def _gaps(*fields):
    return lambda bt, da, ans: [{"field": f, "prompt_en": f"{f}?", "prompt_hi": f"{f}?"} for f in fields]


# --- the budget -------------------------------------------------------------------------------


def test_budget_debits_answered_residuals() -> None:
    """3 residuals already answered + a draft → ZERO further gaps, whatever the LLM offers."""
    qs = compose_onboarding_questions(
        "services", _DRAFT,
        answered=["primary_service_category", "typical_customer_type", "peak_season_or_demand"],
        llm_fn=_gaps("operating_hours", "team_size"),
    )
    assert [q for q in qs if q.kind == "gap"] == [], "the live 'last one then more' defect"


def test_budget_ignores_identity_and_confirm_answers() -> None:
    """Core identity (name/owner/GST card) + confirm-style answers never spend the budget."""
    qs = compose_onboarding_questions(
        "services", _DRAFT,
        answered=["business_name", "owner_name", "gst_identity", "nature_of_business"],
        llm_fn=_gaps("operating_hours"),
    )
    gap_fields = [q.field for q in qs if q.kind == "gap"]
    assert "operating_hours" in gap_fields, "budget untouched by identity/confirm answers"


def test_budget_partial_spend_leaves_remainder() -> None:
    qs = compose_onboarding_questions(
        "services", _DRAFT, answered=["primary_service_category", "web_presence"],
        llm_fn=_gaps("operating_hours", "team_size", "price_range"),
    )
    assert sum(q.kind == "gap" for q in qs) <= 1, "2 of 3 spent → at most 1 residual left"


def test_zero_budget_skips_the_gap_llm() -> None:
    calls = {"n": 0}

    def _counting(bt, da, ans):
        calls["n"] += 1
        return []

    compose_onboarding_questions(
        "services", _DRAFT,
        answered=["primary_service_category", "typical_customer_type", "peak_season_or_demand"],
        llm_fn=_counting,
    )
    assert calls["n"] == 0, "no LLM spend on the hot path when the budget is exhausted"


# --- web-presence first -----------------------------------------------------------------------


def test_web_presence_leads_and_consumes_budget() -> None:
    qs = compose_onboarding_questions("services", _DRAFT, answered=[], llm_fn=_gaps("operating_hours"))
    gap_fields = [q.field for q in qs if q.kind == "gap"]
    assert gap_fields[0] == "web_presence", "the highest-value capture asks first"
    assert len(gap_fields) <= 3
    web = next(q for q in qs if q.field == "web_presence")
    assert web.suggestions_en == ("No website",)


def test_web_presence_suppressed_when_known() -> None:
    drafted = {"attributes": {**_DRAFT["attributes"], "website": "https://rkecom.in"}}
    qs = compose_onboarding_questions("services", drafted, answered=[], llm_fn=_gaps())
    assert all(q.field != "web_presence" for q in qs), "draft website → never asked"
    qs2 = compose_onboarding_questions("services", _DRAFT, answered=["web_presence"], llm_fn=_gaps())
    assert all(q.field != "web_presence" for q in qs2), "answered → never re-asked"


def test_llm_website_synonym_collapses_onto_web_presence() -> None:
    qs = compose_onboarding_questions(
        "services", _DRAFT, answered=[], llm_fn=_gaps("website", "social_media")
    )
    assert [q.field for q in qs if q.kind == "gap"].count("web_presence") == 1, "no synonym repeat"


# --- the URL extractor ------------------------------------------------------------------------


def test_extract_url_forms() -> None:
    assert extract_web_presence_url("rkecom.in") == "https://rkecom.in"
    assert extract_web_presence_url("https://www.rkecom.in/about") == "https://www.rkecom.in/about"
    assert extract_web_presence_url("it's instagram.com/rkecom.") == "https://instagram.com/rkecom"
    assert extract_web_presence_url("indiamart.com/rkecom-services") == "https://indiamart.com/rkecom-services"


def test_extract_url_rejects_refusals_and_free_text() -> None:
    assert extract_web_presence_url("No website") == ""
    assert extract_web_presence_url("Skip") == ""
    assert extract_web_presence_url("nahi hai") == ""
    assert extract_web_presence_url("open 8.30 to 9 daily") == "", "numbers never read as a domain"
    assert extract_web_presence_url("") == ""
