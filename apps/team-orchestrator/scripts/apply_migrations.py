#!/usr/bin/env python3
"""Apply ordered SQL migrations to the shared viabe-team Postgres database.

Pillar 8 (no patchwork): this is the ONE migration runner. There is no shadow
migration mechanism — every schema change is a versioned file in /migrations/.

Connection
----------
Reads a direct Postgres DSN from ``DATABASE_URL`` (preferred) or
``TEAM_SUPABASE_DB_URL``. Note that ``TEAM_SUPABASE_URL`` is the Supabase REST
API URL and is NOT a Postgres DSN — psycopg needs the project's direct
connection string. Its password is the Supabase database password, rotated
alongside ``TEAM_SUPABASE_SECRET_KEY``.

Behaviour
---------
- Creates ``schema_migrations`` (id, name, applied_at) if absent.
- Iterates ``/migrations/*.sql`` in alphabetical order.
- Applies each unapplied file inside its own transaction, then records it.
- Idempotent: re-running skips already-applied files.
- Stops at the first failure and reports it; exit code is non-zero.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

# apply_migrations.py -> scripts -> team-orchestrator -> apps -> repo root
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def resolve_dsn() -> str:
    """Return the Postgres DSN, or exit with guidance if none is configured."""
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")
    if not dsn:
        sys.exit(
            "apply_migrations: set DATABASE_URL or TEAM_SUPABASE_DB_URL "
            "(a direct Postgres DSN, not the Supabase REST URL)"
        )
    return dsn


def migration_files(migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    """Return all migration files in deterministic (alphabetical) order."""
    return sorted(migrations_dir.glob("*.sql"))


def _has_executable_sql(sql: str) -> bool:
    """True if the file holds statements beyond SQL comments / whitespace."""
    return any(
        line.strip() and not line.strip().startswith("--")
        for line in sql.splitlines()
    )


def apply(dsn: str | None = None, migrations_dir: Path = MIGRATIONS_DIR) -> dict:
    """Apply pending migrations. Returns {'applied', 'skipped', 'failed'}."""
    dsn = dsn or resolve_dsn()
    applied: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id         INT PRIMARY KEY,
                name       TEXT UNIQUE NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        next_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM schema_migrations"
        ).fetchone()[0]

        for path in migration_files(migrations_dir):
            name = path.name
            if name in done:
                skipped.append(name)
                continue
            sql = path.read_text()
            try:
                with conn.transaction():
                    if _has_executable_sql(sql):
                        conn.execute(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (id, name) VALUES (%s, %s)",
                        (next_id, name),
                    )
                applied.append(name)
                next_id += 1
            except Exception as exc:  # noqa: BLE001 — reported, then we stop
                failed.append((name, str(exc)))
                break

    return {"applied": applied, "skipped": skipped, "failed": failed}


def main() -> int:
    result = apply()
    for name in result["skipped"]:
        print(f"  skip    {name}")
    for name in result["applied"]:
        print(f"  applied {name}")
    for name, error in result["failed"]:
        print(f"  FAILED  {name}: {error}", file=sys.stderr)

    print(
        f"\n{len(result['applied'])} applied, "
        f"{len(result['skipped'])} skipped, "
        f"{len(result['failed'])} failed"
    )
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
