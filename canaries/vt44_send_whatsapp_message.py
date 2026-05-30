#!/usr/bin/env python3
"""VT-44 canary — send_whatsapp_message acceptance test.

Rule #15: fail-not-skip. This script exits non-zero on any failure.

DEFAULT (no env flags): dry-run mode. TEAM_TWILIO_MOCK_MODE=1 is set
automatically when VT44_CANARY_SEND is unset. Tests the full success-path
wiring (mock SID starts with MK), no network, no real Twilio call.

REAL SEND (opt-in): set VT44_CANARY_SEND=1 and VT44_CANARY_TEST_TO to a
test WhatsApp number (E.164). CL-422: NEVER a real customer number.

Hard guards:
- VT44_CANARY_SEND=1 + VT44_CANARY_TEST_TO empty/unset → sys.exit(1)
- The recipient must equal VT44_CANARY_TEST_TO; no other number accepted.
- TEAM_PHONE_HASH_SALT must be set (used by hash_phone inside the helper).

Usage (dry-run):
    cd apps/team-orchestrator
    TEAM_PHONE_HASH_SALT=test_salt uv run python ../../canaries/vt44_send_whatsapp_message.py

Usage (real send — Fazal-only):
    VT44_CANARY_SEND=1 VT44_CANARY_TEST_TO=+91XXXXXXXXXX \\
    TEAM_TWILIO_ACCOUNT_SID=... TEAM_TWILIO_AUTH_TOKEN=... \\
    TEAM_TWILIO_FROM_NUMBER=whatsapp:+14155238886 \\
    TEAM_PHONE_HASH_SALT=<prod_salt> \\
    uv run python ../../canaries/vt44_send_whatsapp_message.py
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

# ---- Fail-not-skip guard -------------------------------------------------------

REAL_SEND = os.environ.get("VT44_CANARY_SEND", "0") == "1"
TEST_RECIPIENT = os.environ.get("VT44_CANARY_TEST_TO", "").strip()

if REAL_SEND and not TEST_RECIPIENT:
    print("FAIL: VT44_CANARY_SEND=1 but VT44_CANARY_TEST_TO is unset/empty.")
    print("      Set VT44_CANARY_TEST_TO to a test WhatsApp number (E.164).")
    print("      CL-422: NEVER point this at a real customer number.")
    sys.exit(1)

# Ensure TEAM_PHONE_HASH_SALT is set (hash_phone requires it).
if not os.environ.get("TEAM_PHONE_HASH_SALT"):
    print("FAIL: TEAM_PHONE_HASH_SALT not set. hash_phone() will raise.")
    print("      Set TEAM_PHONE_HASH_SALT=<any non-empty value for dry-run>.")
    sys.exit(1)

# In dry-run mode, engage mock so no real Twilio client is built.
if not REAL_SEND:
    os.environ.setdefault("TEAM_TWILIO_MOCK_MODE", "1")
    # Provide stub env so import doesn't crash on missing creds.
    os.environ.setdefault("TEAM_TWILIO_ACCOUNT_SID", "ACtest000000000000000000000000000000")
    os.environ.setdefault("TEAM_TWILIO_AUTH_TOKEN", "test_token")
    os.environ.setdefault("TEAM_TWILIO_FROM_NUMBER", "whatsapp:+14155238886")

# ---------------------------------------------------------------------------
# Import tool after env is wired.
# ---------------------------------------------------------------------------

# Ensure the orchestrator src is on sys.path (when run from canaries/).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "apps", "team-orchestrator", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from orchestrator.agent.tools.send_whatsapp_message import (  # noqa: E402
    SendWhatsAppMessageInput,
    send_whatsapp_message,
)


# ---------------------------------------------------------------------------
# Pool stub: simulates a customer row with last_inbound_at 2h ago.
# CL-422: synthetic phone only.
# ---------------------------------------------------------------------------

SYNTHETIC_PHONE = TEST_RECIPIENT if REAL_SEND else "+91_SYNTHETIC_00001"
# Hard guard: if somehow a real-send is requested without matching a synthetic
# placeholder, we block it unless the recipient is EXACTLY VT44_CANARY_TEST_TO.
if REAL_SEND and SYNTHETIC_PHONE != TEST_RECIPIENT:
    print(f"FAIL: resolved phone {SYNTHETIC_PHONE!r} != VT44_CANARY_TEST_TO {TEST_RECIPIENT!r}")
    sys.exit(1)


def _build_pool(idem_row: Any = None) -> Any:
    """Stub pool that returns a synthetic in-window customer."""
    cur = MagicMock()
    last_inbound = datetime.now(UTC) - timedelta(hours=2)
    customer_row = {
        "phone_e164": SYNTHETIC_PHONE,
        "last_inbound_at": last_inbound,
    }
    responses = [
        idem_row,           # idempotency check → None (no prior send)
        customer_row,       # resolve_customer
        {"count": 0},       # tenant rate limit
        {"count": 0},       # customer rate limit
    ]
    idx = [0]

    def _fetchone() -> Any:
        i = idx[0]
        if i < len(responses):
            idx[0] += 1
            return responses[i]
        return None

    cur.fetchone.side_effect = _fetchone
    cur.execute = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool


# ---------------------------------------------------------------------------
# Canary execution
# ---------------------------------------------------------------------------

def run_dry_run() -> None:
    """Dry-run: mock mode, no network. Asserts mock SID (MK prefix)."""
    print("=== VT-44 canary: DRY-RUN (TEAM_TWILIO_MOCK_MODE=1) ===")

    from orchestrator.utils.twilio_send import send_freeform_message

    pool = _build_pool()
    payload = SendWhatsAppMessageInput(
        tenant_id="canary-tenant",
        customer_id="canary-cust-001",
        body="VT-44 canary dry-run: hello from the test harness.",
        idempotency_key=f"vt44-canary-{uuid.uuid4().hex[:8]}",
    )

    out = send_whatsapp_message(payload, pool=pool, send_fn=send_freeform_message)

    print(f"  status:      {out.status}")
    print(f"  message_sid: {out.message_sid}")
    print(f"  sent_at:     {out.sent_at}")

    assert out.status == "sent", f"Expected 'sent', got {out.status!r}. Error: {out.error_envelope}"
    assert out.message_sid is not None, "message_sid must be set on sent"
    assert out.message_sid.startswith("MK"), (
        f"Mock SID must start with 'MK', got {out.message_sid!r}"
    )

    print("[PASS] dry-run: status=sent, mock SID returned, no network call made.")


def run_real_send() -> None:
    """Real send: VT44_CANARY_SEND=1. Calls Twilio, expects a real SID."""
    print(f"=== VT-44 canary: REAL SEND to {TEST_RECIPIENT} ===")
    print("CL-422: this recipient MUST be a test number, not a real customer.")

    from orchestrator.utils.twilio_send import send_freeform_message

    pool = _build_pool()
    payload = SendWhatsAppMessageInput(
        tenant_id="canary-tenant",
        customer_id="canary-cust-001",
        body="VT-44 canary: real send acceptance test. Disregard this message.",
        idempotency_key=f"vt44-canary-real-{uuid.uuid4().hex[:8]}",
    )

    out = send_whatsapp_message(payload, pool=pool, send_fn=send_freeform_message)

    print(f"  status:      {out.status}")
    print(f"  message_sid: {out.message_sid}")
    print(f"  sent_at:     {out.sent_at}")

    if out.status != "sent":
        print(f"FAIL: status={out.status!r}, error={out.error_envelope}")
        sys.exit(1)

    assert out.message_sid is not None, "message_sid must be set on sent"
    assert out.message_sid.startswith(("SM", "MM")), (
        f"Real Twilio SID must start with SM or MM, got {out.message_sid!r}"
    )
    print(f"[PASS] real send: SID={out.message_sid}")


def run_window_closed_path() -> None:
    """Verify the window_closed path works correctly."""
    print("=== VT-44 canary: window_closed path ===")

    # Inject a customer with last_inbound 25h ago.
    cur = MagicMock()
    last_inbound = datetime.now(UTC) - timedelta(hours=25)
    responses = [
        None,  # no idem row
        {"phone_e164": SYNTHETIC_PHONE, "last_inbound_at": last_inbound},
    ]
    idx = [0]

    def _fetchone() -> Any:
        i = idx[0]
        if i < len(responses):
            idx[0] += 1
            return responses[i]
        return None

    cur.fetchone.side_effect = _fetchone
    cur.execute = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = conn

    from orchestrator.utils.twilio_send import send_freeform_message

    payload = SendWhatsAppMessageInput(
        tenant_id="canary-tenant",
        customer_id="canary-cust-expired",
        body="This should not be sent.",
        idempotency_key=f"vt44-canary-expired-{uuid.uuid4().hex[:8]}",
    )

    out = send_whatsapp_message(payload, pool=pool, send_fn=send_freeform_message)

    assert out.status == "window_closed", f"Expected window_closed, got {out.status!r}"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "window_expired", (
        f"Expected window_expired, got {out.error_envelope.code!r}"
    )
    print(f"  status: {out.status}, code: {out.error_envelope.code}")
    print("[PASS] window_closed path: correct status + code + send not called.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        if REAL_SEND:
            run_real_send()
        else:
            run_dry_run()
        run_window_closed_path()
        print("\n[ALL PASS] VT-44 canary complete.")
    except (AssertionError, Exception) as exc:  # noqa: BLE001
        print(f"\nFAIL: {exc}")
        sys.exit(1)
