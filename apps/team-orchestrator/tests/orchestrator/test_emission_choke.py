"""VT-718 S2 — the single OWNER emission choke (CL-2026-07-28-single-voice-manager).

Truth table for the suppression rule (same normalized body, same recipient, within window,
NO owner inbound between), the shadow/enforce/off modes, the failed-send-never-poisons-the-ring
invariant (fallback ladders must stay free to resend what never went out), the L2
conversation_log rule, and the §0.1.1 boundary: the choke can only SUPPRESS an owner send —
it never touches customer sends and never relaxes the customer effect gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("twilio")
pytest.importorskip("dbos")

from orchestrator.utils import twilio_send  # noqa: E402
from orchestrator.utils.twilio_send import (  # noqa: E402
    CHOKE_SUPPRESSED_SID,
    UngatedCustomerSendError,
    _emission_normalize,
    _l1_dupe,
    _l2_dupe,
    _note_owner_emission,
    _owner_emission_guard,
    customer_send_context,
    note_owner_inbound,
    send_freeform_message,
)

_PHONE = "+919812300007"  # CL-422 synthetic; never a real owner.
_TOKEN = "tok-test-recipient"
_TENANT = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def _clean_choke(monkeypatch):
    """Fresh ring per test + a deterministic salt for note_owner_inbound's hashing."""
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt718-test-salt")
    with twilio_send._emission_lock:
        twilio_send._emission_cache.clear()
    yield
    with twilio_send._emission_lock:
        twilio_send._emission_cache.clear()


def _mode(monkeypatch, mode: str) -> None:
    monkeypatch.setenv("TEAM_OWNER_EMISSION_CHOKE", mode)


# --- normalization -------------------------------------------------------------------------


def test_normalize_collapses_case_space_punct():
    a = _emission_normalize("Got it — already noted!  What's your  website?")
    b = _emission_normalize("got it already noted whats your website")
    assert a == b


def test_normalize_empty_is_empty():
    assert _emission_normalize("   ") == ""


# --- L1 truth table ------------------------------------------------------------------------


def test_l1_dupe_within_window_no_inbound(monkeypatch):
    _mode(monkeypatch, "enforce")
    _note_owner_emission(_TOKEN, "hello owner")
    import time as _t

    assert _l1_dupe(_TOKEN, _emission_normalize("hello owner"), _t.monotonic()) is True


def test_l1_not_dupe_after_inbound(monkeypatch):
    """An owner turn between the two sends makes the repeat LEGITIMATE (a re-ask)."""
    _mode(monkeypatch, "enforce")
    _note_owner_emission(_TOKEN, "what is your website?")
    # The inbound marker keys on hash_phone(phone); write it directly for the token under test.
    with twilio_send._emission_lock:
        import time as _t

        twilio_send._emission_entry(_TOKEN).last_inbound_ts = _t.monotonic()
    import time as _t

    assert _l1_dupe(_TOKEN, _emission_normalize("what is your website?"), _t.monotonic()) is False


def test_l1_window_expiry(monkeypatch):
    _mode(monkeypatch, "enforce")
    _note_owner_emission(_TOKEN, "old line")
    import time as _t

    later = _t.monotonic() + twilio_send._EMISSION_WINDOW_S + 1
    assert _l1_dupe(_TOKEN, _emission_normalize("old line"), later) is False


def test_failed_send_never_poisons_the_ring(monkeypatch):
    """CHECK records nothing — only _note_owner_emission (post-success) does. A failed
    interactive attempt must leave the freeform fallback of the SAME text sendable."""
    _mode(monkeypatch, "enforce")
    import time as _t

    norm = _emission_normalize("fallback ladder text")
    assert _l1_dupe(_TOKEN, norm, _t.monotonic()) is False  # checked (attempt failed after this)
    assert _l1_dupe(_TOKEN, norm, _t.monotonic()) is False  # fallback re-check: still clean


def test_off_mode_records_nothing(monkeypatch):
    _mode(monkeypatch, "off")
    _note_owner_emission(_TOKEN, "hello")
    assert len(twilio_send._emission_cache) == 0


# --- note_owner_inbound --------------------------------------------------------------------


def test_note_owner_inbound_marks_by_phone_hash(monkeypatch):
    _mode(monkeypatch, "enforce")
    from orchestrator.utils.phone_token import hash_phone

    note_owner_inbound(_PHONE)
    token = hash_phone(_PHONE)
    assert twilio_send._emission_cache[token].last_inbound_ts > 0


def test_note_owner_inbound_never_raises():
    note_owner_inbound("")  # empty → no-op, no raise


# --- L2 (conversation_log) rule ------------------------------------------------------------


def _turns(*rows):
    return [{"role": r, "text": t, "created_at": at, "surface": "journey"} for r, t, at in rows]


