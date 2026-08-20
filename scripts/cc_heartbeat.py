#!/usr/bin/env python3
"""CC liveness heartbeat that carries a STATE WORD, not just an mtime.

Clau 2026-08-06 (third occurrence of the stall class): `.running/cc-heartbeat` was a 0-byte file
touched for its mtime. A fresh timestamp therefore proved only that SOMETHING still ran — it could
not distinguish alive-and-thinking from alive-and-stuck, and one such stall cost ~4 hours while the
heartbeat ticked all night.

The fix is to separate two different clocks and write both:

  beat_at     — last heartbeat. Proves the process is alive.
  state_since — when the STATE last CHANGED. Proves the work is moving.

A fresh ``beat_at`` under a stale ``state_since`` is a stall, readable in one `cat`. That comparison
is the whole point; keep both fields.

States: ``working:<item>`` · ``idle`` · ``blocked:<why>``.

Written by the WORK loop (at each phase transition), never by a side loop — a side loop would keep
refreshing ``state_since`` while the real work is wedged, which is the exact failure being fixed.

    python scripts/cc_heartbeat.py --state working --item "o11 baseline bundle"
    python scripts/cc_heartbeat.py --state blocked --why "waiting on Fazal: wedge fix authorization"
    python scripts/cc_heartbeat.py --state idle
    python scripts/cc_heartbeat.py --read          # what Clau runs
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

HEARTBEAT = Path(__file__).resolve().parent.parent / ".running" / "cc-heartbeat"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read() -> dict[str, str]:
    """Prior beat, or {} when absent/malformed — a corrupt heartbeat must never crash the work loop."""
    try:
        return json.loads(HEARTBEAT.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — includes the legacy 0-byte file
        return {}


def write(state: str) -> dict[str, str]:
    """Stamp ``state``, preserving ``state_since`` when the state word is UNCHANGED.

    Preserving it is what makes a stall visible: re-announcing the same state must not reset the
    clock that proves how long the work has sat there.
    """
    prior = _read()
    now = _now()
    unchanged = prior.get("state") == state
    beat = {
        "state": state,
        # Written into the file so a bare `cat` carries the contract, not just `--read`.
        "stale_after_minutes": _STALE_WORKING_MINUTES,
        "state_since": prior.get("state_since", now) if unchanged else now,
        "beat_at": now,
    }
    HEARTBEAT.write_text(json.dumps(beat) + "\n", encoding="utf-8")
    return beat


#: A `working` state older than this is reported STALE by --read (exit 3). 30 min is well past any
#: single build/test/push step; anything longer without a state change means the process is gone.
_STALE_WORKING_MINUTES = 30.0


def _stale_minutes(beat: dict[str, str]) -> float | None:
    try:
        since = datetime.strptime(beat["state_since"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except Exception:  # noqa: BLE001
        return None
    return (datetime.now(UTC) - since).total_seconds() / 60


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", choices=("working", "idle", "blocked"))
    ap.add_argument("--item", help="what is being worked on (required for --state working)")
    ap.add_argument("--why", help="what is blocking (required for --state blocked)")
    ap.add_argument("--read", action="store_true", help="print current state + how long it has held")
    args = ap.parse_args()

    if args.read or args.state is None:
        beat = _read()
        if not beat:
            print("cc-heartbeat: EMPTY or legacy 0-byte file — no state word recorded")
            return 1
        held = _stale_minutes(beat)
        held_txt = f"{held:.0f} min" if held is not None else "unknown"
        state = str(beat.get("state") or "")
        print(f"state={state}  held_for={held_txt}  beat_at={beat.get('beat_at')}")
        # A `working` that has not changed in over half an hour is self-evidently stale: it is
        # claiming work is in flight while nothing has moved. Reporting that as a normal reading —
        # which this did, exit 0 and all — is worse than an empty file, because it actively tells
        # a reader the work is alive. CC's state read `working` for TWO DAYS while dead
        # (2026-08-10 → 08-13) and every `--read` in between looked healthy.
        # Non-zero exit so a scripted check fails rather than merely printing.
        if state.startswith("working") and held is not None and held > _STALE_WORKING_MINUTES:
            hours = held / 60
            print(
                f"STALE: state has been '{state.split(':')[0]}' for {hours:.1f}h "
                f"(> {_STALE_WORKING_MINUTES:.0f} min) with no state change. Treat as DEAD, not "
                "in-flight — reconcile against `git log` and the signal inboxes before believing "
                "any work is underway."
            )
            return 3
        return 0

    if args.state == "working":
        if not args.item:
            print("--state working requires --item", file=sys.stderr)
            return 2
        state = f"working:{args.item}"
    elif args.state == "blocked":
        if not args.why:
            print("--state blocked requires --why", file=sys.stderr)
            return 2
        state = f"blocked:{args.why}"
    else:
        state = "idle"

    beat = write(state)
    print(f"cc-heartbeat: {beat['state']} (since {beat['state_since']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
