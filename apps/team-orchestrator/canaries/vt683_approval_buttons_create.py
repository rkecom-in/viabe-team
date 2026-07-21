"""VT-683 P2c canary — CREATE team_approval_buttons (in-session interactive approval quick-reply)
via the Twilio Content API.

Rule 15: a real API call with real creds, fail-NOT-skip. Run by CC (live egress) with the twilio
creds consumed from the environment (NEVER printed). Creates the EN + HI content objects
(twilio/quick-reply, ONE var {{1}} = the approval ask text, two decision buttons) and does NOT
submit for Meta approval — in-session interactive content (≤3 buttons, sent only inside an open
24h window via send_interactive_message) needs no Meta template approval; the HX is a Twilio-side
registration only (the VT-479 onboarding_confirm_yesno precedent).

Button-title contract (the load-bearing part): a tap echoes the TITLE back as the inbound Body,
which try_resume_pending_approval classifies via classify_approval_reply. Every title below is
verified against the deterministic token sets:
  en "Yes, approve"     -> {yes, approve}: _STRONG_APPROVE, no negation        -> approved
  en "No, reject"       -> {no, reject}:   _NEGATION + _REJECT_KW              -> rejected
  hi "हाँ, मंज़ूर है"      -> {हाँ, ...}:     _STRONG_APPROVE (हाँ)                -> approved
  hi "नहीं, रहने दो"      -> {नहीं, ...}:    bare _NEGATION, no affirmation       -> rejected
None collides with the opt-out/DSR guard (no "stop"/"cancel"/"band"), none is weak-ack-only
(strong yes present), all are <= 12 tokens. Change a title ONLY in lockstep with those sets.

Prints ONLY: friendly_name / language / ContentSid — never the auth token.

Usage:
  ( set -a; source .viabe/secrets/twilio-dev.env; set +a; \
    uv run python canaries/vt683_approval_buttons_create.py )
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

TEMPLATE_NAME = "team_approval_buttons"

BODIES: dict[str, str] = {
    # {{1}} = the PII-safe approval ask text (payload.summary, composed by the arming caller).
    "en": "{{1}}",
    "hi": "{{1}}",
}
BUTTONS: dict[str, list[dict[str, str]]] = {
    "en": [
        {"title": "Yes, approve", "id": "approval_yes"},
        {"title": "No, reject", "id": "approval_no"},
    ],
    "hi": [
        {"title": "हाँ, मंज़ूर है", "id": "approval_yes"},
        {"title": "नहीं, रहने दो", "id": "approval_no"},
    ],
}


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
                    "variables": {"1": "Send the diwali offer to 12 lapsed customers?"},
                    "types": {
                        "twilio/quick-reply": {
                            "body": BODIES[lang],
                            "actions": BUTTONS[lang],
                        }
                    },
                },
            )
            content_sid = created.get("sid", "")
            if not content_sid.startswith("HX"):
                failures.append(f"{lang}: create returned no HX sid: {created!r}")
                continue
            # NO ApprovalRequests submission — in-session interactive content needs no Meta approval.
            results.append({"lang": lang, "sid": content_sid})
        except Exception as exc:  # noqa: BLE001 — fail-NOT-skip: any create error is a failure
            failures.append(f"{lang}: {type(exc).__name__} — {exc}")

    print(json.dumps({"template": TEMPLATE_NAME, "results": results, "failures": failures}, indent=2))
    if failures or len(results) != 2:
        print("CANARY FAIL", file=sys.stderr)
        return 1
    print("CANARY OK — wire the two SIDs into config/twilio_templates.yaml + .viabe/templates.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
