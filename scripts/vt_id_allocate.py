#!/usr/bin/env python3
"""VT-ID allocator — atomic monotonic claim under flock.

Replaces the prior Notion auto_increment_id mechanism (sunset 2026-05-25 as part
of the Notion-to-repo migration). Returns the next VT-N number, increments the
persistent counter, all under an exclusive file lock so concurrent callers
(Cowork + agent-loop daemon + human shell) serialize cleanly.

USAGE
-----
    $ python scripts/vt_id_allocate.py
    VT-169

    $ # Use in a shell pipeline:
    $ vt_id="$(python scripts/vt_id_allocate.py)"
    $ mkdir -p ".viabe/queue/${vt_id}"

DESIGN
------
- Counter at .viabe/sprint/.next-id (text file with a single integer).
- Exclusive lock at .viabe/sprint/.lock (flock-protected critical section).
- On first call after fresh checkout, if .next-id is missing or stale,
  fall back to scanning .viabe/sprint/VT-*.md filenames for the max VT-N
  and continue from there + 1. Catches: manual file creates, git pulls
  that bring in higher-numbered rows from another branch, accidental
  .next-id deletion.

GUARANTEES
----------
- No two callers ever receive the same VT-N (flock serializes claims).
- Monotonic: every new ID is strictly greater than every prior issued
  ID (within a single repo clone; cross-branch conflicts surface as
  merge conflicts on .next-id and are resolved by re-allocating the
  lower-numbered claimant).
- Gaps are possible (a caller that claims then crashes before writing
  the .md file leaves a gap). Same behaviour as Notion's
  auto_increment_id when rows are deleted. Acceptable — gaps don't
  break anything.

CROSS-BRANCH SAFETY
-------------------
If you branch off main, allocate VT-200 on feat/foo, and someone else
allocates VT-200 on feat/bar in parallel, merging both produces a
conflict on .next-id (both updated it to 201). Resolution: rename one
of the two VT-200 files (and any references) to the next free number
(VT-202) and re-merge.

Run `python scripts/vt_id_allocate.py --peek` to read the next-to-be-issued
number without consuming it.
"""
from __future__ import annotations

import fcntl
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPRINT_DIR = REPO / ".viabe" / "sprint"
NEXT_ID_FILE = SPRINT_DIR / ".next-id"
LOCK_FILE = SPRINT_DIR / ".lock"

VT_FILE_RE = re.compile(r"^VT-(\d+)\.md$")


def scan_max_vt_id() -> int:
    """Walk .viabe/sprint/ for VT-N.md files; return max N (or 0 if none)."""
    nums = []
    for entry in SPRINT_DIR.glob("VT-*.md"):
        m = VT_FILE_RE.match(entry.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else 0


def reconcile_next_id() -> int:
    """Read .next-id; if missing or lower than scan-max+1, fix and return the
    correct next number. Caller must hold the flock."""
    scan_max = scan_max_vt_id()
    expected_next = scan_max + 1
    if not NEXT_ID_FILE.exists():
        NEXT_ID_FILE.write_text(f"{expected_next}\n", encoding="utf-8")
        return expected_next
    try:
        stored = int(NEXT_ID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        stored = 0
    if stored < expected_next:
        NEXT_ID_FILE.write_text(f"{expected_next}\n", encoding="utf-8")
        return expected_next
    return stored


def allocate() -> str:
    """Claim and return the next VT-N string. Increments the counter."""
    SPRINT_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.touch(exist_ok=True)
    with open(LOCK_FILE, "r", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            current = reconcile_next_id()
            NEXT_ID_FILE.write_text(f"{current + 1}\n", encoding="utf-8")
            return f"VT-{current}"
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def peek() -> str:
    """Return the next VT-N without consuming it. Caller-friendly query."""
    SPRINT_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.touch(exist_ok=True)
    with open(LOCK_FILE, "r", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
        try:
            current = reconcile_next_id()
            return f"VT-{current}"
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def main(argv: list[str]) -> int:
    if "--peek" in argv:
        print(peek())
    else:
        print(allocate())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
