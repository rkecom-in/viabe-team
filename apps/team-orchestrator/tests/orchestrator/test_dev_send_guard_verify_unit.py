"""VT-559 — dep-less unit tests for the dev send-guard's Verify-OTP wrapper logic.

Extends the VT-476 ``dev_send_guard`` module coverage: the guard also wraps
``client.verify.v2.services(sid).verifications.create`` — the Twilio Verify OTP
dispatch used by ``auth/twilio_verify.py``. That module built its own raw
``twilio.rest.Client`` and called Verify directly, never funneling through this
wrapper — a second rail-bypass alongside the messages one VT-476 already covers.

These exercise ``orchestrator.utils.dev_send_guard`` in ISOLATION — no DB, no DBOS,
no twilio (the module is stdlib-only) — so they run in the lightweight CI ``test``
job + the pre-push dep-less smoke, where twilio/dbos are NOT installed. The live
wiring proof (``twilio_verify._client()`` actually calls ``maybe_wrap_for_dev`` and
the public ``start_verification``/``check_verification`` functions funnel through
it) is asserted by ``test_twilio_verify_dev_guard.py`` (the twilio-requiring
funnel proof).

Core breach-stopping property under test: on dev, a Verify OTP to a non-allowlisted
real number is MOCKED — the wrapped (real) client's ``verifications.create`` is
NEVER called. ``.verification_checks.create`` (validates a code; never sends
anything) is left unguarded and must still be reachable through the wrapper.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.utils.dev_send_guard import DevSendGuardClient, maybe_wrap_for_dev

# Fazal's real number — the number the VT-476 breach actually messaged.
FAZAL_NUMBER = "+919321553267"


class _RecordingVerifications:
    """Fake inner ``.verifications`` that RECORDS every real create(). A recorded
    call here means the guard FAILED to block a real OTP dispatch — the breach."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(sid="VEREAL" + "0" * 26, status="pending")


class _RecordingVerificationChecks:
    """Fake inner ``.verification_checks`` — NEVER guarded (checking a code sends
    nothing to the destination), so every call here should be recorded regardless
    of the allowlist."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(sid="VC" + "0" * 30, status="approved")


class _RecordingServiceContext:
    def __init__(self) -> None:
        self.verifications = _RecordingVerifications()
        self.verification_checks = _RecordingVerificationChecks()


class _RecordingVerifyV2:
    def __init__(self) -> None:
        self._contexts: dict[str, _RecordingServiceContext] = {}

    def services(self, sid: str) -> _RecordingServiceContext:
        # Real Twilio returns a stable context per service sid; a single fake
        # context per sid is enough for the recording assertions below.
        return self._contexts.setdefault(sid, _RecordingServiceContext())


class _RecordingVerify:
    def __init__(self) -> None:
        self.v2 = _RecordingVerifyV2()


class _RecordingVerifyClient:
    def __init__(self) -> None:
        self.verify = _RecordingVerify()


_SERVICE_SID = "VAtest00000000000000000000000000"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    monkeypatch.delenv("DEV_SEND_ALLOWLIST", raising=False)


# --- THE breach-stopping property: non-allowlisted dev OTP NEVER hits real Twilio ---


def test_dev_empty_allowlist_mocks_verify_otp(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    inner = _RecordingVerifyClient()
    guarded = maybe_wrap_for_dev(inner)
    assert isinstance(guarded, DevSendGuardClient)

    result = guarded.verify.v2.services(_SERVICE_SID).verifications.create(
        to=FAZAL_NUMBER, channel="whatsapp"
    )

    service_ctx = inner.verify.v2.services(_SERVICE_SID)
    assert service_ctx.verifications.calls == [], (
        "BREACH: a real Verify OTP escaped the dev guard"
    )
    assert result.sid.startswith("VEDEV")
    assert result.status == "pending"


def test_dev_non_allowlisted_mocked_when_other_number_allowed(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv("DEV_SEND_ALLOWLIST", "+919999900000")  # a different number
    inner = _RecordingVerifyClient()
    guarded = maybe_wrap_for_dev(inner)
    guarded.verify.v2.services(_SERVICE_SID).verifications.create(
        to=f"whatsapp:{FAZAL_NUMBER}", channel="whatsapp"
    )
    service_ctx = inner.verify.v2.services(_SERVICE_SID)
    assert service_ctx.verifications.calls == [], (
        "non-allowlisted dev Verify OTP must be mocked"
    )


# --- the allowlist DOES let an explicit number through (so Fazal can test) ---


def test_dev_allowlisted_verify_otp_reaches_real_transport(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv("DEV_SEND_ALLOWLIST", FAZAL_NUMBER)
    inner = _RecordingVerifyClient()
    guarded = maybe_wrap_for_dev(inner)

    result = guarded.verify.v2.services(_SERVICE_SID).verifications.create(
        to=f"whatsapp:{FAZAL_NUMBER}", channel="whatsapp"
    )

    service_ctx = inner.verify.v2.services(_SERVICE_SID)
    assert len(service_ctx.verifications.calls) == 1, (
        "allowlisted dev Verify OTP must reach real Twilio"
    )
    assert result.sid.startswith("VEREAL")


# --- .verification_checks is NEVER guarded — it validates a code, sends nothing ---


def test_verification_checks_always_passes_through_unguarded(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.delenv("DEV_SEND_ALLOWLIST", raising=False)  # empty → OTP would mock
    inner = _RecordingVerifyClient()
    guarded = maybe_wrap_for_dev(inner)

    result = guarded.verify.v2.services(_SERVICE_SID).verification_checks.create(
        to=FAZAL_NUMBER, code="123456"
    )

    service_ctx = inner.verify.v2.services(_SERVICE_SID)
    assert len(service_ctx.verification_checks.calls) == 1, (
        "verification_checks.create must reach the real client unguarded — it "
        "never dispatches anything to the destination"
    )
    assert result.status == "approved"


# --- prod is UNAFFECTED: guard inert, real Verify calls as today ---


def test_prod_verify_guard_inert(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    inner = _RecordingVerifyClient()
    guarded = maybe_wrap_for_dev(inner)
    assert guarded is inner, "prod must get the unwrapped real client (guard inert)"

    guarded.verify.v2.services(_SERVICE_SID).verifications.create(
        to=FAZAL_NUMBER, channel="whatsapp"
    )  # allowlist ignored — empty allowlist would mock on dev
    service_ctx = inner.verify.v2.services(_SERVICE_SID)
    assert len(service_ctx.verifications.calls) == 1
