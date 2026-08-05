#!/usr/bin/env python3
"""Migration-number allocator — atomic monotonic claim under flock (VT-249).

Mirrors ``scripts/vt_id_allocate.py`` for SQL migration numbers. Migration
files in ``migrations/`` are ordered by their zero-padded 3-digit numeric
prefix (``NNN_description.sql``); the runner (``apply_migrations.py``) applies
them in ``sorted(glob("*.sql"))`` order and tracks by name. Picking the "next
free number" by scanning the directory is NOT concurrency-safe: two parallel
subagents (ultracode fan-out, or Cowork + agent-loop) scanning at the same
time both grab the same number — the collision we hit repeatedly (e.g. VT-240
and VT-86 both reaching for 047). This allocator serializes the claim under an
exclusive file lock, exactly like the VT-ID allocator.

USAGE
-----
    $ python scripts/migration_id_allocate.py
    048

    $ # shell pipeline:
    $ n="$(python scripts/migration_id_allocate.py)"
    $ touch "migrations/${n}_my_change.sql"

    $ python scripts/migration_id_allocate.py --peek   # read without consuming
    048

DESIGN
------
- Counter at ``migrations/.next-migration`` (single integer).
- Exclusive lock at ``migrations/.lock`` (flock-protected critical section).
- On first call after a fresh checkout, or if ``.next-migration`` is missing
  or stale, fall back to scanning ``migrations/NNN_*.sql`` for the max prefix
  and continue from there + 1. Catches manual creates and git pulls that bring
  higher-numbered migrations from another branch.

CROSS-BRANCH SAFETY
-------------------
Same model as the VT-ID allocator: the persisted counter carries reservations
that on-disk scanning can't see. If migration NNN exists on an in-flight branch
but not yet on the branch you're on, the committed ``.next-migration`` keeps you
from re-issuing NNN. Parallel claims on two branches that both reach NNN+1
surface as a merge conflict on ``.next-migration``; resolve by renaming one
migration file to the next free number.

GUARANTEES
----------
- No two callers ever receive the same number (flock serializes claims).
- Monotonic within a clone; gaps are possible and harmless (the runner tracks
  by name, so a gap never skips an unrelated migration).

Returns a zero-padded 3-digit string ("048"), matching the on-disk convention.
"""
from __future__ import annotations

import argparse
import fcntl
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO / "migrations"
NEXT_FILE = MIGRATIONS_DIR / ".next-migration"
LOCK_FILE = MIGRATIONS_DIR / ".lock"
#: Append-only claim record: ts · number · claimed-by · purpose. See _journal().
JOURNAL_FILE = MIGRATIONS_DIR / ".allocation-journal"

MIGRATION_FILE_RE = re.compile(r"^(\d+)_.*\.sql$")


def scan_max_migration() -> int:
    """Walk migrations/ for NNN_*.sql files; return max NNN (or 0 if none)."""
    nums = []
    for entry in MIGRATIONS_DIR.glob("*.sql"):
        m = MIGRATION_FILE_RE.match(entry.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else 0


def reconcile_next() -> int:
    """Read .next-migration; if missing or lower than scan-max+1, fix and
    return the correct next number. Caller must hold the flock."""
    scan_max = scan_max_migration()
    expected_next = scan_max + 1
    if not NEXT_FILE.exists():
        NEXT_FILE.write_text(f"{expected_next}\n", encoding="utf-8")
        return expected_next
    try:
        stored = int(NEXT_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        stored = 0
    if stored < expected_next:
        NEXT_FILE.write_text(f"{expected_next}\n", encoding="utf-8")
        return expected_next
    return stored


def _fmt(n: int) -> str:
    """Zero-pad to at least 3 digits, matching the on-disk convention."""
    return f"{n:03d}"


def _journal(number: str, claimed_by: str, purpose: str) -> None:
    """Append one claim to the journal. Caller MUST hold the exclusive flock.

    Why this exists: on 2026-08-06 the counter showed 191-193 consumed and Clau attributed all
    three to CC's lane. 192 and 193 were CC's; **191 was never created by anyone** and is absent
    from the tree AND from git history. A bare counter records THAT a number went, never WHO took
    it or WHY, so an unaccounted allocation is indistinguishable from a collision — which is the
    exact failure mode CL-424 created this allocator to prevent. The journal makes the question
    answerable in one read instead of by inference.

    Fail-soft: a journal write must never block a claim the lock has already granted. A missing
    line is a gap in the record; a raised exception here would be a gap in the migration sequence.
    """
    try:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(JOURNAL_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp}\t{number}\t{claimed_by}\t{purpose}\n")
    except OSError:
        pass


def allocate(claimed_by: str = "unknown", purpose: str = "unstated") -> str:
    """Claim and return the next migration number. Increments the counter."""
    MIGRATIONS_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.touch(exist_ok=True)
    with open(LOCK_FILE, "r", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            current = reconcile_next()
            NEXT_FILE.write_text(f"{current + 1}\n", encoding="utf-8")
            number = _fmt(current)
            # Inside the lock, so journal order matches claim order under parallel callers.
            _journal(number, claimed_by, purpose)
            return number
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def peek() -> str:
    """Return the next migration number without consuming it."""
    MIGRATIONS_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.touch(exist_ok=True)
    with open(LOCK_FILE, "r", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
        try:
            return _fmt(reconcile_next())
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Claim the next migration number under flock")
    ap.add_argument("--peek", action="store_true", help="read the next number without consuming it")
    ap.add_argument("--by", default="unknown", help='who is claiming (e.g. "cc", "clau", "codex")')
    ap.add_argument("--for", dest="purpose", default="unstated", help='why (e.g. "VT-735 tiers")')
    args = ap.parse_args(argv)
    print(peek() if args.peek else allocate(args.by, args.purpose))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
