"""VT-555 canary — CREATE team_welcome4 (UTILITY quick-reply welcome) via the Twilio Content API.

Rule 15: a real API call with real creds, fail-NOT-skip. Run by CC (live egress) with the twilio
creds consumed from the environment (NEVER printed). Creates the EN + HI content objects
(twilio/quick-reply, ONE var {{1}} = owner name, a "Complete Setup" COMPLETE_SETUP button) and submits
each for WhatsApp approval as category=UTILITY, grouped under the shared template name `team_welcome4`.

Prints ONLY: friendly_name / language / ContentSid / approval status — never the auth token.

Usage:
  ( set -a; source .viabe/secrets/twilio-dev.env; set +a; \
    uv run python canaries/vt555_welcome4_create.py )
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

TEMPLATE_NAME = "team_welcome4"

BODIES: dict[str, str] = {
    "en": "Hi {{1}}, your Viabe account has been created. To complete your setup, tap the button below.",
    "hi": "नमस्ते {{1}}, आपका Viabe अकाउंट बन गया है। सेटअप पूरा करने के लिए नीचे दिए गए बटन पर टैप करें।",
}
BUTTON_TITLE: dict[str, str] = {"en": "Complete Setup", "hi": "सेटअप पूरा करें"}
BUTTON_ID = "COMPLETE_SETUP"  # stable payload, SAME id both languages


def _auth() -> str:
    sid = os.environ.get("TEAM_TWILIO_ACCOUNT_SID", "")
    tok = os.environ.get("TEAM_TWILIO_AUTH_TOKEN", "")
    if not sid or not tok:
        print("FAIL: TEAM_TWILIO_ACCOUNT_SID / TEAM_TWILIO_AUTH_TOKEN not in env", file=sys.stderr)
        sys.exit(2)
    return "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode()


def _post(url: str, auth: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": auth, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    auth = _auth()
    results: list[dict] = []
    failures: list[str] = []
    for lang in ("en", "hi"):
        try:
            created = _post(
                "https://content.twilio.com/v1/Content",
                auth,
                {
                    "friendly_name": f"{TEMPLATE_NAME}_{lang}",
                    "language": lang,
                    "variables": {"1": "Ravi"},
                    "types": {
                        "twilio/quick-reply": {
                            "body": BODIES[lang],
                            "actions": [{"title": BUTTON_TITLE[lang], "id": BUTTON_ID}],
                        }
                    },
                },
            )
            content_sid = created.get("sid", "")
            if not content_sid.startswith("HX"):
                failures.append(f"{lang}: create returned no HX sid: {created!r}")
                continue
            approval = _post(
                f"https://content.twilio.com/v1/Content/{content_sid}/ApprovalRequests/whatsapp",
                auth,
                {"name": TEMPLATE_NAME, "category": "UTILITY"},
            )
            status = (approval.get("whatsapp") or {}).get("status") or approval.get("status", "?")
            results.append({"lang": lang, "sid": content_sid, "approval_status": status})
        except Exception as exc:  # noqa: BLE001 — fail-NOT-skip: any create/submit error is a failure
            failures.append(f"{lang}: {type(exc).__name__} — {exc}")

    print(json.dumps({"template": TEMPLATE_NAME, "results": results, "failures": failures}, indent=2))
    if failures or len(results) != 2:
        print("CANARY FAIL", file=sys.stderr)
        return 1
    print("CANARY OK — wire the two SIDs into config/twilio_templates.yaml + .viabe/templates.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
