"""VT-249 — tests for the repo-root migration-number allocator.

Exercises scripts/migration_id_allocate.py (a repo-level infra tool, tested
here because this is where CI collects Python tests). Pure stdlib — runs even
in the dep-less smoke job. The critical test is concurrency: parallel claims
must never collide (the gap the allocator closes for ultracode fan-out)."""

from __future__ import annotations

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "migration_id_allocate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("migration_id_allocate", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def alloc(tmp_path):
    """Load the allocator with its directory + counter + lock redirected to a
    fresh tmp dir, so tests never touch the real migrations/ counter."""
    mod = _load_module()
    mig = tmp_path / "migrations"
    mig.mkdir()
    mod.MIGRATIONS_DIR = mig
    mod.NEXT_FILE = mig / ".next-migration"
    mod.LOCK_FILE = mig / ".lock"
    return mod


def test_seeds_from_scan_when_counter_absent(alloc):
    """No .next-migration → seed from max on-disk prefix + 1."""
    (alloc.MIGRATIONS_DIR / "045_a.sql").write_text("-- x")
    (alloc.MIGRATIONS_DIR / "046_b.sql").write_text("-- x")
    assert alloc.peek() == "047"


def test_peek_does_not_consume(alloc):
    alloc.NEXT_FILE.write_text("048\n")
    assert alloc.peek() == "048"
    assert alloc.peek() == "048"  # still 048 — peek is non-consuming


def test_allocate_consumes_and_increments(alloc):
    alloc.NEXT_FILE.write_text("048\n")
    assert alloc.allocate() == "048"
    assert alloc.allocate() == "049"
    assert alloc.peek() == "050"


def test_zero_padded_three_digits(alloc):
    alloc.NEXT_FILE.write_text("9\n")
    assert alloc.allocate() == "009"
    assert alloc.allocate() == "010"


def test_stored_counter_wins_over_lower_scan(alloc):
    """Cross-branch reservation: an in-flight migration (e.g. VT-240's 047) is
    not on disk here, but the committed counter (048) keeps us from re-issuing
    it. The persisted counter is exactly what on-disk scanning misses."""
    (alloc.MIGRATIONS_DIR / "046_on_main.sql").write_text("-- x")  # scan max = 046
    alloc.NEXT_FILE.write_text("048\n")  # but 047 is reserved elsewhere
    assert alloc.peek() == "048"
    assert alloc.allocate() == "048"


def test_concurrent_claims_never_collide(alloc):
    """The whole point: N parallel allocators must each get a distinct number.
    Each allocate() opens its own fd on the lock file, so flock serializes the
    critical section across threads (and processes)."""
    alloc.NEXT_FILE.write_text("048\n")
    n = 32
    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(lambda _: alloc.allocate(), range(n)))
    assert len(set(results)) == n, f"collision: {sorted(results)}"
    # Contiguous block 048..048+n-1, no gaps, no dupes.
    assert sorted(int(r) for r in results) == list(range(48, 48 + n))
    assert alloc.peek() == f"{48 + n:03d}"
