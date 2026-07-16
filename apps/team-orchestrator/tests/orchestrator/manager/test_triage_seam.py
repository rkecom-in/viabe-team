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


# ── VT-657 (option C) — LLM-primary campaign-recovery routing (pure, no DB) ───────────────────────


def _stub_seam_reads(
    monkeypatch: pytest.MonkeyPatch, *, has_active_task: bool = False, open_qs=None
) -> None:
    """Neutralize the seam's DB reads (pending_questions / has_active_task / the DF5 status net) for
    a pure-logic no-DB test — the routing decision under test never depends on real DB state."""
    monkeypatch.setattr(
        "orchestrator.manager.pending_questions.get_open", lambda *a, **k: list(open_qs or [])
    )
    monkeypatch.setattr(
        "orchestrator.manager.task_store.has_active_task", lambda *a, **k: has_active_task
    )
    monkeypatch.setattr(
        "orchestrator.owner_inputs.status_query.answer_status_query", lambda *a, **k: None
    )


def _boom(*_a, **_k):
    raise AssertionError("this path must not be reached")


def test_vt657_llm_campaign_recovery_routes_through_shared_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new_task the LLM classifies as campaign_recovery routes through the SHARED
    _dispatch_campaign_first_contact (the D3 dispatch body), NOT the generic clarification plan that
    made the brain dither. The message deliberately does NOT trip the FROZEN keyword net
    (is_campaign_plan_imperative is False for "…offer to bring my past customers back" — the exact
    j02 phrasing D3 misses), so ONLY the LLM route can fire the dispatch."""
    from orchestrator.manager.triage import TriageResult

    _stub_seam_reads(monkeypatch)
    monkeypatch.setattr(
        "orchestrator.manager.triage.triage_turn",
        lambda **k: TriageResult(
            outcome="new_task", reasoning="win-back", task_kind="campaign_recovery"
        ),
    )
    sentinel = ts.TriageSeamResult(
        outcome="new_task", task_id=uuid4(), skip_legacy_dispatch=True
    )
    called = {"n": 0}

    def _fake_dispatch(tenant_id, message_text, message_sid):
        called["n"] += 1
        return sentinel

    monkeypatch.setattr(ts, "_dispatch_campaign_first_contact", _fake_dispatch)
    monkeypatch.setattr(ts, "_create_plan_for_new_task", _boom)  # generic plan must NOT build

    result = ts.triage_seam(
        uuid4(),
        "put together a Diwali offer to bring my past customers back in",
        "SMcamp",
        mode="enforce",
    )
    assert called["n"] == 1
    assert result is sentinel


def test_vt657_llm_general_new_task_skips_campaign_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A general new_task (task_kind default) must NOT hit the campaign dispatch — it builds the
    generic plan exactly as before (backward-compatible)."""
    from orchestrator.manager.triage import TriageResult

    _stub_seam_reads(monkeypatch)
    monkeypatch.setattr(
        "orchestrator.manager.triage.triage_turn",
        lambda **k: TriageResult(outcome="new_task", reasoning="connect data", task_kind="general"),
    )
    monkeypatch.setattr(ts, "_dispatch_campaign_first_contact", _boom)  # must NOT fire for general
    made = {"n": 0}
    monkeypatch.setattr(
        ts, "_create_plan_for_new_task", lambda *a, **k: made.__setitem__("n", 1) or None
    )

    result = ts.triage_seam(uuid4(), "connect my shopify store", "SMgen", mode="enforce")
    assert made["n"] == 1
    assert result.outcome == "new_task"
    assert result.skip_legacy_dispatch is False  # task_id None -> falls through to legacy


def test_vt657_campaign_recovery_not_dispatched_when_active_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The C route mirrors D3's guard: with an active task already owning the tenant, a
    campaign_recovery new_task does NOT mint a second campaign dispatch (never fires the helper) —
    it takes the generic path."""
    from orchestrator.manager.triage import TriageResult

    _stub_seam_reads(monkeypatch, has_active_task=True)
    monkeypatch.setattr(
        "orchestrator.manager.triage.triage_turn",
        lambda **k: TriageResult(
            outcome="new_task", reasoning="win-back", task_kind="campaign_recovery"
        ),
    )
    monkeypatch.setattr(ts, "_dispatch_campaign_first_contact", _boom)  # active task -> no dispatch
    monkeypatch.setattr(ts, "_create_plan_for_new_task", lambda *a, **k: None)

    result = ts.triage_seam(
        uuid4(),
        "put together a Diwali offer to bring my past customers back in",
        "SMact",
        mode="enforce",
    )
    assert result.outcome == "new_task"
