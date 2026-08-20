"""VT-724 — owner email at onboarding: the two-phase echo-confirm turn, question presence,
skip legality, and the retroactive consent-record wiring."""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("dbos")

from orchestrator.onboarding import journey as j  # noqa: E402

_Q = {"field": "owner_email", "kind": "gap", "prompt_en": "email?", "prompt_hi": "email?"}


def _toks(s: str):
    return j._tokens(s)


def test_invalid_email_re_presents_no_record(monkeypatch):
    answers: dict = {}
    out = j._handle_owner_email_turn(uuid4(), _Q, "my email is shop at gmail", _toks("x"), answers, [])
    assert out is not None and out["re_present"] is True
    assert "doesn't look like an email" in out["reply_en"]
    assert answers == {}


def test_valid_email_holds_pending_and_echoes(monkeypatch):
    saved = []
    monkeypatch.setattr(j, "_write_answers_skipped", lambda t, a, s: saved.append(dict(a)))
    answers: dict = {}
    out = j._handle_owner_email_turn(
        uuid4(), _Q, "sure — Shop.Owner+viabe@Gmail.com works", _toks("x"), answers, []
    )
    assert out is not None and out["re_present"] is True
    assert "shop.owner+viabe@gmail.com" in out["reply_en"]  # echoed, lowercased
    assert answers[j._PENDING_EMAIL_KEY] == "shop.owner+viabe@gmail.com"
    assert saved and j._PENDING_EMAIL_KEY in saved[0]  # pending survives restarts
    assert "owner_email" not in answers  # NOT recorded yet — no send may fire


def test_yes_confirms_persists_and_fires_retro_send(monkeypatch):
    persisted = []
    fired = []

    class _Conn:
        def execute(self, sql, params=None):
            persisted.append((sql, params))

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(j, "tenant_connection", lambda t: _Ctx())
    monkeypatch.setattr(
        "orchestrator.onboarding.consent_record_email.send_pending_consent_record",
        lambda t: fired.append(t) or {"sent": True},
    )
    answers = {j._PENDING_EMAIL_KEY: "owner@example.com"}
    out = j._handle_owner_email_turn(uuid4(), _Q, "Yes", _toks("Yes"), answers, [])
    assert out is None  # falls through to the shared record-and-advance
    assert answers["owner_email"] == "owner@example.com"
    assert j._PENDING_EMAIL_KEY not in answers
    assert any("owner_email" in (sql or "") for sql, _ in persisted)
    assert fired  # the retroactive consent-record send got its first caller


def test_new_address_replaces_pending(monkeypatch):
    monkeypatch.setattr(j, "_write_answers_skipped", lambda t, a, s: None)
    answers = {j._PENDING_EMAIL_KEY: "old@example.com"}
    out = j._handle_owner_email_turn(uuid4(), _Q, "no use new@example.com", _toks("no"), answers, [])
    assert out is not None
    assert answers[j._PENDING_EMAIL_KEY] == "new@example.com"


def test_non_yes_non_email_with_pending_nudges(monkeypatch):
    answers = {j._PENDING_EMAIL_KEY: "owner@example.com"}
    out = j._handle_owner_email_turn(uuid4(), _Q, "hmm", _toks("hmm"), answers, [])
    assert out is not None and "owner@example.com" in out["reply_en"]


def test_compose_appends_budget_exempt_email_question():
    from orchestrator.onboarding.question_brain import compose_onboarding_questions

    qs = compose_onboarding_questions("kirana", None, [], llm_fn=lambda *a: [])
    fields = [q.field for q in qs]
    assert fields[-1] == "owner_email"  # asked LAST, present even with zero gaps
    qs2 = compose_onboarding_questions("kirana", None, ["owner_email"], llm_fn=lambda *a: [])
    assert "owner_email" not in [q.field for q in qs2]  # answered → never re-asked


def test_scratch_queue_carries_email():
    from orchestrator.onboarding.whatsapp_signup import from_scratch_question_queue

    fields = [q["field"] for q in from_scratch_question_queue()]
    assert "owner_email" in fields
