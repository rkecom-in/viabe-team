"""VT-694 canary — can a twilio/quick-reply Content object carry VARIABLE button titles?

Rule 15: a real Content API call, fail-NOT-skip. Decides the VT-694 suggestion-buttons design:
if button titles accept {{n}} variables, ONE generic object serves every journey question with
dynamic suggested answers ({{1}}=question, {{2}}..{{4}}=suggestions); if Twilio rejects it, the
fallback is text-listed suggestions. Creates a 3-suggestion object and prints the SID.

Usage:
  ( set -a; source .viabe/secrets/twilio-dev.env; set +a; \
    uv run python canaries/vt694_suggest_buttons_create.py )
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

TEMPLATE_NAME = "journey_suggest_3"


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
            "variables": {"1": "Which city are you in?", "2": "Mumbai", "3": "Delhi", "4": "Bengaluru"},
            "types": {"twilio/quick-reply": {
                "body": "{{1}}",
                "actions": [
                    {"title": "{{2}}", "id": "suggest_1"},
                    {"title": "{{3}}", "id": "suggest_2"},
                    {"title": "{{4}}", "id": "suggest_3"},
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
    print("CANARY OK — variable button titles ACCEPTED; wire the SID into the registry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
