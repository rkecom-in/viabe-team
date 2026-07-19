"""VT-462 — the onboarding-CONDUCTOR dynamic next-question decision + deterministic completion.

The conductor decides WHAT to ask next DYNAMICALLY, bounded by the prereq registry (via
``question_brain.compose_onboarding_questions`` candidates), and NEVER self-declares "complete" — a
deterministic check (``profile_collection_complete``) owns that. These tests pin the load-bearing
invariants with an INJECTED ``llm_fn`` (no live Anthropic call): the candidate source is exercised
deterministically, exactly like ``test_question_brain.py``.

Properties under test:
  1. registry-grounded next question, decided dynamically (confirm-first, then gaps);
  2. volunteered / out-of-order answers are never re-asked;
  3. skip = deferred (revisit-later), not re-pressed every turn;
  4. the conductor NEVER self-marks complete — completion is the deterministic check, and it is true
     IFF no registry-bounded question remains unanswered/unskipped;
  5. resumes from journey state (the decision is a pure function of answers/skipped/draft).
"""

from __future__ import annotations

from typing import Any

from orchestrator.onboarding.conductor import (
    ConductorDecision,
    decide_next_question,
    profile_collection_complete,
)


def _gaps(*fields: str):
    """An injected llm_fn returning the given gap fields (deterministic candidate source)."""

    def _fn(business_type: str, draft_attrs: dict[str, Any], answered: list[str]) -> list[dict[str, Any]]:
        return [
            {"field": f, "prompt_en": f"What is your {f}?", "prompt_hi": f"आपका {f} क्या है?"}
            for f in fields
        ]

    return _fn


def _draft(**attrs: Any) -> dict[str, Any]:
    return {"attributes": dict(attrs), "provenance": {}}


# --- (1) registry-grounded dynamic next question ---------------------------------------------------


def test_next_question_is_confirm_first_then_gap() -> None:
    """The dynamic pick = the first registry-grounded candidate: a confirm-the-draft question beats
    a gap-fill question (the never-assert ordering is preserved)."""
    decision = decide_next_question(
        business_type="restaurant",
        draft=_draft(category="restaurant", city="Pune"),
        answered=[],
        skipped=[],
        llm_fn=_gaps("operating_hours"),
    )
    assert isinstance(decision, ConductorDecision)
    assert decision.next_question is not None
    # category is a confirmable draft field — it comes before the operating_hours gap.
    assert decision.next_question.field == "category"
    assert decision.next_question.kind == "confirm"
    # the full remaining set is registry-bounded (confirm fields + the one gap).
    fields = [q.field for q in decision.remaining]
    assert fields == ["category", "city", "operating_hours"]


def test_next_question_advances_to_gap_once_confirms_answered() -> None:
    """Once the confirm-the-draft fields are answered, the next question is the gap — recomputed
    from current state, not a frozen cursor."""
    decision = decide_next_question(
        business_type="restaurant",
        draft=_draft(category="restaurant", city="Pune"),
        answered=["category", "city"],
        skipped=[],
        llm_fn=_gaps("operating_hours"),
    )
    assert decision.next_question is not None
    assert decision.next_question.field == "operating_hours"
    assert decision.next_question.kind == "gap"


# --- (2) volunteered / out-of-order info is never re-asked -----------------------------------------


def test_volunteered_out_of_order_field_not_reasked() -> None:
    """An owner who answered a LATER field (out of order) / volunteered a field is never re-asked
    it — the answered field is dropped from the candidate set at source."""
    # The owner volunteered operating_hours before any confirm question was asked.
    decision = decide_next_question(
        business_type="restaurant",
        draft=_draft(category="restaurant", city="Pune"),
        answered=["operating_hours"],
        skipped=[],
        llm_fn=_gaps("operating_hours", "price_range"),
    )
    remaining_fields = [q.field for q in decision.remaining]
    assert "operating_hours" not in remaining_fields  # never re-asked
    assert "price_range" in remaining_fields  # still needed
    # The next question is still confirm-first among the remaining.
    assert decision.next_question is not None
    assert decision.next_question.field == "category"


# --- (3) skip = deferred (revisit-later), not re-pressed -------------------------------------------


def test_skipped_field_is_deferred_not_repressed() -> None:
    """A skipped field is deferred: excluded from the next-question stream (revisit_skipped=False)
    so the owner isn't re-pressed every turn — but it reappears on a revisit pass."""
    base = dict(
        business_type="restaurant",
        draft=_draft(category="restaurant"),
        answered=["category"],
        skipped=["operating_hours"],
        llm_fn=_gaps("operating_hours", "price_range"),
    )
    # Default pass: the skipped field is deferred; next is the un-skipped gap.
    decision = decide_next_question(**base)
    assert decision.next_question is not None
    assert decision.next_question.field == "price_range"
    assert "operating_hours" not in [q.field for q in decision.remaining]

    # Revisit pass: the skipped field reappears (the owner CAN be re-offered it at the end).
    revisit = decide_next_question(**{**base, "revisit_skipped": True})
    assert "operating_hours" in [q.field for q in revisit.remaining]


# --- (4) the conductor NEVER self-marks complete — the deterministic check owns it -----------------


def test_completion_is_deterministic_not_self_declared() -> None:
    """``profile_collection_complete`` is true IFF no registry-bounded question remains
    unanswered/unskipped — a pure function of state, never the brain's verdict."""
    # Still has an unanswered confirm + gap -> NOT complete.
    not_done = profile_collection_complete(
        business_type="restaurant",
        draft=_draft(category="restaurant", city="Pune"),
        answered=[],
        skipped=[],
        llm_fn=_gaps("operating_hours"),
    )
    assert not_done is False

    # All confirms answered + the gap skipped (an owner decision to omit) -> complete.
    done = profile_collection_complete(
        business_type="restaurant",
        draft=_draft(category="restaurant", city="Pune"),
        answered=["category", "city"],
        skipped=["operating_hours"],
        llm_fn=_gaps("operating_hours"),
    )
    assert done is True


def test_next_question_none_signals_but_does_not_self_complete() -> None:
    """When no registry-bounded question remains, ``decide_next_question`` returns next_question=None
    — a SIGNAL. The COMPLETION verdict is the separate deterministic function's, never the
    conductor's reasoning (they are distinct callables by design)."""
    decision = decide_next_question(
        business_type="restaurant",
        draft=_draft(category="restaurant"),
        answered=["category"],
        skipped=[],
        llm_fn=_gaps(),  # no gaps
    )
    assert decision.next_question is None
    # The deterministic check agrees (same state) — but it is a DIFFERENT function call: the
    # conductor signals, the check decides.
    assert profile_collection_complete(
        business_type="restaurant",
        draft=_draft(category="restaurant"),
        answered=["category"],
        skipped=[],
        llm_fn=_gaps(),
    ) is True


# --- (5) the decision is a pure function of state (resumability) -----------------------------------


def test_decision_is_pure_function_of_state() -> None:
    """The same (draft, answered, skipped) always yields the same decision — so a fresh inbound
    (each WhatsApp message is a new thread) resumes exactly where the owner left off."""
    args = dict(
        business_type="salon",
        draft=_draft(category="salon", city="Indore"),
        answered=["category"],
        skipped=[],
        llm_fn=_gaps("services"),
    )
    a = decide_next_question(**args)
    b = decide_next_question(**args)
    assert a.next_question is not None and b.next_question is not None
    assert a.next_question.field == b.next_question.field == "city"
    assert a.known == b.known == ("category",)
