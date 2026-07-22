"""VT-691 (Fazal 2026-07-22) — CREATE team_signup_consent_buttons via the Twilio Content API.

Rule 15: a real API call with real creds, fail-NOT-skip. Creates ONE in-session interactive
quick-reply Content object (static bilingual body, TWO buttons) for the WhatsApp-signup consent
ask. NO Meta approval — in-session interactive content (≤3 buttons), sent only in the 24h window
the unknown sender's own inbound just opened (the VT-479/VT-683 precedent).

Fazal ruling (2026-07-22): the signup consent is captured by an EXPLICIT button press — the
signup does not start unless the user presses "I agree"; a "I do not agree" button gives the
explicit refusal path (DPDP/EU). The button TITLE echoes back as the inbound Body, so the grant
set in orchestrator/onboarding/whatsapp_signup.py is the exact-normalized title — keep the
titles in EXACT lockstep with _AGREE_TITLE/_DISAGREE_TITLE there.

Prints ONLY friendly_name / ContentSid — never the auth token.

Usage:
  ( set -a; source .viabe/secrets/twilio-dev.env; set +a; \
    uv run python canaries/vt691_consent_buttons_create.py )
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

TEMPLATE_NAME = "team_signup_consent_buttons"

# Static body (no variables): the consent ask itself, bilingual. Links are the real public
# notice pages. One explicit press covers the same two consents the public page captures as
# checkboxes (DPDP processing notice + India data residency).
BODY = (
    "Namaste! This is Viabe Team — an AI teammate that runs everyday business tasks for you "
    "on WhatsApp.\n\n"
    "To create your account I need your consent: I'll process your business data as described "
    "in our data-processing notice (viabe.ai/team/dpdp) and store it in India "
    "(viabe.ai/team/privacy).\n\n"
    "Tap “I agree” to agree and start your free trial. / शुरू करने के लिए “I agree” दबाएँ।"
)
BUTTONS = [
    {"title": "I agree", "id": "consent_agree"},
    {"title": "I do not agree", "id": "consent_disagree"},
]


def _auth() -> str:
    sid = os.environ.get("TEAM_TWILIO_ACCOUNT_SID", "")
    tok = os.environ.get("TEAM_TWILIO_AUTH_TOKEN", "")
    if not sid or not tok:
        print("FAIL: TEAM_TWILIO_ACCOUNT_SID / TEAM_TWILIO_AUTH_TOKEN not in env", file=sys.stderr)
        sys.exit(2)
    return "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode()


def main() -> int:
    req = urllib.request.Request(
        "https://content.twilio.com/v1/Content",
        data=json.dumps({
            "friendly_name": f"{TEMPLATE_NAME}_en",
            "language": "en",
            "types": {"twilio/quick-reply": {"body": BODY, "actions": BUTTONS}},
        }).encode(),
        headers={"Authorization": _auth(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            created = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 — fail-NOT-skip
        print(f"CANARY FAIL: {type(exc).__name__} — {exc}", file=sys.stderr)
        return 1
    content_sid = created.get("sid", "")
    print(json.dumps({"template": TEMPLATE_NAME, "sid": content_sid}))
    if not content_sid.startswith("HX"):
        print("CANARY FAIL", file=sys.stderr)
        return 1
    print("CANARY OK — wire the SID into config/twilio_templates.yaml + .viabe/templates.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
