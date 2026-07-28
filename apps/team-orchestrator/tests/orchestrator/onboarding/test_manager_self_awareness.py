"""VT-716 — the Manager knows what it just sent (run-5 findings #2/#3/#4).

Pins: the wire-truth merged history (welcome/system sends visible to the brain), the
typed-twice guard (a human repeat never re-processes), and the continuation prompt rule.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

from orchestrator.onboarding import journey as j  # noqa: E402

_TID = str(uuid4())


def test_merged_history_unions_wire_truth(monkeypatch) -> None:
    import orchestrator.conversation_log as cl

    monkeypatch.setattr(
        cl, "active_window",
        lambda t, **k: [
            {"role": "owner", "text": "Hi", "created_at": None, "surface": "signup"},
            {"role": "assistant", "text": "Welcome! Account created.", "created_at": None, "surface": "signup"},
        ],
    )
    g = {"recent_turns": [{"role": "bot", "text": "What's your business called?"}]}
    merged = j._merged_recent_history(_TID, g, "SMx")
    texts = [(t["role"], t["text"]) for t in merged]
    assert ("owner", "Hi") in texts
    assert ("bot", "Welcome! Account created.") in texts, "the WELCOME is visible to the brain"
    assert ("bot", "What's your business called?") in texts
    assert texts.index(("owner", "Hi")) == 0, "chronological — wire history first"


def test_merged_history_dedupes_and_fails_open(monkeypatch) -> None:
    import orchestrator.conversation_log as cl

    monkeypatch.setattr(
        cl, "active_window",
        lambda t, **k: [{"role": "assistant", "text": "Same line", "created_at": None, "surface": "journey"}],
    )
    g = {"recent_turns": [{"role": "bot", "text": "Same line"}]}
    assert len(j._merged_recent_history(_TID, g, None)) == 1, "dedup by (role, text)"

    def _boom(t, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(cl, "active_window", _boom)
    assert j._merged_recent_history(_TID, g, None) == [{"role": "bot", "text": "Same line"}]


def _wire_guard(monkeypatch, *, last_owner_text: str, age_s: int, queue):
    state = {"status": "active", "cursor": 0, "last_message_sid": "SMold",
             "question_queue": list(queue), "answers": {}, "skipped": []}
    monkeypatch.setattr(j, "get_journey", lambda t: dict(state))
    monkeypatch.setattr(j, "_append_recent_turns", lambda *a, **k: None)
    import orchestrator.conversation_log as cl

    monkeypatch.setattr(
        cl, "active_window",
        lambda t, **k: [{
            "role": "owner", "text": last_owner_text,
            "created_at": datetime.now(UTC) - timedelta(seconds=age_s), "surface": "journey",
        }],
    )
    import orchestrator.onboarding.turn_brain as tb

    def _boom(*a, **k):
        raise AssertionError("brain must not run on a typed-twice repeat")

    monkeypatch.setattr(tb, "compose_turn", _boom)
    return state


_Q = {"field": "owner_name", "kind": "gap", "prompt_en": "And your name?", "prompt_hi": "?"}


def test_typed_twice_acks_without_reprocessing(monkeypatch) -> None:
    _wire_guard(monkeypatch, last_owner_text="RKeCom Services Pvt Ltd", age_s=30, queue=[_Q])
    r = j._handle_reply_with_turn_brain(_TID, "RKeCom Services Pvt Ltd", "SMnew", lang="en")
    assert r is not None and r.get("next_q") == _Q
    assert r["reply_en"].startswith("Got it — already noted."), "brief ack + re-present, no re-run"


def test_typed_twice_ignores_old_repeats(monkeypatch) -> None:
    _wire_guard(monkeypatch, last_owner_text="RKeCom Services Pvt Ltd", age_s=600, queue=[_Q])
    # 10-minute-old repeat is NOT typed-twice — processing continues to the brain path.
    import orchestrator.onboarding.turn_brain as tb
    from types import SimpleNamespace as _NS

    called = {}
    monkeypatch.setattr(
        tb, "compose_turn",
        lambda *a, **k: (called.__setitem__("hit", True),
                         _NS(reply_text="Normal turn.", buttons=(), extracted_answers={},
                             mark_confirmed=[], mark_rejected=[], done_hint=False, reasoning=""))[1],
    )
    monkeypatch.setattr(j, "populate_profile_from_draft", lambda t: {})
    monkeypatch.setattr(j, "_apply_turn_plan", lambda t, g, plan, d: ({}, []))
    monkeypatch.setattr(j, "_capture_missed_about_gap", lambda *a, **k: None)
    monkeypatch.setattr(j, "_advance_cursor_past_answered", lambda g, a, s: 0)
    monkeypatch.setattr(j, "_advance", lambda *a, **k: None)
    import orchestrator.onboarding.draft_profile as dp

    monkeypatch.setattr(dp, "get_draft", lambda t: {"attributes": {}, "provenance": {}})
    r = j._handle_reply_with_turn_brain(_TID, "RKeCom Services Pvt Ltd", "SMnew2", lang="en")
    assert called.get("hit") is True, "old repeat reaches the brain normally"
    assert not str(r.get("reply_en", "")).startswith("Got it — already noted.")


def test_prompt_carries_continuation_rule() -> None:
    import inspect

    from orchestrator.onboarding import turn_brain as tb

    src = inspect.getsource(tb)
    assert "ONE CONTINUOUS MANAGER" in src
    assert "never re-greet" in src


def test_present_first_question_is_deterministic_continuation(monkeypatch) -> None:
    """VT-716b: the kickoff presents the seeded first question via the walker send — no LLM,
    no synthetic owner turn."""
    sent = {}
    monkeypatch.setattr(
        j, "get_journey",
        lambda t: {"status": "active", "cursor": 0,
                   "question_queue": [{"field": "business_name", "kind": "gap",
                                       "prompt_en": "What's your business called?", "prompt_hi": "?"}],
                   "answers": {}, "skipped": []},
    )
    monkeypatch.setattr(j, "_send", lambda recipient, q, lang, *, tenant_id=None: sent.update(q=q))
    assert j.present_first_question(_TID, "+919999007001") is True
    assert sent["q"]["prompt_en"] == "What's your business called?"


def test_signup_kickoff_has_no_synthetic_owner_turn() -> None:
    import inspect

    from orchestrator.onboarding import whatsapp_signup as ws

    src = inspect.getsource(ws.handle_unknown_inbound)
    assert '"complete setup"' not in src, "the fake owner request is gone"
    assert "present_first_question" in src
