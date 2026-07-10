"""T8 — runner._maybe_resurface_pending_approval.

On a RESUME cue ("do what you were saying / continue") that lands while an approval is already
armed, the turn must RE-SURFACE that approval and be consumed here — NOT fall through to
triage/new_task (which spawned a competing plan: the confirmed §2 ignored_speech_act/wrong_action/
loop_stall on m_conversation_interruption_midtask_resume_winback). Complements T5, which refuses to
auto-SEND on the same vague reply.

Pure-logic: the helper's only side effects (open-approval read, owner send, locale) are
monkeypatched, so no DB / network / DBOS launch is needed beyond importing the module.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# orchestrator.runner pulls the dbos stack at import — skip cleanly in the dep-less smoke; the full
# orchestrator CI job (with dbos) runs it.
pytest.importorskip("dbos")

from orchestrator import runner  # noqa: E402 — after the dependency-skip guard


def _event(body: str, phone: str | None = "+919999999999") -> SimpleNamespace:
    return SimpleNamespace(body=body, sender_phone=phone)


@pytest.fixture
def sent(monkeypatch):
    """Capture owner sends without touching the network; default locale = en."""
    import orchestrator.owner_surface.freeform_acks as fa

    captured: list[tuple[str, str, str]] = []
    monkeypatch.setattr(fa, "send_freeform_ack", lambda t, r, b: captured.append((t, r, b)))
    monkeypatch.setattr(fa, "resolve_owner_locale", lambda t: "en")
    return captured


def test_resurface_fires_on_resume_cue_with_pending_approval(monkeypatch, sent):
    monkeypatch.setattr(runner, "_open_approval_exists_step", lambda t: True)
    ok = runner._maybe_resurface_pending_approval(
        "t1", _event("ok theek hai, chalo jo pehle bol raha tha wahi karo")
    )
    assert ok is True
    assert len(sent) == 1
    body = sent[0][2].lower()
    assert "approval" in body and "yes" in body  # re-points at the pending plan, says what to do


def test_resurface_skips_when_no_pending_approval(monkeypatch, sent):
    monkeypatch.setattr(runner, "_open_approval_exists_step", lambda t: False)
    ok = runner._maybe_resurface_pending_approval("t1", _event("wahin se karo"))
    assert ok is False
    assert sent == []


def test_resurface_skips_non_resume_reply(monkeypatch, sent):
    # a genuinely new topic while an approval is pending must keep its normal path (answer it),
    # not get bounced with the re-surface line.
    monkeypatch.setattr(runner, "_open_approval_exists_step", lambda t: True)
    ok = runner._maybe_resurface_pending_approval("t1", _event("what's my top product"))
    assert ok is False
    assert sent == []


def test_resurface_skips_explicit_send(monkeypatch, sent):
    # an explicit send verb is an APPROVAL (resolved by try_resume_pending_approval), never a
    # re-surface — is_resume_cue excludes it.
    monkeypatch.setattr(runner, "_open_approval_exists_step", lambda t: True)
    ok = runner._maybe_resurface_pending_approval("t1", _event("chalo bhej do"))
    assert ok is False


def test_resurface_skips_optout(monkeypatch, sent):
    # opt-out/DSR always wins — never re-surface an approval over a STOP.
    monkeypatch.setattr(runner, "_open_approval_exists_step", lambda t: True)
    ok = runner._maybe_resurface_pending_approval("t1", _event("STOP"))
    assert ok is False
    assert sent == []


def test_resurface_no_recipient(monkeypatch, sent):
    monkeypatch.setattr(runner, "_open_approval_exists_step", lambda t: True)
    ok = runner._maybe_resurface_pending_approval("t1", _event("resume", phone=None))
    assert ok is False
    assert sent == []


def test_resurface_hi_locale(monkeypatch, sent):
    import orchestrator.owner_surface.freeform_acks as fa

    monkeypatch.setattr(fa, "resolve_owner_locale", lambda t: "hi")
    monkeypatch.setattr(runner, "_open_approval_exists_step", lambda t: True)
    ok = runner._maybe_resurface_pending_approval("t1", _event("carry on with what you were saying"))
    assert ok is True
    assert sent[0][2] == runner._RESURFACE_PENDING_APPROVAL["hi"]


def test_resurface_failsoft_on_send_error(monkeypatch, sent):
    import orchestrator.owner_surface.freeform_acks as fa

    def _boom(*a, **k):
        raise RuntimeError("send down")

    monkeypatch.setattr(fa, "send_freeform_ack", _boom)
    monkeypatch.setattr(runner, "_open_approval_exists_step", lambda t: True)
    # a send failure must fail SOFT (return False) so the normal pipeline still runs — never raise.
    ok = runner._maybe_resurface_pending_approval("t1", _event("resume"))
    assert ok is False
