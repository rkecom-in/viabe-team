#!/usr/bin/env python3
"""VT-742 exit gate (a) — `no-env-sender-at-send-site`.

``resolve_sender`` must be the ONLY source of a WhatsApp sending number. For two months every send
read ``os.environ["TEAM_TWILIO_FROM_NUMBER"]`` directly at the ``messages.create`` site, which is
why 138 tenants with a provisioned live WABA still sent from the shared Viabe number and why their
customers' replies resolved to no tenant at all (customer inbound routes by the number the customer
messaged TO). A per-tenant sender that any call site can bypass with one env read is not a resolver.

So: forbid the env name inside ``apps/team-orchestrator/src`` anywhere except the resolver, which
owns it as precedence step 3.

Test fixtures, canaries and `.env` files may still set it — they are the environment, not a send
site. Only the orchestrator's shipped source is scanned.

Exit 1 on any occurrence outside the allowlist.
"""

from __future__ import annotations

import subprocess
import sys

_ENV_NAME = "TEAM_TWILIO_FROM_NUMBER"
_SCAN_PREFIX = "apps/team-orchestrator/src/"
_ALLOWLIST = frozenset(
    {
        # The sanctioned single site: precedence step 3 of VT-742 §1.
        "apps/team-orchestrator/src/orchestrator/integrations/sender_resolution.py",
    }
)


def _tracked_python_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", f"{_SCAN_PREFIX}*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def main() -> int:
    violations: list[str] = []
    for rel in _tracked_python_files():
        if rel in _ALLOWLIST:
            continue
        try:
            with open(rel, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if _ENV_NAME in line:
                        violations.append(
                            f"{rel}:{n}: reads {_ENV_NAME} — call "
                            "integrations.sender_resolution.resolve_sender(tenant_id) instead"
                        )
        except (OSError, UnicodeDecodeError):
            continue

    if violations:
        print(
            f"::error::no-env-sender-at-send-site (VT-742): {_ENV_NAME} is the DEFAULT SHARED "
            "sender, not the sender. Reading it at a send site ignores the tenant's own live WABA "
            "and produces a message the customer cannot reply to. Use resolve_sender(tenant_id).",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print("no-env-sender-at-send-site: ok (resolve_sender is the only source of a sending number).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