def test_l2_dupe_assistant_match_no_owner_after(monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setattr(
        "orchestrator.conversation_log.active_window",
        lambda *a, **k: _turns(("assistant", "Your GST looks right?", now - timedelta(seconds=30))),
    )
    assert _l2_dupe(_TENANT, _emission_normalize("Your GST looks right?")) is True


def test_l2_owner_turn_after_match_clears_it(monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setattr(
        "orchestrator.conversation_log.active_window",
        lambda *a, **k: _turns(
            ("assistant", "Your GST looks right?", now - timedelta(seconds=60)),
            ("owner", "which gst?", now - timedelta(seconds=20)),
        ),
    )
    assert _l2_dupe(_TENANT, _emission_normalize("Your GST looks right?")) is False


def test_l2_naive_datetime_handled(monkeypatch):
    naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=10)
    monkeypatch.setattr(
        "orchestrator.conversation_log.active_window",
        lambda *a, **k: _turns(("assistant", "hello", naive)),
    )
    assert _l2_dupe(_TENANT, _emission_normalize("hello")) is True


def test_l2_fails_open_on_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("orchestrator.conversation_log.active_window", _boom)
    assert _l2_dupe(_TENANT, _emission_normalize("hello")) is False


# --- guard modes ---------------------------------------------------------------------------


def test_guard_off_is_inert(monkeypatch):
    _mode(monkeypatch, "off")
    _note_owner_emission(_TOKEN, "hello")  # off → not recorded anyway
    assert _owner_emission_guard(_TOKEN, "hello", tenant_id=None, surface="t") is False


def test_guard_shadow_logs_but_never_suppresses(monkeypatch, caplog):
    _mode(monkeypatch, "shadow")
    _note_owner_emission(_TOKEN, "hello owner")
    import logging

    with caplog.at_level(logging.WARNING):
        assert _owner_emission_guard(_TOKEN, "hello owner", tenant_id=None, surface="t") is False
    assert any("emission-choke SHADOW" in r.message for r in caplog.records)


def test_guard_enforce_suppresses_dup(monkeypatch):
    _mode(monkeypatch, "enforce")
    _note_owner_emission(_TOKEN, "hello owner")
    assert _owner_emission_guard(_TOKEN, "hello owner", tenant_id=None, surface="t") is True


def test_guard_enforce_allows_fresh_text(monkeypatch):
    _mode(monkeypatch, "enforce")
    _note_owner_emission(_TOKEN, "hello owner")
    assert _owner_emission_guard(_TOKEN, "different line", tenant_id=None, surface="t") is False


def test_guard_empty_body_never_suppressed(monkeypatch):
    _mode(monkeypatch, "enforce")
    assert _owner_emission_guard(_TOKEN, "", tenant_id=None, surface="t") is False


# --- transport integration -----------------------------------------------------------------


def test_freeform_enforce_returns_sentinel_without_twilio_call(monkeypatch, twilio_create):
    _mode(monkeypatch, "enforce")
    sid1 = send_freeform_message("same line", _PHONE)
    assert sid1 != CHOKE_SUPPRESSED_SID
    sid2 = send_freeform_message("same line", _PHONE)
    assert sid2 == CHOKE_SUPPRESSED_SID
    assert twilio_create.call_count == 1  # the duplicate never reached Twilio


def test_freeform_shadow_sends_both(monkeypatch, twilio_create):
    _mode(monkeypatch, "shadow")
    send_freeform_message("same line", _PHONE)
    send_freeform_message("same line", _PHONE)
    assert twilio_create.call_count == 2


def test_freeform_dup_after_inbound_sends(monkeypatch, twilio_create):
    _mode(monkeypatch, "enforce")
    send_freeform_message("what is your website?", _PHONE)
    note_owner_inbound(_PHONE)  # the owner spoke — a verbatim re-ask is legitimate
    sid = send_freeform_message("what is your website?", _PHONE)
    assert sid != CHOKE_SUPPRESSED_SID
    assert twilio_create.call_count == 2


def test_record_turn_false_skips_conversation_log(monkeypatch, twilio_create):
    _mode(monkeypatch, "off")
    calls = []
    monkeypatch.setattr(
        twilio_send, "_record_owner_conversation_turn", lambda *a, **k: calls.append(a)
    )
    send_freeform_message("hi", _PHONE, tenant_id=_TENANT, record_turn=False)
    assert calls == []
    send_freeform_message("hi again", _PHONE, tenant_id=_TENANT)
    assert len(calls) == 1


# --- §0.1.1: the choke governs the VOICE only — customer sends + effect gates untouched ----


def test_choke_never_touches_customer_sends(monkeypatch, twilio_create):
    """A customer-session send is NEVER deduped by the owner choke (identical bodies both go),
    and the customer effect gate still fail-closes outside its context regardless of choke mode."""
    _mode(monkeypatch, "enforce")
    with pytest.raises(UngatedCustomerSendError):
        send_freeform_message("promo", _PHONE, is_customer_session=True)
    with customer_send_context():
        s1 = send_freeform_message("promo", _PHONE, is_customer_session=True)
        s2 = send_freeform_message("promo", _PHONE, is_customer_session=True)
    assert CHOKE_SUPPRESSED_SID not in (s1, s2)
    assert twilio_create.call_count == 2


def test_choke_cannot_create_or_approve_a_send(monkeypatch, twilio_create):
    """Suppress-only: with the choke fully on, a NON-duplicate send behaves byte-identically —
    the guard has no path that adds, reorders, or approves an emission."""
    _mode(monkeypatch, "enforce")
    sid = send_freeform_message("fresh unique line", _PHONE)
    assert sid != CHOKE_SUPPRESSED_SID
    assert twilio_create.call_count == 1
