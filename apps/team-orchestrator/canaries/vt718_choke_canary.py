"""VT-718 — emission-choke canary (Rule #15): prove the S2 guard on the DEPLOYED-dev env path.

Drives the REAL transport (send_freeform_message) with a bogus +1555 owner number — the VT-476
dev send-guard mocks the Twilio edge, nothing leaves the building — and asserts the suppression
truth table live: duplicate-no-inbound suppressed (enforce) / logged (shadow), inbound-between
repeat allowed, distinct text allowed. Mode is driven in-process (the flag reads per-call), so
one run proves shadow AND enforce regardless of the Railway var.

Run:  railway run --service vt-orchestrator-service --environment development -- \
        uv run python canaries/vt718_choke_canary.py
Fails loudly (exit 1) on any wrong outcome — never skip-on-error.
"""

from __future__ import annotations

import logging
import os
import random
import sys

os.environ.setdefault("TEAM_TWILIO_MOCK_MODE", "0")  # dev send-guard is the mock edge, not mock-mode
# Sealed Railway vars read unset under `railway run` (known class) — the canary needs only
# CONSISTENT values, never the real ones: the salt hashes a bogus number, and the dev
# send-guard mocks the Twilio edge before any credential is used.
os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt718-canary-local-salt")
os.environ.setdefault("TEAM_TWILIO_ACCOUNT_SID", "ACcanary000000000000000000000000000")
os.environ.setdefault("TEAM_TWILIO_AUTH_TOKEN", "canary-local-token")
os.environ.setdefault("TEAM_TWILIO_FROM_NUMBER", "+910000000000")
os.environ.setdefault("EXPECTED_ENV", "dev")  # fail-closed guard posture; never prod

logging.basicConfig(level=logging.WARNING, format="%(message)s")

from orchestrator.utils.twilio_send import (  # noqa: E402
    CHOKE_SUPPRESSED_SID,
    note_owner_inbound,
    send_freeform_message,
)

PHONE = f"+1555{random.randint(1_000_000, 9_999_999)}"  # bogus range; dev guard mocks it
BODY = "Canary line: your weekly report lands every Monday."

checks: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    checks.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}: {name}")


def main() -> int:
    # --- SHADOW: duplicate logs a warning but BOTH send ---------------------------------------
    os.environ["TEAM_OWNER_EMISSION_CHOKE"] = "shadow"
    s1 = send_freeform_message(BODY, PHONE)
    s2 = send_freeform_message(BODY, PHONE)
    check("shadow: first send real", s1 != CHOKE_SUPPRESSED_SID)
    check("shadow: duplicate still sends (log-only)", s2 != CHOKE_SUPPRESSED_SID)

    # --- ENFORCE: duplicate suppressed -------------------------------------------------------
    os.environ["TEAM_OWNER_EMISSION_CHOKE"] = "enforce"
    s3 = send_freeform_message(BODY, PHONE)
    check("enforce: duplicate suppressed (sentinel)", s3 == CHOKE_SUPPRESSED_SID)

    # --- inbound-between: verbatim repeat becomes legitimate ----------------------------------
    note_owner_inbound(PHONE)
    s4 = send_freeform_message(BODY, PHONE)
    check("enforce: repeat AFTER owner inbound sends", s4 != CHOKE_SUPPRESSED_SID)

    # --- distinct text never suppressed -------------------------------------------------------
    s5 = send_freeform_message("A different line entirely.", PHONE)
    check("enforce: distinct text sends", s5 != CHOKE_SUPPRESSED_SID)

    # --- customer path untouched by the owner choke (effect gate still fail-closed) ----------
    from orchestrator.utils.twilio_send import UngatedCustomerSendError

    try:
        send_freeform_message(BODY, PHONE, is_customer_session=True)
        check("customer effect gate still fail-closed", False)
    except UngatedCustomerSendError:
        check("customer effect gate still fail-closed", True)

    failed = [n for n, ok in checks if not ok]
    print(f"\n=== vt718 choke canary: {len(checks) - len(failed)}/{len(checks)} PASS ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
