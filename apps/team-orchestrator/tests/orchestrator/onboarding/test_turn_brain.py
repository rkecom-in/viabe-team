"""VT-569 — pure unit tests for the onboarding TURN-BRAIN (``orchestrator.onboarding.turn_brain``).

No DB, no live LLM: the single LLM call is behind ``_invoke_llm``, which these tests monkeypatch to
return canned JSON, so the prompt-build → parse → validate path is exercised deterministically. The
load-bearing behaviours pinned here:

  - a bare "No" to a confirm yields a NON-identical, alternative-offering reply + ``mark_rejected``
    (never the identical question — the live dead-end);
  - multi-field extraction from ONE message surfaces every field in ``extracted_answers``;
  - a raising / empty / unparseable LLM degrades to ``None`` (the caller then falls back to the walker);
  - buttons are hard-capped at 3 (Meta limit);
  - claim-grounding: the prompt enumerates ONLY discovered facts (with provenance).
"""

from __future__ import annotations

import json
from typing import Any

from orchestrator.onboarding import turn_brain
from orchestrator.onboarding.turn_brain import TurnPlan, compose_turn


def _canned(monkeypatch: Any, payload: dict[str, Any] | str) -> list[str]:
    """Monkeypatch the LLM call to return ``payload`` (JSON-encoded if a dict). Returns a list that
    captures the (system, user) prompts the brain built, for prompt-content assertions."""
    captured: list[str] = []
    raw = payload if isinstance(payload, str) else json.dumps(payload)

    def _fake(system_prompt: str, user_prompt: str) -> str:
        captured.append(system_prompt)
        captured.append(user_prompt)
        return raw

    monkeypatch.setattr(turn_brain, "_invoke_llm", _fake)
    return captured


_CONFIRM_STATE = {
    "question_queue": [
        {"field": "business_type", "kind": "confirm",
         "prompt_en": "We found you're a Local services business — is that right?",
         "draft_value": "services"},
        {"field": "operating_hours", "kind": "gap", "prompt_en": "What are your hours?"},
    ],
    "cursor": 0,
    "answers": {},
    "skipped": [],
}


def test_no_to_confirm_is_non_identical_and_marks_rejected(monkeypatch):
    """A bare 'No' to a confirm: the brain's reply is DIFFERENT from the confirm question and it flags
    the field rejected + offers alternatives (the anti-dead-end contract at the LLM layer)."""
    _canned(monkeypatch, {
        "reply_text": "No worries — so what kind of business is it? Retail, or something else?",
        "buttons": ["Retail", "Manufacturing", "Other"],
        "extracted_answers": {},
        "mark_confirmed": [],
        "mark_rejected": ["business_type"],
        "done_hint": False,
        "reasoning": "owner rejected the discovered type; ask for the real one",
    })
    plan = compose_turn(_CONFIRM_STATE, {"business_type": "services"}, "No", locale="en")
    assert isinstance(plan, TurnPlan)
    assert plan.reply_text != _CONFIRM_STATE["question_queue"][0]["prompt_en"], (
        "the reply must NOT be the identical confirm question (the dead-end)"
    )
    assert "business_type" in plan.mark_rejected
    assert plan.buttons == ("Retail", "Manufacturing", "Other")


def test_multi_field_extraction_from_one_message(monkeypatch):
    """The brain can extract SEVERAL fields from one owner message — the whole point of the freedom
    (fewer turns, less burden). Every field surfaces in extracted_answers for the recorders."""
    _canned(monkeypatch, {
        "reply_text": "Perfect, noted! Anything else you'd like me to know?",
        "buttons": [],
        "extracted_answers": {"operating_hours": "9am-9pm", "city": "Pune", "price_range": "budget"},
        "mark_confirmed": [],
        "mark_rejected": [],
        "done_hint": False,
        "reasoning": "owner volunteered three facts",
    })
    plan = compose_turn(_CONFIRM_STATE, {"business_type": "services"},
                        "we're open 9am-9pm in Pune, budget prices", locale="en")
    assert plan is not None
    assert plan.extracted_answers == {"operating_hours": "9am-9pm", "city": "Pune", "price_range": "budget"}


