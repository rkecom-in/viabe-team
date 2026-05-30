#!/usr/bin/env python3
"""VT-250 owner-portal OTP (Twilio Verify) canary (Rule #15, DR-15).

Default = MOCK mode (no network, 0 paise). Exercises the full
``orchestrator.auth.twilio_verify`` surface + the CL-390 no-PII-in-logs
invariant deterministically.

REAL mode — set BOTH:
    VT250_REAL_VERIFY=1
    TWILIO_VERIFY_SERVICE_SID=<the Fazal-provisioned Verify Service SID>
    TEAM_TWILIO_ACCOUNT_SID / TEAM_TWILIO_AUTH_TOKEN
    VT250_CANARY_TEST_PHONE=<a SYNTHETIC test number — NEVER a real owner/
                             customer per CL-422; e.g. a Twilio test/verified
                             number you control>

Real mode makes a LIVE Verify start to the test number, then a check with a
deliberately-wrong code (asserting `denied`/not-approved without a human in
the loop — we cannot read the real OTP here, so the real-mode "approved" leg
is necessarily mock-only). It is FAIL-NOT-SKIP: if VT250_REAL_VERIFY=1 but the
Service SID / creds / test phone are absent, the canary EXITS NON-ZERO (it does
not silently fall back to mock).

CL-422: synthetic only — the real-mode phone MUST be a test number. CL-390:
the phone + OTP NEVER appear in any log line (asserted by A-LOG).

Wall-clock budget ≤ 30s. Cost budget: 0 paise in mock; a few Verify segments
in real mode (1 start + 1 check).

Invocation (mock, default):
    cd apps/team-orchestrator
    uv run --no-project --with twilio python canaries/vt250_owner_otp.py

Invocation (real):
    cd apps/team-orchestrator
    (
      set -a; source ../../.viabe/secrets/twilio-dev.env; set +a
      VT250_REAL_VERIFY=1 VT250_CANARY_TEST_PHONE=+15005550006 \
        uv run --no-project --with twilio python canaries/vt250_owner_otp.py
    )
"""

from __future__ import annotations

import logging
import os
import sys
from io import StringIO
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESULTS: dict[str, dict[str, Any]] = {}

# CL-422 synthetic: a mock-mode phone that is NEVER a real owner/customer.
_MOCK_PHONE = "+919812300250"
_MOCK_TENANT = "11111111-1111-4111-8111-111111111111"
_MOCK_OTP = "654321"
_WRONG_OTP = "000000"


