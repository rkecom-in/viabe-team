#!/usr/bin/env python3
"""Tier 2 dev testing — synthetic Twilio webhook fixture (VT-3.3a, CL-67).

Constructs a Twilio-shaped payload and POSTs it at a locally-running
orchestrator's /api/orchestrator/twilio-ingress endpoint, then prints the
returned workflow_id + status.

Prerequisites:
  - orchestrator running locally:  uvicorn main:app --app-dir src
  - INTERNAL_API_SECRET set in the environment (same value the server uses)

Usage:
  python scripts/synthetic_webhook.py --tenant-id <uuid> --body "STOP" \\
      --sender "+919999999999" [--message-type inbound_message|status_callback]
"""

from __future__ import annotations

import argparse
import os
import uuid

import httpx

_DEFAULT_URL = "http://localhost:8000/api/orchestrator/twilio-ingress"


def main() -> int:
    parser = argparse.ArgumentParser(description="Fire a synthetic Twilio webhook")
    parser.add_argument("--tenant-id", required=True, help="tenant UUID")
    parser.add_argument("--body", default="hello", help="message text")
    parser.add_argument("--sender", default="+919999999999", help="sender phone (E.164)")
    parser.add_argument(
        "--message-type",
        default="inbound_message",
        choices=["inbound_message", "status_callback"],
    )
    parser.add_argument("--url", default=_DEFAULT_URL)
    args = parser.parse_args()

    secret = os.environ.get("INTERNAL_API_SECRET")
    if not secret:
        print("error: INTERNAL_API_SECRET not set in the environment")
        return 1

    twilio_fields: dict[str, str] = {
        "From": args.sender,
        "To": "+910000000000",
        "Body": args.body,
        "MessageSid": f"SM{uuid.uuid4().hex}",
        "NumMedia": "0",
    }
    if args.message_type == "status_callback":
        twilio_fields["MessageStatus"] = "failed"

    response = httpx.post(
        args.url,
        json={"tenant_id": args.tenant_id, "twilio_fields": twilio_fields},
        headers={"X-Internal-Secret": secret},
        timeout=15,
    )
    print(f"HTTP {response.status_code}")
    print(response.text)
    # Workflow status: inspect pipeline_runs / pipeline_steps for the printed
    # run_id, or the orchestrator logs.
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
