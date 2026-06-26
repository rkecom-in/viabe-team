"""VT-411 — ownership route tests (wires the dormant ownership fns onto the critical path).

Mounts ONLY the ownership router (no DB; the ownership functions are monkeypatched). Asserts:
  1. each route 403s without the internal secret;
  2. with the secret, each route INVOKES its ownership function and returns the mapped JSON;
  3. the din route 422s on a <20-char reason.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from orchestrator.auth.twilio_verify import VerifyStartResult  # noqa: E402

_SECRET = "vt411-test-secret"
_HDR = {"X-Internal-Secret": _SECRET}
_GOOD_REASON = "regulatory KYC ownership verification at onboarding"  # >= 20 chars


@pytest.fixture
def client(monkeypatch):
    from orchestrator.api.ownership import router

    monkeypatch.setenv("INTERNAL_API_SECRET", _SECRET)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ---- 1. internal-secret gate (403 without it) ----------------------------------------------------


def test_otp_start_requires_internal_secret(client):
    body = {"tenant_id": "t1", "public_phone": "+919321553267"}
    assert client.post("/api/orchestrator/onboard/ownership/otp/start", json=body).status_code == 403
    assert (
        client.post(
            "/api/orchestrator/onboard/ownership/otp/start",
            json=body,
            headers={"X-Internal-Secret": "wrong"},
        ).status_code
        == 403
    )


def test_otp_confirm_requires_internal_secret(client):
    body = {"tenant_id": "t1", "public_phone": "+919321553267", "code": "123456"}
    assert (
        client.post("/api/orchestrator/onboard/ownership/otp/confirm", json=body).status_code == 403
    )


def test_din_requires_internal_secret(client):
    body = {"tenant_id": "t1", "din": "00000001", "cin": "U12345MH2020PTC000001", "reason": _GOOD_REASON}
    assert client.post("/api/orchestrator/onboard/ownership/din", json=body).status_code == 403


# ---- 2. with the secret → invokes the ownership fn + returns the mapped JSON ----------------------


def test_otp_start_invokes_and_maps(client, monkeypatch):
    calls = {}

    def fake_start(tenant_id, public_phone, **kw):
        calls["args"] = (tenant_id, public_phone)
        return VerifyStartResult(verification_sid="VEtest123", status="pending", channel="whatsapp")

    monkeypatch.setattr("orchestrator.onboarding.ownership.start_ownership_otp", fake_start)

    r = client.post(
        "/api/orchestrator/onboard/ownership/otp/start",
        json={"tenant_id": "t1", "public_phone": "+919321553267"},
        headers=_HDR,
    )
    assert r.status_code == 200
    assert r.json() == {"verification_sid": "VEtest123", "status": "pending"}
    assert calls["args"] == ("t1", "+919321553267")


def test_otp_confirm_invokes_and_maps_true(client, monkeypatch):
    calls = {}

    def fake_confirm(tenant_id, public_phone, code, **kw):
        calls["args"] = (tenant_id, public_phone, code)
        return True

    monkeypatch.setattr("orchestrator.onboarding.ownership.confirm_ownership_otp", fake_confirm)

    r = client.post(
        "/api/orchestrator/onboard/ownership/otp/confirm",
        json={"tenant_id": "t1", "public_phone": "+919321553267", "code": "123456"},
        headers=_HDR,
    )
    assert r.status_code == 200
    assert r.json() == {"owner_channel_verified": True}
    assert calls["args"] == ("t1", "+919321553267", "123456")


def test_otp_confirm_maps_false(client, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.onboarding.ownership.confirm_ownership_otp", lambda *a, **k: False
    )
    r = client.post(
        "/api/orchestrator/onboard/ownership/otp/confirm",
        json={"tenant_id": "t1", "public_phone": "+919321553267", "code": "000000"},
        headers=_HDR,
    )
    assert r.status_code == 200
    assert r.json() == {"owner_channel_verified": False}


def test_din_invokes_and_maps(client, monkeypatch):
    calls = {}

    def fake_din(tenant_id, din, cin, *, reason, request_fn=None):
        calls["args"] = (tenant_id, din, cin, reason)
        return True

    monkeypatch.setattr("orchestrator.onboarding.ownership.verify_owner_via_din", fake_din)

    r = client.post(
        "/api/orchestrator/onboard/ownership/din",
        json={
            "tenant_id": "t1",
            "din": "00000001",
            "cin": "U12345MH2020PTC000001",
            "reason": _GOOD_REASON,
        },
        headers=_HDR,
    )
    assert r.status_code == 200
    assert r.json() == {"owner_channel_verified": True}
    assert calls["args"] == ("t1", "00000001", "U12345MH2020PTC000001", _GOOD_REASON)


# ---- 3. din 422s on a <20-char reason ------------------------------------------------------------


def test_din_short_reason_422(client, monkeypatch):
    invoked = {"called": False}

    def fake_din(*a, **k):
        invoked["called"] = True
        return True

    monkeypatch.setattr("orchestrator.onboarding.ownership.verify_owner_via_din", fake_din)

    r = client.post(
        "/api/orchestrator/onboard/ownership/din",
        json={"tenant_id": "t1", "din": "00000001", "cin": "U1", "reason": "too short"},
        headers=_HDR,
    )
    assert r.status_code == 422
    assert not invoked["called"]  # guard fires before the ownership fn


def test_din_missing_reason_422(client):
    # pydantic-level required-field rejection (422) — reason absent entirely
    r = client.post(
        "/api/orchestrator/onboard/ownership/din",
        json={"tenant_id": "t1", "din": "00000001", "cin": "U1"},
        headers=_HDR,
    )
    assert r.status_code == 422