def test_llm_failure_returns_none(monkeypatch):
    """A raising LLM → compose_turn returns None (the fail-soft signal; the caller runs the walker)."""
    def _boom(system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("llm down")

    monkeypatch.setattr(turn_brain, "_invoke_llm", _boom)
    assert compose_turn(_CONFIRM_STATE, {}, "hi", locale="en") is None


def test_empty_and_unparseable_reply_return_none(monkeypatch):
    """An empty reply_text, empty output, and non-JSON output all degrade to None (fall back)."""
    _canned(monkeypatch, {"reply_text": "", "buttons": []})
    assert compose_turn(_CONFIRM_STATE, {}, "hi", locale="en") is None
    _canned(monkeypatch, "")
    assert compose_turn(_CONFIRM_STATE, {}, "hi", locale="en") is None
    _canned(monkeypatch, "the model refused to emit json")
    assert compose_turn(_CONFIRM_STATE, {}, "hi", locale="en") is None


def test_buttons_hard_capped_at_three(monkeypatch):
    """More than 3 requested buttons → truncated to 3 (WhatsApp/Meta quick-reply hard limit)."""
    _canned(monkeypatch, {
        "reply_text": "Which one?",
        "buttons": ["A", "B", "C", "D", "E"],
        "extracted_answers": {},
    })
    plan = compose_turn(_CONFIRM_STATE, {}, "options?", locale="en")
    assert plan is not None
    assert len(plan.buttons) == 3
    assert plan.buttons == ("A", "B", "C")


def test_prompt_is_claim_grounded_with_provenance(monkeypatch):
    """The user prompt enumerates ONLY discovered facts, tagged with provenance — the claim-grounding
    substrate (the brain is told these are the only facts it may state)."""
    captured = _canned(monkeypatch, {"reply_text": "Hi!", "extracted_answers": {}})
    compose_turn(
        _CONFIRM_STATE,
        {"business_type": "services", "city": "Pune"},
        "hello",
        locale="en",
        provenance={"business_type": {"source": "gbp", "reasoning": "domain wins over category"}},
        is_start=True,
    )
    system_prompt, user_prompt = captured[0], captured[1]
    assert "ONLY facts you may state" in user_prompt
    assert "business_type: services (source: gbp; domain wins over category)" in user_prompt
    assert "city: Pune" in user_prompt
    assert "FIRST message" in user_prompt, "the start turn instructs a single warm greeting"
    assert "NEVER invent a business fact" in system_prompt


def test_extraction_values_stringified_and_empties_dropped(monkeypatch):
    """Extracted values are coerced to trimmed strings and empty values are dropped (never recorded)."""
    _canned(monkeypatch, {
        "reply_text": "ok",
        "extracted_answers": {"a": "  spaced  ", "b": "", "c": None, "d": 42},
    })
    plan = compose_turn(_CONFIRM_STATE, {}, "x", locale="en")
    assert plan is not None
    assert plan.extracted_answers == {"a": "spaced", "d": "42"}


# --- VT-571: the distilled-memory block (mig 163) — compact, don't drop --------------------------


def test_build_prompts_renders_distilled_memory_when_present():
    """When the journey carries a non-empty ``conversation_summary``, the user prompt renders a
    'CONVERSATION SO FAR (distilled memory …)' block ABOVE the raw recent-conversation window."""
    from orchestrator.onboarding.turn_brain import _build_prompts

    state = {
        "question_queue": [{"field": "about", "kind": "gap", "prompt_en": "Tell me about it"}],
        "cursor": 0, "answers": {}, "skipped": [],
        "recent_turns": [{"role": "owner", "text": "hi"}],
        "conversation_summary": "Owner runs a bakery in Pune; prefers Hinglish; wants festival promos.",
    }
    _, user = _build_prompts(state, {}, "hello", locale="en", provenance=None, is_start=False)
    assert "CONVERSATION SO FAR (distilled memory" in user
    assert "Owner runs a bakery in Pune; prefers Hinglish; wants festival promos." in user
    assert user.index("CONVERSATION SO FAR") < user.index("RECENT CONVERSATION"), (
        "the distilled memory must sit ABOVE the raw recent window"
    )


def test_build_prompts_omits_distilled_memory_when_absent():
    """No summary (None or empty) → the distilled block is omitted entirely (no empty header)."""
    from orchestrator.onboarding.turn_brain import _build_prompts

    for summary in (None, "", "   "):
        state = {
            "question_queue": [{"field": "about", "kind": "gap", "prompt_en": "x"}],
            "cursor": 0, "answers": {}, "skipped": [],
            "recent_turns": [], "conversation_summary": summary,
        }
        _, user = _build_prompts(state, {}, "hello", locale="en", provenance=None, is_start=False)
        assert "distilled memory" not in user
        assert "CONVERSATION SO FAR" not in user
