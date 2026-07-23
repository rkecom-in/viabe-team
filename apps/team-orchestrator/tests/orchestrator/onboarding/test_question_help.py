"""VT-701 — plain-language questions + per-question help + the helpful represent guard.

Fazal (live run #4): "I don't understand what to respond and the AI is not helping me
understand it" — after "When do you typically operate?" and a deflected "What does that mean?".
"""
from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from orchestrator.onboarding.question_brain import (  # noqa: E402
    compose_onboarding_questions,
    field_help,
)

_DRAFT = {"attributes": {"nature_of_business": "BI reports", "legal_name": "X Pvt Ltd"}}


def test_field_help_covers_canonical_fields() -> None:
    en, hi = field_help("operating_hours")
    assert "when customers can reach" in en and hi
    assert field_help("hours")[0] == en, "aliases resolve to the same help"
    assert field_help("unknown_field") == ("", "")


def test_llm_help_rides_the_question_with_fallback() -> None:
    def _llm(bt, da, ans):
        return [
            {"field": "payment_terms", "prompt_en": "How do customers pay?", "prompt_hi": "?",
             "help_en": "Do customers pay before, on delivery, or later on credit?",
             "help_hi": "ग्राहक पहले, डिलीवरी पर, या बाद में उधार पर देते हैं?"},
            {"field": "operating_hours", "prompt_en": "Working hours?", "prompt_hi": "?"},
        ]

    qs = compose_onboarding_questions("services", _DRAFT, answered=["web_presence"], llm_fn=_llm)
    by_field = {q.field: q for q in qs if q.kind == "gap"}
    assert by_field["payment_terms"].help_en.startswith("Do customers pay before")
    assert "when customers can reach" in by_field["operating_hours"].help_en, (
        "LLM omitted help → the canonical-field fallback fills it"
    )


def test_web_presence_question_carries_help() -> None:
    qs = compose_onboarding_questions("services", _DRAFT, answered=[], llm_fn=lambda *a: [])
    web = next(q for q in qs if q.field == "web_presence")
    assert "website, Facebook" in web.help_en


def test_gap_prompt_demands_plain_words_and_help() -> None:
    from orchestrator.onboarding import question_brain as qb
    import inspect

    src = inspect.getsource(qb._llm_compose_gaps)
    assert "PLAIN WORDS" in src and "help_en" in src


def test_represent_guard_sends_help_and_buttons(monkeypatch) -> None:
    """The runner guard re-presents WITH the plain-language explanation + the question's own
    buttons — never the bare robotic line when help exists."""
    from types import SimpleNamespace

    from orchestrator import runner

    sent: dict = {}
    import orchestrator.onboarding.journey as j

    monkeypatch.setattr(
        j, "get_journey",
        lambda t: {"status": "active", "cursor": 0, "question_queue": [{
            "field": "operating_hours", "kind": "gap",
            "prompt_en": "When do you typically operate?", "prompt_hi": "?",
            "suggestions_en": ["24/7 online", "10am-8pm"],
        }], "answers": {}, "skipped": []},
    )
    monkeypatch.setattr(j, "_send", lambda recipient, q, lang, *, tenant_id=None: sent.update(q=q))
    event = SimpleNamespace(sender_phone="+919999005001")
    assert runner._journey_represent_instead_of_consent_ask.__wrapped__("t-1", event) is True
    assert sent["q"]["prompt_en"].startswith("I'm asking when customers can reach"), (
        "the explanation leads"
    )
    assert "When do you typically operate?" in sent["q"]["prompt_en"]
    assert sent["q"]["suggestions_en"] == ["24/7 online", "10am-8pm"], "buttons ride the re-present"


def test_represent_guard_fallback_copy_without_help(monkeypatch) -> None:
    from types import SimpleNamespace

    from orchestrator import runner

    sent: dict = {}
    import orchestrator.onboarding.journey as j

    monkeypatch.setattr(
        j, "get_journey",
        lambda t: {"status": "active", "cursor": 0, "question_queue": [{
            "field": "special_thing", "kind": "gap",
            "prompt_en": "Anything special?", "prompt_hi": "?",
        }], "answers": {}, "skipped": []},
    )
    monkeypatch.setattr(j, "_send", lambda recipient, q, lang, *, tenant_id=None: sent.update(q=q))
    event = SimpleNamespace(sender_phone="+919999005002")
    assert runner._journey_represent_instead_of_consent_ask.__wrapped__("t-2", event) is True
    assert sent["q"]["prompt_en"] == "Let's finish setting up first — Anything special?"


def test_turn_brain_prompt_carries_the_three_rules() -> None:
    from orchestrator.onboarding import turn_brain as tb
    import inspect

    src = inspect.getsource(tb)
    assert "PLAIN WORDS" in src
    assert "NEVER DEFLECT CONFUSION" in src
    assert "STAY ON THE OBJECTIVE" in src


def test_still_needed_renders_help(monkeypatch) -> None:
    from orchestrator.onboarding.turn_brain import _fmt_still_needed

    out = _fmt_still_needed([{
        "field": "operating_hours", "kind": "gap",
        "prompt_en": "Working hours?", "help_en": "When can customers reach you?",
    }])
    assert "(meaning: When can customers reach you?)" in out
