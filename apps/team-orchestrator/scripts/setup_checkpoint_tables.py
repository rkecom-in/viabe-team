#!/usr/bin/env python3
"""Initialize langgraph + DBOS checkpoint tables in target Postgres.

Thin wrapper around orchestrator.graph.init_substrate. Idempotent.
"""

from __future__ import annotations

import os
import sys

from orchestrator.graph import init_substrate


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1
    try:
        init_substrate(database_url)
    except Exception as exc:
        print(f"init_substrate failed: {exc}", file=sys.stderr)
        return 2
    print("Checkpoint tables initialized")
    return 0


if __name__ == "__main__":
    sys.exit(main())
