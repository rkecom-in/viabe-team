"""VT-250 — unit tests for the Twilio Verify owner-OTP client.

Mock-mode only (TEAM_TWILIO_VERIFY_MOCK_MODE=1) — no live Twilio call, no
network, no DB. Placed at the tests/ top level (NOT tests/orchestrator/) so
the orchestrator package's autouse twilio_send stub fixture does not apply;
the mock path here never imports twilio.

Assertions:
  - start_verification → status 'pending', verification_sid present
  - check_verification(correct) → approved
  - check_verification(wrong) → denied
  - channel routing: whatsapp (live) OK; sms gated OFF → ChannelGatedError;
    sms with gate env → OK; unknown channel → InvalidChannelError
  - CL-390: NO phone / NO code in any emitted log record — only
    verification_sid + tenant_id (+ channel/status)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator.auth.twilio_verify import (  # noqa: E402
    ChannelGatedError,
    InvalidChannelError,
    check_verification,
    start_verification,
)

_SYNTHETIC_PHONE = "+919812300001"  # CL-422 synthetic; never a real owner.
_SYNTHETIC_TENANT = "11111111-1111-4111-8111-111111111111"
_CORRECT_OTP = "654321"
_WRONG_OTP = "000000"


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch):
    """Force mock mode + a deterministic mock OTP.

    VT-559: ``_client()`` is no longer ``@lru_cache``'d (a cached client would
    freeze a stale dev-guard wrap decision across an env change), so there is no
    cache to clear here anymore.
    """
    monkeypatch.setenv("TEAM_TWILIO_VERIFY_MOCK_MODE", "1")
    monkeypatch.setenv("VT250_MOCK_OTP", _CORRECT_OTP)
    monkeypatch.delenv("VT250_SMS_CHANNEL_ENABLED", raising=False)
    yield


def test_start_returns_pending():
    result = start_verification(
        _SYNTHETIC_PHONE, "whatsapp", tenant_id=_SYNTHETIC_TENANT
    )
    assert result.status == "pending"
    assert result.verification_sid
    assert result.verification_sid.startswith("VEmock")
    assert result.channel == "whatsapp"


def test_check_correct_code_approved():
    result = check_verification(
        _SYNTHETIC_PHONE, _CORRECT_OTP, tenant_id=_SYNTHETIC_TENANT
    )
    assert result.approved is True
    assert result.status == "approved"
    assert result.verification_sid


def test_check_wrong_code_denied():
    result = check_verification(
        _SYNTHETIC_PHONE, _WRONG_OTP, tenant_id=_SYNTHETIC_TENANT
    )
    assert result.approved is False
    assert result.status == "denied"


def test_channel_routing_whatsapp_live():
    # whatsapp is the live channel — always permitted (no gate).
    result = start_verification(_SYNTHETIC_PHONE, "whatsapp")
    assert result.channel == "whatsapp"
    assert result.status == "pending"


def test_channel_routing_sms_gated_off():
    # sms built but GATED OFF by default → ChannelGatedError.
    with pytest.raises(ChannelGatedError):
        start_verification(_SYNTHETIC_PHONE, "sms")


def test_channel_routing_sms_gate_open(monkeypatch):
    monkeypatch.setenv("VT250_SMS_CHANNEL_ENABLED", "1")
    result = start_verification(_SYNTHETIC_PHONE, "sms")
    assert result.channel == "sms"
    assert result.status == "pending"


def test_unknown_channel_rejected():
    with pytest.raises(InvalidChannelError):
        start_verification(_SYNTHETIC_PHONE, "carrier-pigeon")


def test_cl390_no_phone_or_code_in_logs(caplog):
    """CL-390 (LOCKED): the phone + code MUST NOT appear in any log record.

    Capture every record start + check emit and assert neither the synthetic
    phone nor either OTP code is present in any message or its args.
    """
    sensitive = (_SYNTHETIC_PHONE, _CORRECT_OTP, _WRONG_OTP)

    with caplog.at_level(logging.DEBUG, logger="orchestrator.auth.twilio_verify"):
        start_verification(_SYNTHETIC_PHONE, "whatsapp", tenant_id=_SYNTHETIC_TENANT)
        check_verification(_SYNTHETIC_PHONE, _CORRECT_OTP, tenant_id=_SYNTHETIC_TENANT)
        check_verification(_SYNTHETIC_PHONE, _WRONG_OTP, tenant_id=_SYNTHETIC_TENANT)

    assert caplog.records, "expected log records from the verify client"
    for record in caplog.records:
        # Fully-rendered message (format string + args interpolated).
        rendered = record.getMessage()
        for secret in sensitive:
            assert secret not in rendered, (
                f"CL-390 violation: secret found in rendered log: {rendered!r}"
            )
        # Defensive: also scan the raw args tuple/dict, in case a future
        # change passes a secret as a structured arg never interpolated.
        raw_args = record.args
        if isinstance(raw_args, dict):
            arg_values = list(raw_args.values())
        elif isinstance(raw_args, tuple):
            arg_values = list(raw_args)
        else:
            arg_values = [raw_args] if raw_args is not None else []
        for secret in sensitive:
            assert secret not in arg_values, (
                f"CL-390 violation: secret found in log args: {raw_args!r}"
            )

    # The tenant_id IS allowed (and expected) in the log substrate.
    assert any(
        _SYNTHETIC_TENANT in r.getMessage() for r in caplog.records
    ), "expected tenant_id in the log substrate (it is the allowed identifier)"
