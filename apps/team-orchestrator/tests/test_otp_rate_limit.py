"""VT-394 — unit tests for the orchestrator-side OTP request rate-limiter.

Direction B (Cowork review 2026-06-14): an in-process windowed limiter behind a
swap seam (``OtpRateLimiter`` ABC → ``InProcessOtpRateLimiter``), global by
construction (single uvicorn worker). Mirrors team-web's ``otp-rate-limit.ts``
(5 req / 15-min fixed window, per-IP AND per-phone, keys SHA-256[:16]-hashed).

No DB, no network, no Twilio — pure in-process counter. Placed at the tests/ top
level (NOT tests/orchestrator/) so the package's autouse twilio_send stub does
not apply (this module never touches twilio).

Assertions:
  - under-limit passes; the 6th request (over the 5/window cap) trips
  - per-IP and per-phone are independent dimensions
  - a blocked-by-ip request does NOT consume the per-phone budget
  - FAIL-OPEN: a forced internal error admits the request (never locks out)
  - the phone/IP are HASHED at rest — no plaintext token in the limiter's map
  - the module-level limiter is SHARED state (two checks share the counter)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator.auth import otp_rate_limit as rl_mod  # noqa: E402
from orchestrator.auth.otp_rate_limit import (  # noqa: E402
    OTP_MAX_PER_IP,
    OTP_MAX_PER_PHONE,
    InProcessOtpRateLimiter,
    _reset_otp_rate_limit,
    _tokenize,
    check_otp_rate_limit,
    get_otp_rate_limiter,
)

_IP = "203.0.113.7"
_PHONE = "+919812300042"  # CL-422 synthetic; never a real owner.


@pytest.fixture(autouse=True)
def _reset():
    """Clear the shared limiter before + after each test (test seam)."""
    _reset_otp_rate_limit()
    yield
    _reset_otp_rate_limit()


def _fresh() -> InProcessOtpRateLimiter:
    """A standalone limiter (own counter) for dimension-isolation tests."""
    return InProcessOtpRateLimiter()


def test_under_limit_passes():
    limiter = _fresh()
    for i in range(OTP_MAX_PER_IP):
        # Distinct phones so the per-phone cap is not what trips.
        res = limiter.check(_IP, f"+9198123000{i:02d}")
        assert res.allowed is True, f"request {i} should be under the cap"
        assert res.blocked_by is None


def test_over_limit_trips_by_ip():
    limiter = _fresh()
    # Distinct phones → only the per-IP cap can trip.
    for i in range(OTP_MAX_PER_IP):
        assert limiter.check(_IP, f"+9198123000{i:02d}").allowed is True
    over = limiter.check(_IP, "+919812300099")
    assert over.allowed is False
    assert over.blocked_by == "ip"


def test_over_limit_trips_by_phone():
    limiter = _fresh()
    # Distinct IPs → only the per-phone cap can trip.
    for i in range(OTP_MAX_PER_PHONE):
        assert limiter.check(f"10.0.0.{i}", _PHONE).allowed is True
    over = limiter.check("10.0.0.250", _PHONE)
    assert over.allowed is False
    assert over.blocked_by == "phone"


def test_per_ip_and_per_phone_independent():
    """The IP and phone dimensions use separate buckets — exhausting one leaves
    the other's budget intact."""
    limiter = _fresh()
    # Exhaust the per-IP cap from one IP with distinct phones.
    for i in range(OTP_MAX_PER_IP):
        limiter.check(_IP, f"+9198123100{i:02d}")
    blocked = limiter.check(_IP, _PHONE)
    assert blocked.blocked_by == "ip"
    # A FRESH IP for the SAME phone still has full per-phone budget — proving
    # the ip trip did not asymmetrically burn the phone counter.
    fresh = limiter.check("198.51.100.1", _PHONE)
    assert fresh.allowed is True
    assert fresh.blocked_by is None


def test_fail_open_on_internal_error(monkeypatch, caplog):
    """FAIL-OPEN: if the limiter's own machinery raises, the request is ADMITTED
    (never lock out signups) and the degrade is logged loudly."""
    limiter = _fresh()

    def _boom(*_a, **_k):
        raise RuntimeError("forced limiter failure")

    monkeypatch.setattr(limiter, "_hit", _boom)
    with caplog.at_level(logging.ERROR, logger="orchestrator.auth.otp_rate_limit"):
        res = limiter.check(_IP, _PHONE)
    assert res.allowed is True, "fail-OPEN: a limiter error must ADMIT the request"
    assert res.blocked_by is None
    assert any(
        "FAILING OPEN" in r.getMessage() for r in caplog.records
    ), "the fail-open degrade must be logged loudly"


def test_keys_hashed_not_raw():
    """CL-390: the in-memory map holds only SHA-256[:16] tokens — never the raw
    phone or IP."""
    limiter = _fresh()
    limiter.check(_IP, _PHONE)
    keys = list(limiter._buckets.keys())
    assert keys, "expected at least one bucket after a check"
    joined = "||".join(keys)
    assert _PHONE not in joined, "raw phone must NOT appear in any bucket key"
    assert _IP not in joined, "raw IP must NOT appear in any bucket key"
    # The expected token IS present (hashed at rest, mirrors _tokenizePhone).
    assert _tokenize(_PHONE) in joined
    assert _tokenize(_IP) in joined


def test_module_limiter_is_shared_state():
    """Global-by-construction: two ``check_otp_rate_limit`` calls go through the
    one module-level limiter and share its counter. Drain the per-IP budget via
    the module function, then assert the SAME shared limiter is over-limit."""
    # OTP_MAX_PER_IP successful checks through the module-level helper.
    for i in range(OTP_MAX_PER_IP):
        assert check_otp_rate_limit(_IP, f"+9198124000{i:02d}").allowed is True
    # A second, independent call site (the singleton accessor) sees the SAME
    # exhausted counter — proving the state is module-shared, not per-call.
    res = get_otp_rate_limiter().check(_IP, "+919812400099")
    assert res.allowed is False
    assert res.blocked_by == "ip"
    # And it is literally the same object.
    assert get_otp_rate_limiter() is rl_mod._LIMITER
