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

import argparse
import os
import sys
import urllib.parse
from pathlib import Path

import psycopg

# apply_migrations.py -> scripts -> team-orchestrator -> apps -> repo root
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

# VT-362 environment guard. The runner connects to whatever DATABASE_URL is injected (dev=Seoul,
# prod=Mumbai), so a wrong-env run is the catastrophic failure (dev seed -> prod real-PII DB, or a
# migration at the wrong target). The guard makes that STRUCTURALLY impossible: a CLI run must state
# its intended env and the connected DB must prove it matches before ANY migration applies.
VALID_ENVS = ("dev", "prod")


class EnvironmentGuardError(SystemExit):
    """Raised (as a SystemExit) when the connected DB does not match the expected environment."""


def resolve_dsn() -> str:
    """Return the Postgres DSN, or exit with guidance if none is configured."""
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")
    if not dsn:
        sys.exit(
            "apply_migrations: set DATABASE_URL or TEAM_SUPABASE_DB_URL "
            "(a direct Postgres DSN, not the Supabase REST URL)"
        )
    return dsn


def dsn_host(dsn: str) -> str | None:
    """Extract ONLY the connection host from a DSN — never the password (CL-431: no secret plaintext).
    Handles both URL DSNs (postgresql://...) and libpq keyword DSNs."""
    try:
        info = psycopg.conninfo.conninfo_to_dict(dsn)
        host = info.get("host")
        if host:
            return str(host)
    except Exception:  # noqa: BLE001 — fall through to URL parsing
        pass
    try:
        return urllib.parse.urlsplit(dsn).hostname
    except Exception:  # noqa: BLE001
        return None


def _table_exists(conn: psycopg.Connection, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()
    return bool(row and row[0] is not None)


def guard_environment(
    conn: psycopg.Connection,
    dsn: str,
    expected_env: str,
    expected_host_substr: str | None = None,
) -> str:
    """REFUSE to proceed unless the connected DB matches ``expected_env``. Aborts (EnvironmentGuardError)
    on any mismatch, BEFORE any migration applies. Never prints the DSN/password — only the host +
    sentinel value (CL-431).

    Steady state: the ``app_environment`` sentinel (one row) must equal ``expected_env``.
    Bootstrap (fresh DB, no sentinel): requires ``expected_host_substr`` and asserts the connection
    host contains it (a non-secret project-identity check), then stamps the sentinel so every later
    run is sentinel-guarded.
    """
    if expected_env not in VALID_ENVS:
        raise EnvironmentGuardError(
            f"apply_migrations: --expected-env must be one of {VALID_ENVS}, got {expected_env!r}"
        )

    if _table_exists(conn, "app_environment"):
        rows = conn.execute("SELECT name FROM app_environment").fetchall()
        if len(rows) != 1:
            raise EnvironmentGuardError(
                f"apply_migrations: app_environment must hold exactly one row, found {len(rows)} "
                "— refusing (tampered sentinel)."
            )
        actual = rows[0][0]
        if actual != expected_env:
            raise EnvironmentGuardError(
                f"apply_migrations: ENV MISMATCH — the connected DB is stamped '{actual}' but "
                f"--expected-env is '{expected_env}'. Refusing to apply (VT-362 wrong-env guard)."
            )
        print(f"  env-guard: sentinel '{actual}' == expected '{expected_env}' ✓")
        return "steady"

    # Bootstrap: no sentinel yet -> require a non-secret host-identity check before stamping.
    host = dsn_host(dsn)
    if not expected_host_substr:
        raise EnvironmentGuardError(
            "apply_migrations: the app_environment sentinel is absent (fresh DB) — a bootstrap run "
            "REQUIRES --expected-host-substr (or EXPECTED_HOST_SUBSTR) to verify the connection "
            "identity before stamping. Refusing (VT-362 bootstrap guard)."
        )
    if not host or expected_host_substr not in host:
        raise EnvironmentGuardError(
            f"apply_migrations: bootstrap host-check FAILED — connection host {host!r} does not "
            f"contain the expected '{expected_host_substr}' for env '{expected_env}'. Refusing "
            "(VT-362 wrong-project guard)."
        )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_environment ("
        " singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),"
        " name text NOT NULL CHECK (name IN ('dev','prod')))"
    )
    conn.execute(
        "INSERT INTO app_environment (name) VALUES (%s) ON CONFLICT (singleton) DO NOTHING",
        (expected_env,),
    )
    print(
        f"  env-guard: bootstrap — host {host!r} contains '{expected_host_substr}'; "
        f"stamped sentinel '{expected_env}' ✓"
    )
    return "bootstrap"


def migration_files(migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    """Return all migration files in deterministic (alphabetical) order."""
    return sorted(migrations_dir.glob("*.sql"))


def _has_executable_sql(sql: str) -> bool:
    """True if the file holds statements beyond SQL comments / whitespace."""
    return any(
        line.strip() and not line.strip().startswith("--")
        for line in sql.splitlines()
    )


def apply(
    dsn: str | None = None,
    migrations_dir: Path = MIGRATIONS_DIR,
    *,
    expected_env: str | None = None,
    expected_host_substr: str | None = None,
) -> dict:
    """Apply pending migrations. Returns {'applied', 'skipped', 'failed'}.

    VT-362: when ``expected_env`` is given (the CLI path), the environment guard runs FIRST and aborts
    on any mismatch before a single migration applies. When ``expected_env`` is None (programmatic
    callers — test fixtures apply to throwaway LOCAL databases), the guard is skipped: those never
    touch the dev/prod Supabase projects the guard exists to protect.
    """
    dsn = dsn or resolve_dsn()
    applied: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    with psycopg.connect(dsn, autocommit=True) as conn:
        if expected_env is not None:
            guard_environment(conn, dsn, expected_env, expected_host_substr)
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
        next_id_row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM schema_migrations"
        ).fetchone()
        next_id = next_id_row[0] if next_id_row else 1

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


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="apply_migrations",
        description="Apply ordered SQL migrations behind the VT-362 environment guard.",
    )
    parser.add_argument(
        "--expected-env",
        choices=VALID_ENVS,
        default=os.environ.get("EXPECTED_ENV"),
        help="REQUIRED: the environment you INTEND to migrate (dev|prod). No default — the caller "
        "must state intent; the run aborts if the connected DB does not match.",
    )
    parser.add_argument(
        "--expected-host-substr",
        default=os.environ.get("EXPECTED_HOST_SUBSTR"),
        help="Required only on a fresh DB (no app_environment sentinel yet): a substring the "
        "connection host must contain, so a bootstrap run still can't hit the wrong project.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if not args.expected_env:
        print(
            "apply_migrations: --expected-env {dev|prod} is REQUIRED (or the EXPECTED_ENV env var). "
            "Refusing to apply without an explicit target environment (VT-362).",
            file=sys.stderr,
        )
        return 2
    try:
        result = apply(
            expected_env=args.expected_env,
            expected_host_substr=args.expected_host_substr,
        )
    except EnvironmentGuardError as exc:
        print(str(exc), file=sys.stderr)
        return 3

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
