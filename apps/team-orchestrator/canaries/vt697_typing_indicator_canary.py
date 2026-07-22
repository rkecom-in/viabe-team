"""VT-697 canary — the Twilio v3 WhatsApp TYPING indicator (Public Beta).

Rule 15: a real API call, fail-NOT-skip. Proves the endpoint + payload shape the runtime
uses (`send_typing_indicator`): POST /v3/Indicators/Typing.json with an INBOUND message sid.
Canary-proved 2026-07-23: ``channel`` MUST be uppercase "WHATSAPP" (lowercase → 400); success
returns {"success": true} and the owner's WhatsApp shows read ticks + "typing…" for <=25s.

Usage (sid = a recent INBOUND SM/MM MessageSid for this account):
  ( set -a; source .viabe/secrets/twilio-dev.env; set +a; \
    uv run python canaries/vt697_typing_indicator_canary.py SMxxxxxxxx )
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].startswith(("SM", "MM")):
        print("FAIL: pass a recent INBOUND MessageSid (SM…/MM…)", file=sys.stderr)
        return 2
    sid = os.environ.get("TEAM_TWILIO_ACCOUNT_SID", "")
    tok = os.environ.get("TEAM_TWILIO_AUTH_TOKEN", "")
    if not sid or not tok:
        print("FAIL: creds not in env", file=sys.stderr)
        return 2
    auth = "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode()
    req = urllib.request.Request(
        "https://messaging.twilio.com/v3/Indicators/Typing.json",
        data=json.dumps({"channel": "WHATSAPP", "messageId": sys.argv[1]}).encode(),
        headers={"Authorization": auth, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
    except urllib.error.HTTPError as exc:  # noqa: PERF203
        print(f"CANARY REJECTED ({exc.code}): {exc.read().decode()[:300]}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"CANARY FAIL: {type(exc).__name__} — {exc}", file=sys.stderr)
        return 1
    print(body)
    if '"success":true' not in body.replace(" ", ""):
        print("CANARY FAIL: unexpected body", file=sys.stderr)
        return 1
    print("CANARY OK — typing indicator accepted; the referenced inbound is now read-ticked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
