"""VT-362 — environment guard on the migration runner.

Proves a wrong-env migration is STRUCTURALLY impossible: apply_migrations refuses unless the connected
DB matches an explicitly-passed --expected-env (the `app_environment` sentinel + a bootstrap host-ref
check). Dev-only — needs a live Postgres via DATABASE_URL (the migrations CI job / pre-push local PG);
no prod creds. Each test runs against its OWN throwaway database so the sentinel can differ per case.
"""

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — migration env-guard tests skipped",
)

import apply_migrations as am  # noqa: E402 — after the psycopg skip guard
from apply_migrations import EnvironmentGuardError  # noqa: E402


@pytest.fixture
def fresh_db():
    """Create a throwaway database, yield its DSN, drop it after (sentinel isolation per test)."""
    base = os.environ["DATABASE_URL"]
    info = psycopg.conninfo.conninfo_to_dict(base)
    name = f"viabe_guard_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = psycopg.conninfo.make_conninfo(**{**info, "dbname": name})
    try:
        yield dsn
    finally:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,)
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _has_table(dsn: str, table: str) -> bool:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0] is not None


# --- bootstrap (fresh DB, no sentinel) ---------------------------------------
def test_bootstrap_requires_host_substr(fresh_db):
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        with pytest.raises(EnvironmentGuardError, match="bootstrap"):
            am.guard_environment(conn, fresh_db, "dev", None)
    assert not _has_table(fresh_db, "app_environment")  # nothing stamped on refusal


def test_bootstrap_wrong_host_aborts(fresh_db):
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        with pytest.raises(EnvironmentGuardError, match="host-check FAILED"):
            am.guard_environment(conn, fresh_db, "dev", "definitely-not-this-host-xyz")
    assert not _has_table(fresh_db, "app_environment")


def test_bootstrap_stamps_then_steady_and_mismatch(fresh_db):
    host = am.dsn_host(fresh_db)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        assert am.guard_environment(conn, fresh_db, "dev", host) == "bootstrap"
        assert conn.execute("SELECT name FROM app_environment").fetchone()[0] == "dev"
        # steady-state: matching env passes, repeatedly
        assert am.guard_environment(conn, fresh_db, "dev", host) == "steady"
        # the wrong env is refused
        with pytest.raises(EnvironmentGuardError, match="ENV MISMATCH"):
            am.guard_environment(conn, fresh_db, "prod", host)


def test_invalid_env_rejected(fresh_db):
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        with pytest.raises(EnvironmentGuardError, match="must be one of"):
            am.guard_environment(conn, fresh_db, "staging", am.dsn_host(fresh_db))


# --- the acceptance assertions (full apply() path) ---------------------------
def test_apply_aborts_on_env_mismatch_zero_applied(fresh_db):
    host = am.dsn_host(fresh_db)
    with psycopg.connect(fresh_db, autocommit=True) as conn:  # stamp the DB as 'dev'
        am.guard_environment(conn, fresh_db, "dev", host)
    # a prod-intended apply against the dev-stamped DB must abort before ANY migration
    with pytest.raises(EnvironmentGuardError, match="ENV MISMATCH"):
        am.apply(dsn=fresh_db, expected_env="prod", expected_host_substr=host)
    assert not _has_table(fresh_db, "schema_migrations")  # guard ran before schema_migrations


def test_main_missing_expected_env_returns_2(fresh_db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", fresh_db)
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    assert am.main(argv=[]) == 2  # refuses without an explicit target env
    assert not _has_table(fresh_db, "schema_migrations")  # nothing touched


def test_apply_dev_against_dev_proceeds(fresh_db):
    host = am.dsn_host(fresh_db)
    result = am.apply(dsn=fresh_db, expected_env="dev", expected_host_substr=host)  # bootstrap + apply
    assert result["failed"] == []
    assert result["applied"]  # migrations ran
    assert _has_table(fresh_db, "app_environment") and _has_table(fresh_db, "schema_migrations")


def test_unguarded_apply_still_works_for_test_fixtures(fresh_db):
    """The programmatic path (no expected_env) is unguarded — test fixtures depend on it against
    throwaway local DBs, which never touch the dev/prod Supabase the guard protects."""
    result = am.apply(dsn=fresh_db)  # no expected_env -> no guard
    assert result["failed"] == []
    assert not _has_table(fresh_db, "app_environment")  # an unguarded run never stamps the sentinel
