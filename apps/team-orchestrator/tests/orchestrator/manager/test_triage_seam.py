"""VT-606 (team-lead ruling round 2) — the triage seam's legacy-mode pin (pure, no DB). The
shadow/enforce (DB-backed) coverage is in ``test_triage_seam_db.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("anthropic")

from orchestrator.manager import triage_seam as ts  # noqa: E402


def test_legacy_mode_never_calls_triage(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pin: legacy mode must not even IMPORT triage.triage_turn, let alone call it — the hot
    path stays byte-identical to pre-VT-606."""
    called = {"n": 0}

    def _boom(*a, **k):  # would fail loudly if ever reached
        called["n"] += 1
        raise AssertionError("triage_turn must NEVER be called in legacy mode")

    monkeypatch.setattr("orchestrator.manager.triage.triage_turn", _boom)

    result = ts.triage_seam(uuid4(), "hello", "SMxxx", mode="legacy")

    assert called["n"] == 0
    assert result.outcome is None
    assert result.task_id is None
    assert result.skip_legacy_dispatch is False


def test_legacy_mode_no_op_result_is_a_singleton() -> None:
    """No DB access at all in legacy mode — a bogus tenant_id must not raise."""
    result = ts.triage_seam(uuid4(), "anything", "SMxxx", mode="legacy")
    assert result is ts._NO_OP


def test_triage_seam_result_direct_reply_defaults_none() -> None:
    """Shared infra (Step 1) — the new direct_reply_text field defaults None so every existing
    3-arg construction stays byte-compatible, and _NO_OP carries no reply."""
    r = ts.TriageSeamResult(outcome=None, task_id=None, skip_legacy_dispatch=False)
    assert r.direct_reply_text is None
    assert ts._NO_OP.direct_reply_text is None
    r2 = ts.TriageSeamResult(
        outcome="new_task", task_id=None, skip_legacy_dispatch=True, direct_reply_text="hello"
    )
    assert r2.direct_reply_text == "hello"


def test_legacy_mode_default_when_no_explicit_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAM_MANAGER_LOOP_MODE", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(
        "orchestrator.manager.triage.triage_turn", lambda **k: called.__setitem__("n", 1)
    )

    result = ts.triage_seam(uuid4(), "hello", "SMxxx")

    assert called["n"] == 0
    assert result is ts._NO_OP


def test_df5_question_shape_gate_pin() -> None:
    """Hook-caught regression pin: the DF5 net answers QUESTIONS only. classify_status_query is
    bag-of-words, so an IMPERATIVE carrying a count token ("win back lapsed customers") must NOT be
    question-shaped — it falls through to D3/triage (new_task), never a count answer."""
    from orchestrator.onboarding.campaign_first_contact import _INTERROGATIVE_LEAD_RE

    def q(t: str) -> bool:
        toks = set(t.lower().replace("?", " ").split())
        return "?" in t or bool(_INTERROGATIVE_LEAD_RE.match(t)) or bool(
            toks & {"kitne", "kitni", "kitna", "how"}
        )

    assert q("win back lapsed customers") is False
    assert q("run a win-back campaign for my lapsed customers") is False
    assert q("how many lapsed customers do I have?") is True
    assert q("total kitne customers hain jo lapse ho gaye?") is True
    assert q("what's the status?") is True
