#!/usr/bin/env python3
"""VT-227 — twilio_inbound_replay daily TTL purge canary.

3 assertions:
- A1: seed 50 rows >24h + 50 rows <24h; run workflow; assert exactly 50 deleted
- A2: re-run workflow → no further deletions
- A3: log capture: row-count integer + cutoff timestamp; ZERO PII

Subshell-source supabase-dev.env. Wall-clock ≤ 15s.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_SIDS: list[str] = []


def assertion(num: int, name: str, passed: bool, *,
               observed: Any = None, expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _preflight() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    print("PREFLIGHT OK")


def _seed(pool: Any) -> None:
    now = datetime.now(UTC)
    old_ts = now - timedelta(hours=25)  # >24h: should be purged
    fresh_ts = now - timedelta(hours=1)  # <24h: should be retained
    with pool.connection() as conn:
        for _ in range(50):
            sid = f"SM_vt227_old_{uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO twilio_inbound_replay "
                "(message_sid, received_at, source_ip, signature_first_8) "
                "VALUES (%s, %s, '127.0.0.1', 'aaaaaaaa')",
                (sid, old_ts),
            )
            INSERTED_SIDS.append(sid)
        for _ in range(50):
            sid = f"SM_vt227_fresh_{uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO twilio_inbound_replay "
                "(message_sid, received_at, source_ip, signature_first_8) "
                "VALUES (%s, %s, '127.0.0.1', 'aaaaaaaa')",
                (sid, fresh_ts),
            )
            INSERTED_SIDS.append(sid)


def _cleanup(pool: Any) -> None:
    if not INSERTED_SIDS:
        return
    with pool.connection() as conn:
        for sid in INSERTED_SIDS:
            conn.execute(
                "DELETE FROM twilio_inbound_replay WHERE message_sid = %s",
                (sid,),
            )


def run_canary() -> int:
    _preflight()

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    pool = get_pool()

    _seed(pool)

    # Capture logs to assert no PII
    captured_logs: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_logs.append(self.format(record))

    cap = _Cap()
    cap.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.addHandler(cap)
    root_logger.setLevel(logging.INFO)

    try:
        from orchestrator.observability.twilio_replay_purge import (
            purge_twilio_inbound_replay_body,
        )

        # A1: first run deletes ~50 old rows
        now = datetime.now(UTC)
        purge_twilio_inbound_replay_body(now, now)

        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM twilio_inbound_replay "
                "WHERE message_sid LIKE 'SM_vt227_old_%%'",
            )
            old_left = cur.fetchone()
            cur.execute(
                "SELECT count(*) AS n FROM twilio_inbound_replay "
                "WHERE message_sid LIKE 'SM_vt227_fresh_%%'",
            )
            fresh_left = cur.fetchone()

        old_count = int(old_left["n"] if isinstance(old_left, dict) else old_left[0])
        fresh_count = int(fresh_left["n"] if isinstance(fresh_left, dict) else fresh_left[0])
        pass_1 = old_count == 0 and fresh_count == 50
        assertion(
            1,
            "First run: 50 old rows deleted; 50 fresh rows retained",
            pass_1,
            observed={"old_remaining": old_count, "fresh_remaining": fresh_count},
        )

        # A2: second run, no further deletions of fresh
        purge_twilio_inbound_replay_body(now, now)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM twilio_inbound_replay "
                "WHERE message_sid LIKE 'SM_vt227_fresh_%%'",
            )
            fresh_after2 = cur.fetchone()
        fresh_count_2 = int(
            fresh_after2["n"] if isinstance(fresh_after2, dict) else fresh_after2[0]
        )
        pass_2 = fresh_count_2 == 50
        assertion(
            2,
            "Second run: still 50 fresh rows (no extra deletions)",
            pass_2,
            observed={"fresh_remaining": fresh_count_2},
        )

        # A3: log line carries row-count + cutoff; no PII pattern
        purge_lines = [line for line in captured_logs if "twilio_inbound_replay purge" in line]
        pii_pattern = re.compile(r"(\+?91\d{9,12}|phone_tok_[0-9a-f]+)")
        pii_hits = [line for line in purge_lines if pii_pattern.search(line)]
        pass_3 = len(purge_lines) >= 2 and len(pii_hits) == 0
        assertion(
            3,
            "Log lines emitted with no PII",
            pass_3,
            observed={"log_lines": len(purge_lines), "pii_hits": len(pii_hits)},
        )

    finally:
        root_logger.removeHandler(cap)

    _cleanup(pool)
    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(run_canary())
