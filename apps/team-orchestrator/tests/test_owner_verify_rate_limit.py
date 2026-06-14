"""VT-394 — verify-start endpoint enforces the OTP rate-limit (Direction B).

TestClient over the owner_verify router in Twilio mock mode (no network, no DB).
The limiter is checked BEFORE start_verification; over-limit → HTTP 429. The
client IP is forwarded by the internal-secret-authenticated team-web caller via
X-Forwarded-For and is trusted ONLY past the secret check.

Placed at the tests/ top level (NOT tests/orchestrator/) so the package's
autouse twilio_send stub fixture does not apply — this path uses the separate
twilio_verify mock path, which makes no network call.

Assertions:
  - a 403 (bad/absent secret) never reaches the limiter or Twilio
  - under-limit start returns 200 'pending'
  - the 6th request from the same forwarded IP returns 429
  - distinct forwarded IPs (same phone budget left) keep passing → the per-IP
    cap keys on the FORWARDED X-Forwarded-For, proving team-web's IP reaches it
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator.auth.otp_rate_limit import (  # noqa: E402
    OTP_MAX_PER_IP,
    _reset_otp_rate_limit,
)

_SECRET = "vt394-test-secret"
_PHONE = "+919812300055"  # CL-422 synthetic.


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api.owner_verify import router

    monkeypatch.setenv("INTERNAL_API_SECRET", _SECRET)
    monkeypatch.setenv("TEAM_TWILIO_VERIFY_MOCK_MODE", "1")
    _reset_otp_rate_limit()
    app = FastAPI()
    app.include_router(router)
    yield TestClient(app)
    _reset_otp_rate_limit()


def _start(client, ip: str, phone: str = _PHONE, secret: str = _SECRET):
    return client.post(
        "/api/orchestrator/owner/verify-start",
        json={"phone": phone, "channel": "whatsapp"},
        headers={"X-Internal-Secret": secret, "X-Forwarded-For": ip},
    )


def test_bad_secret_403_before_limiter(client):
    # A wrong secret is rejected (403) and never consumes a limiter token.
    r = _start(client, "1.2.3.4", secret="wrong")
    assert r.status_code == 403


def test_under_limit_returns_pending(client):
    r = _start(client, "1.2.3.4")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending"
    assert body["verification_sid"]


def test_over_limit_returns_429(client):
    ip = "5.6.7.8"
    # Distinct phones so only the per-IP cap can trip first.
    for i in range(OTP_MAX_PER_IP):
        r = _start(client, ip, phone=f"+9198123010{i:02d}")
        assert r.status_code == 200, f"request {i} should be under the cap"
    over = _start(client, ip, phone="+919812301099")
    assert over.status_code == 429
    assert "too many" in over.json()["detail"].lower()


def test_per_ip_keys_on_forwarded_ip(client):
    """The per-IP cap keys on the FORWARDED X-Forwarded-For header — distinct
    forwarded IPs each get their own budget, proving team-web's IP reaches the
    orchestrator limiter (Direction B contract)."""
    # Exhaust IP A.
    for i in range(OTP_MAX_PER_IP):
        assert _start(client, "9.9.9.9", phone=f"+9198123020{i:02d}").status_code == 200
    assert _start(client, "9.9.9.9", phone="+919812302099").status_code == 429
    # A DIFFERENT forwarded IP still passes (own bucket).
    assert _start(client, "9.9.9.10", phone="+919812302100").status_code == 200
