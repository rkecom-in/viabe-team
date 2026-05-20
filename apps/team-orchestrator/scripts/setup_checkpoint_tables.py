#!/usr/bin/env python3
"""Initialize langgraph + DBOS checkpoint tables in target Postgres.

Wrapper around orchestrator.graph.init_substrate. Idempotent.

Precondition: ``public.pipeline_runs`` must exist (created by
apply_migrations.py running repo-root ``migrations/*.sql`` first).
``init_substrate`` installs RLS policies on the checkpoint tables that
reference ``pipeline_runs`` (CL-190 / CL-202 cross-tenant isolation), so
calling it against a fresh database fails partway through.

Exit codes:
  0 — checkpoint tables initialized
  1 — DATABASE_URL/TEAM_SUPABASE_DB_URL not set
  2 — init_substrate raised after precondition passed
  3 — pipeline_runs precondition not met; run apply_migrations.py first
"""

from __future__ import annotations

import os
import sys

import psycopg

from orchestrator.graph import init_substrate


def resolve_dsn() -> str | None:
    return os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")


def pipeline_runs_exists(dsn: str) -> bool:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pipeline_runs')")
            row = cur.fetchone()
            return row is not None and row[0] is not None


def main() -> int:
    dsn = resolve_dsn()
    if not dsn:
        print(
            "setup_checkpoint_tables: set DATABASE_URL or TEAM_SUPABASE_DB_URL",
            file=sys.stderr,
        )
        return 1

    try:
        if not pipeline_runs_exists(dsn):
            print(
                "setup_checkpoint_tables: pipeline_runs not found. "
                "Run apply_migrations.py first (from apps/team-orchestrator/).",
                file=sys.stderr,
            )
            return 3
    except Exception as exc:
        print(f"precondition check failed: {exc}", file=sys.stderr)
        return 2

    try:
        init_substrate(dsn)
    except Exception as exc:
        print(f"init_substrate failed: {exc}", file=sys.stderr)
        return 2

    print("Checkpoint tables initialized")
    return 0


if __name__ == "__main__":
    sys.exit(main())
