#!/usr/bin/env python3
"""VT-738 — backfill the forensic tables whose queries failed during a re-drive.

Why this exists: the first re-drive's dump guessed two column names wrong
(`manager_task_steps.attempt`, `pipeline_runs.created_at`) and lost those two tables to an
`UndefinedColumn`. The dump reported the loss instead of skipping quietly, and `--keep-tenants`
means the rows are still there — so the run does not have to be repeated.

**There is a deadline.** `test_tenant_reaper._REAP_AGE_HOURS = 1`: an hour after the run the kept
tenants are gone and so is the evidence. Run this as soon as the re-drive finishes.

Reads every `*.json` in the forensics dir, re-queries the tables that recorded an error, and
rewrites each file in place with the recovered rows.

    set -a; source .viabe/secrets/supabase-dev.env; set +a
    uv run --no-project --with 'psycopg[binary]' python canaries/vt738_backfill_forensics.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

FORENSICS = Path(__file__).resolve().parent / "_reports" / "vt738_forensics"

REPAIRED: dict[str, str] = {
    "manager_task_steps": (
        "SELECT id, task_id, step_seq, kind, status, specialist, version, created_at, updated_at "
        "FROM manager_task_steps WHERE tenant_id = %(t)s ORDER BY created_at"
    ),
    "pipeline_runs": (
        "SELECT id, run_type, status, final_outcome, error_summary, started_at, ended_at "
        "FROM pipeline_runs WHERE tenant_id = %(t)s ORDER BY started_at"
    ),
}


def main() -> int:
    import psycopg

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("FATAL: DATABASE_URL not set (source .viabe/secrets/supabase-dev.env)")
        return 2
    if not FORENSICS.is_dir():
        print(f"FATAL: no forensics dir at {FORENSICS}")
        return 2

    files = sorted(FORENSICS.glob("*.json"))
    if not files:
        print(f"nothing to backfill in {FORENSICS}")
        return 0

    repaired_files = missing_tenants = 0
    with psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn:
        for path in files:
            payload = json.loads(path.read_text())
            tenant_id = payload.get("tenant_id")
            broken = [t for t, v in payload.items() if isinstance(v, dict) and "error" in v]
            if not broken or not tenant_id:
                continue
            # A tenant already eaten by the reaper cannot be recovered — say so per file rather
            # than writing an empty list that would read as "there was nothing here".
            exists = conn.execute(
                "SELECT 1 FROM tenants WHERE id = %s", (tenant_id,)
            ).fetchone()
            if not exists:
                missing_tenants += 1
                print(f"  REAPED {path.name}: tenant {tenant_id[:8]}… is gone — NOT recoverable")
                continue
            for table in broken:
                sql = REPAIRED.get(table)
                if sql is None:
                    continue
                cur = conn.execute(sql, {"t": tenant_id})
                cols = [d[0] for d in (cur.description or [])]
                payload[table] = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
                print(f"  {path.name}: {table} -> {len(payload[table])} row(s)")
            path.write_text(json.dumps(payload, indent=1, default=str))
            repaired_files += 1

    print(f"backfilled {repaired_files} file(s); {missing_tenants} tenant(s) already reaped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
