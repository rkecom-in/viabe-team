#!/usr/bin/env python3
"""VT-718 S2 — `gate-owner-emission-choke` (CL-2026-07-28-single-voice-manager).

The single-voice invariant: EVERY owner-bound WhatsApp send flows through the one transport
funnel (`apps/team-orchestrator/src/orchestrator/utils/twilio_send.py`), where the S2 emission
choke (`_owner_emission_guard`) runs. A send path that escapes the funnel escapes the choke —
so escaping the funnel is a CI failure, not a review catch.

Two checks over git-tracked orchestrator Python:

1. NO TWILIO OUTSIDE THE FUNNEL — constructing a Twilio REST client, importing the SDK, or
   posting to the Twilio API host anywhere outside the transport module (+ its dev send-guard
   and the typing-indicator helper inside the same file) is a bypass. Tests are exempt.
2. THE FUNNEL KEEPS ITS GUARD — the transport must still call `_owner_emission_guard(` in the
   freeform + interactive primitives and define `note_owner_inbound` (text anchors, so a
   refactor cannot silently drop the choke).
"""

from __future__ import annotations

import re
import subprocess
import sys

_TRANSPORT = "apps/team-orchestrator/src/orchestrator/utils/twilio_send.py"

#: Files that may legitimately touch the Twilio SDK / API host.
_ALLOWLIST = frozenset(
    {
        _TRANSPORT,
        # The dev send-guard wraps the client the transport constructs (allowlist mock).
        "apps/team-orchestrator/src/orchestrator/utils/dev_send_guard.py",
        # Twilio VERIFY (login OTP) — a Verify-service API call, not conversational voice; it
        # cannot double-speak, and it is already dev-send-guard wrapped (VT-559).
        "apps/team-orchestrator/src/orchestrator/auth/twilio_verify.py",
        # This checker names the forbidden patterns.
        "scripts/check_owner_emission_choke.py",
    }
)

#: A Twilio egress outside the funnel: SDK import, client construction, or the raw API host.
_BYPASS = re.compile(
    r"(from\s+twilio(\.|\s)|import\s+twilio\b|twilio\.rest|api\.twilio\.com|messaging\.twilio\.com)"
)

#: Anchors that must remain in the transport (the guard can't be silently dropped).
_FUNNEL_ANCHORS = (
    "_owner_emission_guard(",
    "def note_owner_inbound",
    "def _owner_emission_guard",
)


def _tracked_orchestrator_py() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "apps/team-orchestrator/src/**/*.py", "apps/team-orchestrator/src/*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def main() -> int:
    violations: list[str] = []

    for rel in _tracked_orchestrator_py():
        if rel in _ALLOWLIST or "/tests/" in rel or rel.rsplit("/", 1)[-1].startswith("test_"):
            continue
        try:
            with open(rel, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if _BYPASS.search(line):
                        violations.append(
                            f"{rel}:{n}: Twilio egress outside the transport funnel — every "
                            "owner-bound send must route through utils/twilio_send.py (VT-718 choke)"
                        )
        except (OSError, UnicodeDecodeError):
            continue

    try:
        with open(_TRANSPORT, encoding="utf-8") as fh:
            transport_src = fh.read()
        for anchor in _FUNNEL_ANCHORS:
            if anchor not in transport_src:
                violations.append(
                    f"{_TRANSPORT}: missing anchor {anchor!r} — the S2 emission choke has been "
                    "dropped from the transport funnel"
                )
        # The two session primitives must each consult the guard (template sends are documented-out).
        for primitive in ("def send_freeform_message", "def send_interactive_message"):
            seg_start = transport_src.find(primitive)
            seg_end = transport_src.find("\ndef ", seg_start + 1) if seg_start != -1 else -1
            segment = transport_src[seg_start : seg_end if seg_end != -1 else None] if seg_start != -1 else ""
            if seg_start == -1 or "_owner_emission_guard(" not in segment:
                violations.append(
                    f"{_TRANSPORT}: {primitive.removeprefix('def ')} no longer consults "
                    "_owner_emission_guard — the choke must run in every session primitive"
                )
    except OSError as exc:
        violations.append(f"{_TRANSPORT}: unreadable ({exc}) — cannot verify the choke anchors")

    if violations:
        print(
            "::error::gate-owner-emission-choke (VT-718): the single-voice invariant is broken — "
            "an owner send path escapes (or the transport dropped) the S2 emission choke.",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print("gate-owner-emission-choke: ok (all Twilio egress inside the choked transport funnel).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