def assertion(key: str, name: str, passed: bool, *, observed=None, expected=None):
    status = "PASS" if passed else "FAIL"
    RESULTS[key] = {"name": name, "status": status}
    print(f"[{key}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _real_mode() -> bool:
    return os.environ.get("VT250_REAL_VERIFY", "0") == "1"


def _preflight_real() -> str:
    """Real-mode preflight — fail-not-skip if anything required is missing."""
    missing = [
        name
        for name in (
            "TWILIO_VERIFY_SERVICE_SID",
            "TEAM_TWILIO_ACCOUNT_SID",
            "TEAM_TWILIO_AUTH_TOKEN",
            "VT250_CANARY_TEST_PHONE",
        )
        if not os.environ.get(name)
    ]
    if missing:
        print(
            "PREFLIGHT FAIL (real mode) — missing: "
            + ", ".join(missing)
            + ". Real mode is fail-not-skip (Rule #15); it does NOT fall back "
            "to mock.",
            file=sys.stderr,
        )
        sys.exit(2)
    test_phone = os.environ["VT250_CANARY_TEST_PHONE"]
    # CL-422 guard surface: refuse anything that is not explicitly flagged as a
    # test number by the operator. We can't truly know it's synthetic, but we
    # force an explicit env so a real owner number is never passed by accident.
    print(
        "PREFLIGHT OK (real mode) — Verify Service SID present; "
        "test phone supplied via VT250_CANARY_TEST_PHONE (operator asserts "
        "SYNTHETIC per CL-422). The phone is NOT printed (CL-390)."
    )
    return test_phone


def _attach_log_capture() -> StringIO:
    """Capture everything the verify client logs so A-LOG can scan it."""
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    logger = logging.getLogger("orchestrator.auth.twilio_verify")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return buf


def run_canary() -> int:
    real = _real_mode()
    real_phone = _preflight_real() if real else None

    if not real:
        os.environ["TEAM_TWILIO_VERIFY_MOCK_MODE"] = "1"
        os.environ["VT250_MOCK_OTP"] = _MOCK_OTP
        os.environ.pop("VT250_SMS_CHANNEL_ENABLED", None)
        print("PREFLIGHT OK (mock mode) — no network, 0 paise.")

    from orchestrator.auth import twilio_verify
    from orchestrator.auth.twilio_verify import (
        ChannelGatedError,
        InvalidChannelError,
        check_verification,
        start_verification,
    )

    twilio_verify._client.cache_clear()
    log_buf = _attach_log_capture()

    phone = real_phone if real else _MOCK_PHONE

    # A1: start (whatsapp, live) → pending
    start = start_verification(phone, "whatsapp", tenant_id=_MOCK_TENANT)
    assertion(
        "A1",
        "start_verification(whatsapp) → status pending + verification_sid",
        start.status == "pending" and bool(start.verification_sid),
        observed={"status": start.status, "has_sid": bool(start.verification_sid)},
        expected={"status": "pending"},
    )

    # A2: check(wrong) → NOT approved (denied). Always exercisable in both modes.
    wrong = check_verification(phone, _WRONG_OTP, tenant_id=_MOCK_TENANT)
    assertion(
        "A2",
        "check_verification(wrong code) → not approved",
        wrong.approved is False,
        observed={"approved": wrong.approved, "status": wrong.status},
        expected={"approved": False},
    )

    # A3: check(correct) → approved. Mock-only (real OTP can't be read here).
    if real:
        assertion(
            "A3",
            "check_verification(correct) → approved [SKIPPED in real mode — "
            "no human-in-loop to read the live OTP; A2 covers the deny path live]",
            True,
            observed={"mode": "real", "note": "approved-leg is mock-only"},
        )
    else:
        ok = check_verification(phone, _MOCK_OTP, tenant_id=_MOCK_TENANT)
        assertion(
            "A3",
            "check_verification(correct mock code) → approved",
            ok.approved is True and ok.status == "approved",
            observed={"approved": ok.approved, "status": ok.status},
            expected={"approved": True},
        )

    # A4: channel routing — sms is GATED OFF by default.
    gated = False
    try:
        start_verification(phone, "sms", tenant_id=_MOCK_TENANT)
    except ChannelGatedError:
        gated = True
    assertion(
        "A4",
        "sms channel GATED OFF by default (ChannelGatedError) — whatsapp is live",
        gated,
        observed={"sms_gated": gated},
        expected={"sms_gated": True},
    )

    # A5: unknown channel → InvalidChannelError.
    invalid = False
    try:
        start_verification(phone, "carrier-pigeon", tenant_id=_MOCK_TENANT)
    except InvalidChannelError:
        invalid = True
    assertion(
        "A5",
        "unknown channel → InvalidChannelError",
        invalid,
        observed={"invalid_rejected": invalid},
        expected={"invalid_rejected": True},
    )

    # A-LOG: CL-390 — no phone / no OTP in any captured log line.
    logged = log_buf.getvalue()
    secrets = [phone, _MOCK_OTP, _WRONG_OTP]
    leaked = [s for s in secrets if s and s in logged]
    assertion(
        "A-LOG",
        "CL-390: no phone / no OTP code in any emitted log line "
        "(only verification_sid + tenant_id)",
        not leaked,
        observed={
            "leaked_count": len(leaked),
            "tenant_in_logs": _MOCK_TENANT in logged,
            "log_chars": len(logged),
        },
        expected={"leaked_count": 0},
    )

    return _finalise(real)


def _finalise(real: bool) -> int:
    print("\n=== CANARY SUMMARY ===")
    for k in RESULTS:
        r = RESULTS[k]
        print(f"  [{k}] {r['status']} — {r['name']}")
    mode = "REAL (live Twilio Verify)" if real else "MOCK (no network)"
    print(f"\n=== mode: {mode} ===")
    if not real:
        print("=== cost: 0 paise (mock-mode canary; no Twilio call) ===")

    failed = [k for k, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print(f"\nALL {len(RESULTS)} ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
