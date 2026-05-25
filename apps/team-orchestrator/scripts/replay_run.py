#!/usr/bin/env python3
"""Replay a run's reasoning timeline (VT-104, ops-only Phase 1).

Usage::

    python apps/team-orchestrator/scripts/replay_run.py <run_id>

Prints a chronologically-ordered timeline of every ``pipeline_log`` event
for the given ``run_id``. Reads through :func:`query_run` so workspace-
level (tenant_id NULL) rows surface alongside tenant rows. PII has
already been redacted at write time by VT-102's writer; this script
performs NO additional redaction (defence-in-depth: the write boundary
is the single seam).

Service-role only — exposes raw payload structure for ops debugging.
NOT exposed to tenants in Phase 1 (see VT-104 brief §3).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import UUID

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _ensure_pool() -> None:
    """Initialise the connection pool from ``DATABASE_URL`` if not already up."""
    from orchestrator import graph as graph_mod

    if graph_mod._pool is not None:
        return
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(2)
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    graph_mod._pool = ConnectionPool(
        db_url,
        min_size=1,
        max_size=4,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=True,
    )


def replay(run_id: UUID | str) -> int:
    """Print the timeline for ``run_id`` to stdout. Returns process exit code."""
    _ensure_pool()
    from orchestrator.observability import query_run

    events = query_run(run_id)
    if not events:
        print(f"No events for run_id={run_id}")
        return 1

    print(f"=== Replay: run_id={run_id} ({len(events)} events) ===")
    for e in events:
        ts = e.created_at.isoformat() if e.created_at else "<no-ts>"
        tenant = str(e.tenant_id) if e.tenant_id else "<workspace>"
        head = (
            f"[{ts}] {e.event_type} severity={e.severity} "
            f"component={e.component} tenant={tenant}"
        )
        if e.duration_ms is not None:
            head += f" duration_ms={e.duration_ms}"
        print(head)
        # Payload one-line summary; full JSON pretty-printed below.
        payload_str = json.dumps(e.payload, default=str)
        if len(payload_str) > 200:
            print(f"  payload (truncated): {payload_str[:200]}...")
        else:
            print(f"  payload: {payload_str}")
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: replay_run.py <run_id>", file=sys.stderr)
        return 2
    try:
        run_id = UUID(sys.argv[1])
    except ValueError:
        print(f"invalid uuid: {sys.argv[1]!r}", file=sys.stderr)
        return 2
    return replay(run_id)


if __name__ == "__main__":
    sys.exit(main())
