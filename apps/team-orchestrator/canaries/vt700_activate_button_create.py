"""VT-700 canary — the ACTIVATE TEAM consent ask as a tappable quick-reply.

Rule 15: a real Content API call, fail-NOT-skip. The data-inputs consent GRANT floor is the
exact phrase "ACTIVATE TEAM" (pre_filter exact match; config/data_inputs_enable_keywords.yaml)
— so the button TITLE is exactly that phrase: a tap echoes it as the inbound Body and rides the
EXISTING deterministic grant path with zero classifier change. "Not now" echoes into the
deterministic decline handling (decline-ack + re-ask). Body = {{1}} (the full consent prompt),
so the enable phrase lands in conversation_log via the interactive send's var-1 recording and
the runner's consent-ask recognition is unchanged.

Usage:
  ( set -a; source .viabe/secrets/twilio-dev.env; set +a; \
    uv run python canaries/vt700_activate_button_create.py )
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

TEMPLATE_NAME = "team_activate_button"


def main() -> int:
    sid = os.environ.get("TEAM_TWILIO_ACCOUNT_SID", "")
    tok = os.environ.get("TEAM_TWILIO_AUTH_TOKEN", "")
    if not sid or not tok:
        print("FAIL: creds not in env", file=sys.stderr)
        return 2
    auth = "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode()
    req = urllib.request.Request(
        "https://content.twilio.com/v1/Content",
        data=json.dumps({
            "friendly_name": f"{TEMPLATE_NAME}_en",
            "language": "en",
            "variables": {"1": "Your AI team is ready — go-ahead needed."},
            "types": {"twilio/quick-reply": {
                "body": "{{1}}",
                "actions": [
                    {"title": "ACTIVATE TEAM", "id": "activate_yes"},
                    {"title": "Not now", "id": "activate_later"},
                ],
            }},
        }).encode(),
        headers={"Authorization": auth, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            created = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:  # noqa: PERF203
        print(f"CANARY REJECTED ({exc.code}): {exc.read().decode()[:300]}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"CANARY FAIL: {type(exc).__name__} — {exc}", file=sys.stderr)
        return 1
    content_sid = created.get("sid", "")
    print(json.dumps({"template": TEMPLATE_NAME, "sid": content_sid}))
    if not content_sid.startswith("HX"):
        print("CANARY FAIL", file=sys.stderr)
        return 1
    print("CANARY OK — register the SID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
