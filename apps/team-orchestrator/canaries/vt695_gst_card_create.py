"""VT-695 canary — the FORMATTED GST identity card Content object (en + hi).

Rule 15: real Content API calls, fail-NOT-skip. Replaces the semicolon-blob confirm text with a
structured in-session card: static multi-line body (WhatsApp bold/emoji formatting lives in the
TEMPLATE — variables must stay single-line per Meta), 5 per-field variables, fixed Yes/No/Skip
quick-reply buttons (titles round-trip through the journey's _YES/_NO/_SKIP token sets unchanged).

Usage:
  ( set -a; source .viabe/secrets/twilio-dev.env; set +a; \
    uv run python canaries/vt695_gst_card_create.py )
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

TEMPLATE_NAME = "journey_gst_card"

BODY_EN = (
    "Found your business online \U0001f50e\n\n"
    "*{{1}}*\n"
    "{{2}}\n\n"
    "\U0001f4cd {{3}}\n"
    "\U0001f4bc {{4}}\n"
    "\U0001f9fe GSTIN ending {{5}}\n\n"
    "Is this your business?"
)
BODY_HI = (
    "आपका बिज़नेस ऑनलाइन मिला \U0001f50e\n\n"
    "*{{1}}*\n"
    "{{2}}\n\n"
    "\U0001f4cd {{3}}\n"
    "\U0001f4bc {{4}}\n"
    "\U0001f9fe GSTIN अंत {{5}}\n\n"
    "क्या यही आपका बिज़नेस है?"
)
SAMPLE = {
    "1": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
    "2": "Private Limited Company",
    "3": "A/403, Dheeraj Heritage, Santacruz West, Mumbai 400054",
    "4": "Supplier of Services",
    "5": "…B1ZE",
}


def _create(auth: str, lang: str, body: str, actions_yes_no_skip: list[dict[str, str]]) -> str:
    req = urllib.request.Request(
        "https://content.twilio.com/v1/Content",
        data=json.dumps({
            "friendly_name": f"{TEMPLATE_NAME}_{lang}",
            "language": lang,
            "variables": SAMPLE,
            "types": {"twilio/quick-reply": {"body": body, "actions": actions_yes_no_skip}},
        }).encode(),
        headers={"Authorization": auth, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        created = json.loads(resp.read().decode())
    return created.get("sid", "")


def main() -> int:
    sid = os.environ.get("TEAM_TWILIO_ACCOUNT_SID", "")
    tok = os.environ.get("TEAM_TWILIO_AUTH_TOKEN", "")
    if not sid or not tok:
        print("FAIL: creds not in env", file=sys.stderr)
        return 2
    auth = "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode()
    out: dict[str, str] = {}
    for lang, body, actions in (
        ("en", BODY_EN, [
            {"title": "Yes", "id": "gst_yes"},
            {"title": "No", "id": "gst_no"},
            {"title": "Skip", "id": "gst_skip"},
        ]),
        ("hi", BODY_HI, [
            {"title": "हां", "id": "gst_yes"},
            {"title": "नहीं", "id": "gst_no"},
            {"title": "Skip", "id": "gst_skip"},
        ]),
    ):
        try:
            content_sid = _create(auth, lang, body, actions)
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            print(f"CANARY REJECTED {lang} ({exc.code}): {exc.read().decode()[:300]}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"CANARY FAIL {lang}: {type(exc).__name__} — {exc}", file=sys.stderr)
            return 1
        if not content_sid.startswith("HX"):
            print(f"CANARY FAIL {lang}: no HX sid", file=sys.stderr)
            return 1
        out[lang] = content_sid
    print(json.dumps({"template": TEMPLATE_NAME, "sids": out}))
    print("CANARY OK — formatted multi-line body + single-line vars ACCEPTED; register both SIDs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
