"""VT-697 — perceived-latency fixes: the deterministic TAP fast-path + the typing indicator.

Fazal (live run): "there is a wait time of minimum 10 secs or more". A quick-reply tap echoes
the suggestion text verbatim; when the inbound body exactly matches a suggestion of the
presented gap question, the journey records + advances + presents the NEXT question with no
LLM on the hot path. Typed free text and Skip taps keep the full brain. The typing indicator
(Twilio v3, Public Beta) fires at ingress fail-soft on a daemon thread.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

from orchestrator.onboarding import journey as j  # noqa: E402

_Q_GAP = {
    "field": "payment_terms", "kind": "gap",
    "prompt_en": "How do customers usually pay you?", "prompt_hi": "?",
    "suggestions_en": ["Advance payment", "On delivery", "Credit period"],
    "suggestions_hi": [],
}
_Q_NEXT = {
    "field": "team_size", "kind": "gap",
    "prompt_en": "How big is your team?", "prompt_hi": "?",
    "suggestions_en": ["Just me", "2-5 people", "6+"], "suggestions_hi": [],
}


def _wire_state(monkeypatch, queue, *, answers=None):
    state = {"status": "active", "cursor": 0, "last_message_sid": None,
             "question_queue": list(queue), "answers": dict(answers or {}), "skipped": []}
    monkeypatch.setattr(j, "get_journey", lambda t: dict(state))
    advanced: dict = {}

    def _adv(t, cursor, answers, skipped, sid):
        advanced.update(cursor=cursor, answers=dict(answers), sid=sid)
        state["cursor"] = cursor
        state["answers"] = dict(answers)

    monkeypatch.setattr(j, "_advance", _adv)
    monkeypatch.setattr(j, "_append_recent_turns", lambda *a, **k: None)
    monkeypatch.setattr(j, "_install_recomposed_queue", lambda *a, **k: None)
    import orchestrator.onboarding.draft_profile as dp

    monkeypatch.setattr(dp, "get_draft", lambda t: {"attributes": {}, "provenance": {}})
    return state, advanced


def _brain_bomb(monkeypatch):
    """compose_turn must NOT be reached on the fast path."""
    import orchestrator.onboarding.turn_brain as tb

    def _boom(*a, **k):
        raise AssertionError("LLM reached on the deterministic tap fast-path")

    monkeypatch.setattr(tb, "compose_turn", _boom)


def test_tap_records_and_presents_next_without_llm(monkeypatch) -> None:
    tid = str(uuid4())
    state, advanced = _wire_state(monkeypatch, [_Q_GAP, _Q_NEXT])
    _brain_bomb(monkeypatch)
    r = j._handle_reply_with_turn_brain(tid, "On delivery", "SMtap1", lang="en")
    assert advanced["answers"]["payment_terms"] == "On delivery"
    assert r["next_q"]["field"] == "team_size", "next question presented deterministically"
    assert r["reply_en"] == "How big is your team?"
    assert r.get("done") is False


def test_tap_match_is_case_insensitive_exact(monkeypatch) -> None:
    tid = str(uuid4())
    _, advanced = _wire_state(monkeypatch, [_Q_GAP, _Q_NEXT])
    _brain_bomb(monkeypatch)
    j._handle_reply_with_turn_brain(tid, "  advance payment ", "SMtap2", lang="en")
    assert advanced["answers"]["payment_terms"] == "advance payment"


def test_typed_free_text_keeps_the_brain(monkeypatch) -> None:
    tid = str(uuid4())
    _, advanced = _wire_state(monkeypatch, [_Q_GAP, _Q_NEXT])
    import orchestrator.onboarding.turn_brain as tb

    called = {}
    plan = SimpleNamespace(reply_text="Got it.", buttons=(), extracted_answers={},
                           mark_confirmed=[], mark_rejected=[], done_hint=False, reasoning="")
    monkeypatch.setattr(tb, "compose_turn", lambda *a, **k: (called.__setitem__("hit", True), plan)[1])
    monkeypatch.setattr(j, "populate_profile_from_draft", lambda t: {})
    monkeypatch.setattr(j, "_capture_missed_about_gap", lambda *a, **k: None)
    monkeypatch.setattr(j, "_advance_cursor_past_answered", lambda g, a, s: 1)
    j._handle_reply_with_turn_brain(tid, "mostly UPI, sometimes cash on delivery", "SMtap3", lang="en")
    assert called.get("hit") is True, "non-exact text is the LLM's to interpret"


def test_skip_tap_keeps_the_brain(monkeypatch) -> None:
    q = dict(_Q_GAP, suggestions_en=["Advance payment", "Skip"])
    tid = str(uuid4())
    _wire_state(monkeypatch, [q, _Q_NEXT])
    import orchestrator.onboarding.turn_brain as tb

    called = {}
    plan = SimpleNamespace(reply_text="Skipped.", buttons=(), extracted_answers={},
                           mark_confirmed=[], mark_rejected=[], done_hint=False, reasoning="")
    monkeypatch.setattr(tb, "compose_turn", lambda *a, **k: (called.__setitem__("hit", True), plan)[1])
    monkeypatch.setattr(j, "populate_profile_from_draft", lambda t: {})
    monkeypatch.setattr(j, "_capture_missed_about_gap", lambda *a, **k: None)
    monkeypatch.setattr(j, "_advance_cursor_past_answered", lambda g, a, s: 1)
    j._handle_reply_with_turn_brain(tid, "Skip", "SMtap4", lang="en")
    assert called.get("hit") is True, "Skip semantics stay with the existing machinery"


def test_confirm_questions_never_fast_path(monkeypatch) -> None:
    card = {"field": "business_type", "kind": "confirm", "prompt_en": "Services — right?",
            "prompt_hi": "?", "suggestions_en": ["Services"]}
    tid = str(uuid4())
    _wire_state(monkeypatch, [card, _Q_NEXT])
    import orchestrator.onboarding.turn_brain as tb

    called = {}
    plan = SimpleNamespace(reply_text="OK.", buttons=(), extracted_answers={},
                           mark_confirmed=[], mark_rejected=[], done_hint=False, reasoning="")
    monkeypatch.setattr(tb, "compose_turn", lambda *a, **k: (called.__setitem__("hit", True), plan)[1])
    monkeypatch.setattr(j, "populate_profile_from_draft", lambda t: {})
    monkeypatch.setattr(j, "_capture_missed_about_gap", lambda *a, **k: None)
    monkeypatch.setattr(j, "_advance_cursor_past_answered", lambda g, a, s: 1)
    j._handle_reply_with_turn_brain(tid, "Services", "SMtap5", lang="en")
    assert called.get("hit") is True, "confirms carry value-promotion semantics — never fast-pathed"


# --- the typing indicator ---------------------------------------------------------------------


def _inline_threads(monkeypatch):
    import threading

    class _Inline:
        def __init__(self, target=None, **k):
            self._t = target

        def start(self):
            self._t()

    monkeypatch.setattr(threading, "Thread", _Inline)


def test_typing_indicator_posts_v3(monkeypatch) -> None:
    from orchestrator.utils import twilio_send as ts

    monkeypatch.setenv("TEAM_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TEAM_TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.delenv("TEAM_TWILIO_MOCK_MODE", raising=False)
    _inline_threads(monkeypatch)
    seen = {}
    import urllib.request as ur

    class _Resp:
        def read(self):
            return b"{}"

    def _open(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = req.data.decode()
        return _Resp()

    monkeypatch.setattr(ur, "urlopen", _open)
    ts.send_typing_indicator("SMabc123")
    assert seen["url"] == "https://messaging.twilio.com/v3/Indicators/Typing.json"
    assert '"messageId": "SMabc123"' in seen["body"] and '"channel": "WHATSAPP"' in seen["body"]


def test_typing_indicator_noops_safely(monkeypatch) -> None:
    from orchestrator.utils import twilio_send as ts

    _inline_threads(monkeypatch)
    import urllib.request as ur

    def _never(*a, **k):
        raise AssertionError("must not reach the network")

    monkeypatch.setattr(ur, "urlopen", _never)
    monkeypatch.delenv("TEAM_TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TEAM_TWILIO_AUTH_TOKEN", raising=False)
    ts.send_typing_indicator("SMabc")  # no creds → silent no-op
    monkeypatch.setenv("TEAM_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TEAM_TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TEAM_TWILIO_MOCK_MODE", "1")
    ts.send_typing_indicator("SMabc")  # mock mode → log-only
    ts.send_typing_indicator("")  # no sid → no-op


def test_typing_indicator_network_failure_is_soft(monkeypatch) -> None:
    from orchestrator.utils import twilio_send as ts

    monkeypatch.setenv("TEAM_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TEAM_TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.delenv("TEAM_TWILIO_MOCK_MODE", raising=False)
    _inline_threads(monkeypatch)
    import urllib.request as ur

    def _boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(ur, "urlopen", _boom)
    ts.send_typing_indicator("SMabc")  # must not raise
