"""VT-486 canary — confirm the team_reengage Content SIDs LOAD via the Twilio Content API.

Rule 15: a REAL API call, real creds, fail-NOT-skip. Run by CC (live egress) with the Twilio
creds consumed from the environment (never printed — Rule #18 by-reference). The SEND itself is
mocked by the VT-476 dev send-guard; THIS canary only proves the two SIDs resolve (a real lookup
returns a Content object), so the out-of-window owner send can reference a real template.

A 404 / 401 / fetch error is a FAILURE, not a skip. Prints only the SID, the body, and the
fetched meta status — never the auth token.

Usage:
  ( set -a; source .viabe/secrets/twilio-dev.env; set +a; \
    uv run python canaries/vt486_reengage_content_api.py )
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import urllib.request

# VT-486 Fazal-provided SIDs — name/(lang) -> SID.
SIDS: dict[tuple[str, str], str] = {
    ("team_reengage", "en"): "HXbdb250089fafc02a0d75ce6817e9ce11",
    ("team_reengage", "hi"): "HX27a50d65fedbb7b6a3c2fb6a6a24f13c",
}


def _get(url: str, auth_header: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": auth_header})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    sid = os.environ.get("TEAM_TWILIO_ACCOUNT_SID", "")
    tok = os.environ.get("TEAM_TWILIO_AUTH_TOKEN", "")
    if not sid or not tok:
        print("FAIL: TEAM_TWILIO_ACCOUNT_SID / TEAM_TWILIO_AUTH_TOKEN not in env", file=sys.stderr)
        return 2

    auth = "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode()

    failures: list[str] = []
    results: list[dict] = []
    for (name, lang), content_sid in SIDS.items():
        try:
            content = _get(f"https://content.twilio.com/v1/Content/{content_sid}", auth)
        except Exception as exc:  # noqa: BLE001 — fail-not-skip: any fetch error is a failure
            failures.append(f"{name}/{lang} {content_sid}: FETCH FAILED — {exc}")
            continue
        body = (content.get("types") or {}).get("twilio/text", {}).get("body") or ""
        if not body:
            failures.append(f"{name}/{lang} {content_sid}: loaded but carries no twilio/text body")
        results.append(
            {
                "template": name,
                "lang": lang,
                "sid": content_sid,
                "loaded": True,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "body": body,
            }
        )

    print(json.dumps(results, indent=2, ensure_ascii=False))
    if failures:
        print("\nCANARY FAILURES:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"\nCANARY OK: {len(results)}/{len(SIDS)} team_reengage SIDs loaded", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
