"""VT-383 F1 canary — fetch the 10 CL-438 Content SIDs from the Twilio Content API.

Rule 15: real API call, real creds, fail-NOT-skip. Run by CC (live egress) with the
twilio creds consumed from the environment (never printed). Per SID this records:
Meta approval STATUS + the APPROVED body + body_sha256 — the registry pins and the
agent_selectable flips key off THIS output, not our drafts (the approved body is canon).

Asserts (CL-438.1):
  - every fetch succeeds (a 404/401 is a FAILURE, not a skip);
  - the four customer winback bodies carry the customer STOP opt-out line
    (checked against the APPROVED body text, not our documentation).

Usage:
  ( set -a; source .viabe/secrets/twilio-dev.env; set +a; \
    uv run python canaries/vt383_f1_content_api.py )
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request

# The 10 Fazal-delivered SIDs (CL-438, 2026-06-12) — name/(lang) -> SID.
SIDS: dict[tuple[str, str], str] = {
    ("team_winback_simple", "en"): "HX601925a292da89e9d00d3fdf8742f765",
    ("team_winback_simple", "hi"): "HX5da4406f8a6691f52555cd179f40be73",
    ("team_winback_offer", "en"): "HX637d3dc2969a722f627e0dfd2c166b1e",
    ("team_winback_offer", "hi"): "HX9370d1b1a1c917a88ef512b7d545ac46",
    ("team_agent_draft_approval", "en"): "HX1fa31e0339d5739d7936e6edf39e08a3",
    ("team_agent_draft_approval", "hi"): "HX81929b92dd3a159e920b5eb338700cf8",
    ("team_l3_presend_notice", "en"): "HXb114769da63f0c72d4a9f01c2fd0ed80",
    ("team_l3_presend_notice", "hi"): "HX8184dfe127d1f5bc124384192a4793be",
    ("team_autonomy_offer", "en"): "HX150525f3963603ad00d234bd01b37224",
    ("team_autonomy_offer", "hi"): "HXae12acceccc259235478a7a60c53d628",
}

# The customer-facing winbacks whose APPROVED bodies must carry the STOP line.
_STOP_REQUIRED = {"team_winback_simple", "team_winback_offer"}
_STOP_TOKENS = {"en": "STOP", "hi": "STOP"}  # the keyword itself is English on both


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
    import base64

    auth = "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode()

    failures: list[str] = []
    results: list[dict] = []
    for (name, lang), content_sid in SIDS.items():
        try:
            content = _get(f"https://content.twilio.com/v1/Content/{content_sid}", auth)
            approval = _get(
                f"https://content.twilio.com/v1/Content/{content_sid}/ApprovalRequests", auth
            )
        except Exception as exc:  # noqa: BLE001 — fail-not-skip: any fetch error is a failure
            failures.append(f"{name}/{lang} {content_sid}: FETCH FAILED — {exc}")
            continue
        body = (
            (content.get("types") or {}).get("twilio/text", {}).get("body")
            or (content.get("types") or {}).get("twilio/quick-reply", {}).get("body")
            or ""
        )
        wa = (approval.get("whatsapp") or {})
        status = wa.get("status", "unknown")
        sha = hashlib.sha256(body.encode()).hexdigest()
        results.append(
            {
                "template": name,
                "lang": lang,
                "sid": content_sid,
                "meta_status": status,
                "body_sha256": sha,
                "body": body,
                "rejection_reason": wa.get("rejection_reason") or None,
            }
        )
        if name in _STOP_REQUIRED and _STOP_TOKENS[lang] not in body:
            failures.append(
                f"{name}/{lang}: APPROVED body is missing the customer STOP opt-out line"
            )

    print(json.dumps(results, indent=2, ensure_ascii=False))
    if failures:
        print("\nCANARY FAILURES:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"\nCANARY OK: {len(results)}/10 fetched", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
