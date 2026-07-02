"""VT-559 — the Twilio Verify OTP path must fail-closed through the dev send-guard
chokepoint (SAFETY-CRITICAL, rail bypass fix).

CONFIRMED BREACH: ``auth/twilio_verify.py`` built its own raw ``twilio.rest.Client``
and called ``verify.v2.services(sid).verifications.create(to=, channel=)`` directly —
this never passed through ``utils/twilio_send._client()``, so
``dev_send_guard.maybe_wrap_for_dev`` never wrapped it. A dev signup pointed at any
real number sent a real WhatsApp OTP. Fail-OPEN on dev.

The fix routes ``twilio_verify._client()`` through the SAME ``maybe_wrap_for_dev``
chokepoint ``twilio_send._client()`` uses. These tests drive the actual public
functions (``start_verification`` / ``check_verification``) with the guard installed
over a SPY inner client, asserting the real Twilio transport is never reached for a
non-allowlisted destination on dev, and that an allowlisted destination and prod
posture are unaffected. Companion to ``test_dev_send_guard_verify_unit.py`` (the
dep-less guard-logic proof) and mirrors ``test_dev_send_guard_transport.py``'s
pattern for the messages path.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("twilio")

from orchestrator.auth import twilio_verify  # noqa: E402
from orchestrator.utils.dev_send_guard import (  # noqa: E402
    DevSendGuardClient,
    maybe_wrap_for_dev,
)

# Fazal's real number — the number the VT-476 breach actually messaged. The guard
# MUST mock a dev OTP to it when it is not explicitly allowlisted.
FAZAL_NUMBER = "+919321553267"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test owns EXPECTED_ENV + DEV_SEND_ALLOWLIST explicitly; start unset.
    Real-Verify path (not TEAM_TWILIO_VERIFY_MOCK_MODE) — the guard is what must
    stop the real call, not the separate full-mock flag."""
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    monkeypatch.delenv("DEV_SEND_ALLOWLIST", raising=False)
    monkeypatch.delenv("TEAM_TWILIO_VERIFY_MOCK_MODE", raising=False)
    monkeypatch.setenv("TWILIO_VERIFY_SERVICE_SID", "VAtest00000000000000000000000000")


@pytest.fixture
def spy_inner():
    """A spy standing in for the REAL Twilio client's Verify surface.

    ``inner.verify.v2.services(sid).verifications.create`` returns a real-shaped
    VerificationInstance (a genuine-looking ``VE…`` sid) and records every call, so
    a test can assert whether the REAL transport was hit.
    """
    inner = MagicMock()
    inner.verify.v2.services.return_value.verifications.create = MagicMock(
        return_value=SimpleNamespace(sid="VE" + "1" * 32, status="pending")
    )
    return inner


# --------------------------------------------------------------------------- #
# Static proof the fix lives at _client(): the actual OTP-send bypass this row
# closes.
# --------------------------------------------------------------------------- #


def test_verify_client_installs_guard_at_source():
    source = inspect.getsource(twilio_verify)
    start = source.index("def _client(")
    nxt = source.index("\ndef ", start + 1)
    assert "maybe_wrap_for_dev" in source[start:nxt], (
        "twilio_verify._client() must wrap its client via maybe_wrap_for_dev — "
        "otherwise the OTP send path can reach real Twilio on dev (VT-559)"
    )


# --------------------------------------------------------------------------- #
# (a) dev + non-allowlisted number -> OTP create mocked, no real client call.
# --------------------------------------------------------------------------- #


def test_dev_empty_allowlist_mocks_otp_no_real_call(spy_inner, monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.delenv("DEV_SEND_ALLOWLIST", raising=False)
    monkeypatch.setattr(twilio_verify, "_client", lambda: DevSendGuardClient(spy_inner))

    result = twilio_verify.start_verification(FAZAL_NUMBER, "whatsapp", tenant_id="t1")

    # CORE SAFETY ASSERTION: the REAL Twilio Verify transport was NEVER invoked.
    spy_inner.verify.v2.services.return_value.verifications.create.assert_not_called()
    assert result.status == "pending"
    assert result.verification_sid.startswith("VEDEV")


# --------------------------------------------------------------------------- #
# (b) dev + allowlisted number -> real path reached.
# --------------------------------------------------------------------------- #


def test_dev_allowlisted_number_reaches_real_transport(spy_inner, monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv("DEV_SEND_ALLOWLIST", FAZAL_NUMBER)
    monkeypatch.setattr(twilio_verify, "_client", lambda: DevSendGuardClient(spy_inner))

    result = twilio_verify.start_verification(FAZAL_NUMBER, "whatsapp", tenant_id="t1")

    spy_inner.verify.v2.services.return_value.verifications.create.assert_called_once()
    assert result.verification_sid.startswith("VE1")  # the real spy's SID, not a mock


# --------------------------------------------------------------------------- #
# (c) prod posture unchanged -> guard inert, real call reached regardless of
# the allowlist.
# --------------------------------------------------------------------------- #


def test_prod_guard_inert_real_call_reached(spy_inner, monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    monkeypatch.delenv("DEV_SEND_ALLOWLIST", raising=False)
    monkeypatch.setattr(twilio_verify, "_client", lambda: maybe_wrap_for_dev(spy_inner))

    result = twilio_verify.start_verification(FAZAL_NUMBER, "whatsapp", tenant_id="t1")

    spy_inner.verify.v2.services.return_value.verifications.create.assert_called_once()
    assert result.verification_sid.startswith("VE1")


# --------------------------------------------------------------------------- #
# Regression: check_verification's .verification_checks chain is NOT guarded
# (validates a code, sends nothing) but must still reach the real client through
# the wrapped .verify proxy without breaking.
# --------------------------------------------------------------------------- #


def test_check_verification_passthrough_unaffected(spy_inner, monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.delenv("DEV_SEND_ALLOWLIST", raising=False)
    spy_inner.verify.v2.services.return_value.verification_checks.create = MagicMock(
        return_value=SimpleNamespace(sid="VC" + "2" * 32, status="approved")
    )
    monkeypatch.setattr(twilio_verify, "_client", lambda: DevSendGuardClient(spy_inner))

    result = twilio_verify.check_verification(FAZAL_NUMBER, "123456", tenant_id="t1")

    spy_inner.verify.v2.services.return_value.verification_checks.create.assert_called_once()
    assert result.approved is True
    assert result.status == "approved"
