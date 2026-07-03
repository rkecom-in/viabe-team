"""VT-583 (CL-2026-07-03) — the runner-side consent-converse (C) + completed-no-send fallback (D1)
detection logic, against a real Postgres (RLS-respecting conversation_log via record_turn).

Both features hinge on lifetime-log reads: a consent GRANT only fires when a consent ASK was the last
thing we sent AND the reply is an unambiguous affirm (deterministic, zero-LLM); the D1 fallback only
fires when a COMPLETED brain run produced no assistant turn at/after the owner's inbound. These tests
seed conversation_log through the real record_turn path and assert the detectors directly.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-583 consent/no-send substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str, *, lang: str | None = None) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, business_type, "
            "whatsapp_number, preferred_language) "
            "VALUES ('vt583 consent/nosend', 'founding', 'trial', now(), 'services', %s, %s) RETURNING id",
            (f"+9199{uuid4().int % 10**8:08d}", lang),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _event(body: str, sid: str, *, phone: str = "+919000009999", kind: str = "inbound_message"):
    return SimpleNamespace(
        message_type=kind, body=body, twilio_message_sid=sid, sender_phone=phone
    )


# --- C: consent positive-intent gate (marker + deterministic affirm) ------------------------------


# The consent ask is recorded by consent_required_handler as a 'system'-surface turn whose text carries
# the enable phrase (the content marker the gate keys off).
_CONSENT_ASK_TEXT = "Your AI team is ready. Reply ACTIVATE TEAM to enable data inputs. Reply STOP to pause."


def test_consent_affirm_after_ask_grants(substrate):
    from orchestrator.conversation_log import record_turn
    from orchestrator.runner import _consent_affirm_after_ask

    tenant = _new_tenant(substrate.dsn)
    record_turn(tenant, "assistant", _CONSENT_ASK_TEXT, surface="system")
    assert _consent_affirm_after_ask(str(tenant), "haan chalu karo") is True


def test_consent_affirm_without_prior_ask_does_not_grant(substrate):
    """An affirm with NO consent ask as the last thing we sent must NOT grant (never enable on a bare
    'yes' the owner might have meant for something else)."""
    from orchestrator.conversation_log import record_turn
    from orchestrator.runner import _consent_affirm_after_ask

    tenant = _new_tenant(substrate.dsn)
    record_turn(tenant, "assistant", "some other manager reply", surface="manager")
    assert _consent_affirm_after_ask(str(tenant), "yes") is False


def test_consent_decline_after_ask_does_not_grant(substrate):
    from orchestrator.conversation_log import record_turn
    from orchestrator.runner import _consent_affirm_after_ask

    tenant = _new_tenant(substrate.dsn)
    record_turn(tenant, "assistant", _CONSENT_ASK_TEXT, surface="system")
    assert _consent_affirm_after_ask(str(tenant), "no thanks not now") is False


def test_consent_ambiguous_after_ask_does_not_grant(substrate):
    from orchestrator.conversation_log import record_turn
    from orchestrator.runner import _consent_affirm_after_ask

    tenant = _new_tenant(substrate.dsn)
    record_turn(tenant, "assistant", _CONSENT_ASK_TEXT, surface="system")
    assert _consent_affirm_after_ask(str(tenant), "what will it do with my data?") is False


def test_consent_marker_tracks_most_recent(substrate):
    """The marker is the MOST-RECENT assistant turn: a later non-ask send supersedes an earlier consent
    ask, so a stale ask can't be resurrected by an unrelated affirm."""
    from orchestrator.conversation_log import record_turn
    from orchestrator.runner import _consent_affirm_after_ask, _last_assistant_turn_was_consent_ask

    tenant = _new_tenant(substrate.dsn)
    record_turn(tenant, "assistant", _CONSENT_ASK_TEXT, surface="system")
    assert _last_assistant_turn_was_consent_ask(str(tenant)) is True
    time.sleep(0.02)
    record_turn(tenant, "assistant", "here is your latest campaign summary", surface="manager")
    assert _last_assistant_turn_was_consent_ask(str(tenant)) is False
    # …so a plain affirm now does NOT grant (the last thing we sent was not the ask).
    assert _consent_affirm_after_ask(str(tenant), "yes") is False


# --- D1: completed-no-send detection --------------------------------------------------------------


def test_brain_reply_detected_when_assistant_turn_follows_inbound(substrate):
    from orchestrator.conversation_log import record_turn
    from orchestrator.runner import _brain_emitted_owner_reply

    tenant = _new_tenant(substrate.dsn)
    record_turn(tenant, "owner", "hi what's up", message_sid="SID-d1-a", surface="manager")
    time.sleep(0.02)
    record_turn(tenant, "assistant", "here's your answer", surface="manager")
    assert _brain_emitted_owner_reply(str(tenant), "SID-d1-a") is True


def test_no_reply_detected_when_nothing_follows_inbound(substrate):
    """A completed run that recorded the inbound but NO assistant turn → detected as no-send (fallback
    should fire)."""
    from orchestrator.conversation_log import record_turn
    from orchestrator.runner import _brain_emitted_owner_reply

    tenant = _new_tenant(substrate.dsn)
    record_turn(tenant, "owner", "please do the thing", message_sid="SID-d1-b", surface="manager")
    assert _brain_emitted_owner_reply(str(tenant), "SID-d1-b") is False


def test_stale_prior_reply_not_miscounted(substrate):
    """An assistant turn that PREDATES this inbound must not count as a reply to it (the created_at >=
    inbound guard) — otherwise a prior answer would suppress the fallback for a later silent run."""
    from orchestrator.conversation_log import record_turn
    from orchestrator.runner import _brain_emitted_owner_reply

    tenant = _new_tenant(substrate.dsn)
    record_turn(tenant, "assistant", "an old answer from before", surface="manager")
    time.sleep(0.02)
    record_turn(tenant, "owner", "new question now", message_sid="SID-d1-c", surface="manager")
    assert _brain_emitted_owner_reply(str(tenant), "SID-d1-c") is False


def test_d1_fallback_sends_localized_honest_line(substrate, monkeypatch):
    """_send_completed_no_reply_fallback picks the owner locale and sends the honest, substance-railed
    line through the in-session manager path (send_freeform_ack)."""
    from orchestrator.owner_surface import freeform_acks
    from orchestrator.runner import _send_completed_no_reply_fallback

    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        freeform_acks, "send_freeform_ack",
        lambda tid, recipient, body: captured.append((str(tid), body)) or True,
    )

    tenant = _new_tenant(substrate.dsn, lang="hi")
    _send_completed_no_reply_fallback(str(tenant), _event("do it", "SID-d1-fb"))
    assert captured, "the fallback must send a line"
    body = captured[-1][1]
    assert body and "अपडेट" in body  # the Hindi honest line for a hi-locale owner


def test_d1_fallback_no_recipient_is_noop(substrate, monkeypatch):
    from orchestrator.owner_surface import freeform_acks
    from orchestrator.runner import _send_completed_no_reply_fallback

    captured: list[str] = []
    monkeypatch.setattr(freeform_acks, "send_freeform_ack", lambda *a, **k: captured.append("x"))
    tenant = _new_tenant(substrate.dsn)
    _send_completed_no_reply_fallback(str(tenant), _event("hi", "SID-d1-none", phone=""))
    assert not captured, "no recipient → no send (never crashes)"
