"""VT-411 — ownership binding (DIN-KYC + ownership-OTP) unit tests (injected transports, no DB/live)."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

_REASON = "owner KYC ownership director verification"  # ≥20 chars


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from orchestrator.integrations.methods import sandbox_kyc

    monkeypatch.setenv("SANDBOX_API_KEY", "k")
    monkeypatch.setenv("SANDBOX_API_SECRET", "s")
    monkeypatch.setenv("TEAM_TWILIO_VERIFY_MOCK_MODE", "1")  # no live Twilio
    sandbox_kyc._token = None
    sandbox_kyc._token_exp = 0.0
    yield
    sandbox_kyc._token = None
    sandbox_kyc._token_exp = 0.0


def _director_recorder(companies):
    def request_fn(_method, path, _headers, _body):
        if path == "/authenticate":
            return {"access_token": "TOK"}
        return {"data": {"director_data": {"din": "01234567", "name": "A Director"}, "company_data": companies}}

    return request_fn


def test_verify_owner_via_din_true_when_din_directs_cin(monkeypatch):
    from orchestrator.onboarding import ownership

    flips = {"n": 0}
    monkeypatch.setattr("orchestrator.onboarding.mca_store.set_owner_channel_verified",
                        lambda _t: flips.update(n=flips["n"] + 1))
    req = _director_recorder([{"company_name": "RKECOM", "cin": "U52609MH2020OPC344309"}])
    ok = ownership.verify_owner_via_din("t1", "01234567", "U52609MH2020OPC344309", reason=_REASON, request_fn=req)
    assert ok is True and flips["n"] == 1  # KYC asserted → flag set once


def test_verify_owner_via_din_false_when_din_does_not_direct_cin(monkeypatch):
    from orchestrator.onboarding import ownership

    flips = {"n": 0}
    monkeypatch.setattr("orchestrator.onboarding.mca_store.set_owner_channel_verified",
                        lambda _t: flips.update(n=flips["n"] + 1))
    req = _director_recorder([{"company_name": "OTHER CO", "cin": "U99999XX9999XXX999999"}])
    ok = ownership.verify_owner_via_din("t1", "01234567", "U52609MH2020OPC344309", reason=_REASON, request_fn=req)
    assert ok is False and flips["n"] == 0  # no DIN↔CIN link → NOT verified (fail-closed)


def test_verify_owner_via_din_fail_closed_on_blank_input(monkeypatch):
    from orchestrator.onboarding import ownership

    monkeypatch.setattr("orchestrator.onboarding.mca_store.set_owner_channel_verified",
                        lambda _t: pytest.fail("must not flip on blank input"))
    assert ownership.verify_owner_via_din("t1", "", "U52609MH2020OPC344309", reason=_REASON) is False
    assert ownership.verify_owner_via_din("t1", "01234567", "", reason=_REASON) is False


def test_start_ownership_otp_is_a_distinct_verification():
    from orchestrator.onboarding import ownership

    res = ownership.start_ownership_otp("t1", "+919321553267")  # mock mode → a pending sid
    assert res.status == "pending" and res.verification_sid
