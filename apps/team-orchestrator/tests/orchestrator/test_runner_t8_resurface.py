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


# --- R1: runner._maybe_reconfirm_send_push (send-push re-confirm net) ------------------------------
# An ambiguous SEND PUSH against an OPEN customer-send approval must be RE-CONFIRMED (only SPEAKS —
# never resolves/approves/sends), not fall through to the brain (which claimed a send / spawned a
# competing plan — the sr_consequential_bulk_send + sr_always_confirm_first_contact_floor breakers).
# Pure-logic: the open-approval read + owner send + locale are monkeypatched; the send goes through the
# replay-safe _send_owner_reply_step (@DBOS.step), monkeypatched here to capture.


@pytest.fixture
def reconfirm_sent(monkeypatch):
    captured: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        runner, "_send_owner_reply_step", lambda t, r, b: (captured.append((t, r, b)), True)[1]
    )
    import orchestrator.owner_surface.freeform_acks as fa

    monkeypatch.setattr(fa, "resolve_owner_locale", lambda t: "en")
    return captured


def test_reconfirm_fires_on_send_push_with_open_customer_send_approval(monkeypatch, reconfirm_sent):
    monkeypatch.setattr(runner, "_open_customer_send_approval_exists_step", lambda t: True)
    ok = runner._maybe_reconfirm_send_push(
        "t1", _event("jaldi karo yaar, sabko ek saath bhej do, wait mat karo")
    )
    assert ok is True
    assert len(reconfirm_sent) == 1
    body = reconfirm_sent[0][2].lower()
    # honest + money-safe: NOTHING sent yet, and it names the one explicit reply that resolves the send.
    assert "haven't sent" in body
    assert "haan bhej do" in body


def test_reconfirm_skips_when_no_open_customer_send_approval(monkeypatch, reconfirm_sent):
    monkeypatch.setattr(runner, "_open_customer_send_approval_exists_step", lambda t: False)
    ok = runner._maybe_reconfirm_send_push("t1", _event("sabko bhej do"))
    assert ok is False
    assert reconfirm_sent == []


def test_reconfirm_skips_non_send_push_reply(monkeypatch, reconfirm_sent):
    # a genuinely new topic while an approval is pending keeps its normal path (answer it), not a re-confirm.
    monkeypatch.setattr(runner, "_open_customer_send_approval_exists_step", lambda t: True)
    ok = runner._maybe_reconfirm_send_push("t1", _event("what's my top product"))
    assert ok is False
    assert reconfirm_sent == []


def test_reconfirm_skips_negated_send(monkeypatch, reconfirm_sent):
    # "mat bhejo ruk jao" is a REJECT (negation binds the send) — never a re-confirm.
    monkeypatch.setattr(runner, "_open_customer_send_approval_exists_step", lambda t: True)
    ok = runner._maybe_reconfirm_send_push("t1", _event("mat bhejo ruk jao"))
    assert ok is False
    assert reconfirm_sent == []


def test_reconfirm_skips_optout(monkeypatch, reconfirm_sent):
    # opt-out / DSR (incl. a CD6 global-stop) always wins — never re-confirm over a STOP.
    monkeypatch.setattr(runner, "_open_customer_send_approval_exists_step", lambda t: True)
    for body in ("STOP", "bas ab message mat bhejo"):
        assert runner._maybe_reconfirm_send_push("t1", _event(body)) is False, body
    assert reconfirm_sent == []


def test_reconfirm_weak_ack_only_gets_reconfirm(monkeypatch, reconfirm_sent):
    # accepted bounded cost: a bare "theek hai" while a customer-send approval is open gets a
    # money-safe re-confirm rather than a brain turn.
    monkeypatch.setattr(runner, "_open_customer_send_approval_exists_step", lambda t: True)
    ok = runner._maybe_reconfirm_send_push("t1", _event("theek hai"))
    assert ok is True
    assert len(reconfirm_sent) == 1


def test_reconfirm_hi_locale(monkeypatch, reconfirm_sent):
    import orchestrator.owner_surface.freeform_acks as fa

    monkeypatch.setattr(fa, "resolve_owner_locale", lambda t: "hi")
    monkeypatch.setattr(runner, "_open_customer_send_approval_exists_step", lambda t: True)
    ok = runner._maybe_reconfirm_send_push("t1", _event("sabko ek saath bhej do jaldi"))
    assert ok is True
    assert reconfirm_sent[0][2] == runner._RECONFIRM_SEND_PUSH["hi"]


def test_reconfirm_no_recipient(monkeypatch, reconfirm_sent):
    monkeypatch.setattr(runner, "_open_customer_send_approval_exists_step", lambda t: True)
    ok = runner._maybe_reconfirm_send_push("t1", _event("bhej do", phone=None))
    assert ok is False
    assert reconfirm_sent == []


def test_reconfirm_failsoft_on_send_error(monkeypatch, reconfirm_sent):
    def _boom(*a, **k):
        raise RuntimeError("send down")

    monkeypatch.setattr(runner, "_send_owner_reply_step", _boom)
    monkeypatch.setattr(runner, "_open_customer_send_approval_exists_step", lambda t: True)
    # a send failure must fail SOFT (return False) so the normal pipeline still runs — never raise.
    ok = runner._maybe_reconfirm_send_push("t1", _event("sabko bhej do"))
    assert ok is False
