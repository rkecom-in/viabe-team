"""Migration-173 ledger — end-to-end proof against a real migrated Postgres.

Self-provisions a THROWAWAY database (from ``LEDGER_TEST_PG_DSN`` or the local
default ``postgres://localhost:5432/postgres``), applies ALL migrations, and proves:
  * a tenant-scoped ``llm_call_events`` INSERT lands under ``app_role`` + the
    ``app.current_tenant`` GUC, carrying the ``compute_cost_usd`` value;
  * RLS FORCE isolates it — another tenant's GUC can't see/read/write it, and the
    WITH CHECK rejects a cross-tenant insert;
  * a platform (NULL-tenant) row is service-role territory — written by the
    privileged role, invisible to ``app_role``.
Then DROPs the database. Skips cleanly when no Postgres is reachable.

``langchain_core`` is importorskip'd because importing ``orchestrator.llm.pricing``
pulls the package ``__init__`` (which imports the provider seam). The DB-only CI job
(no langchain) skips this; the full-dep run exercises it.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("langchain_core")

import apply_migrations  # noqa: E402
from orchestrator.llm.pricing import compute_cost_usd  # noqa: E402

_BASE_DSN = os.environ.get("LEDGER_TEST_PG_DSN", "postgres://localhost:5432/postgres")


def _swap_dbname(dsn: str, dbname: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def _pg_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=2):
            return True
    except Exception:  # noqa: BLE001 — any connect failure => skip the suite
        return False


@pytest.fixture(scope="module")
def throwaway_db():
    if not _pg_reachable(_BASE_DSN):
        pytest.skip(f"no Postgres reachable at {_BASE_DSN} — ledger PG integration skipped")

    dbname = f"llm_ledger_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_BASE_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{dbname}"')  # noqa: S608 — dbname is a generated hex literal
    test_dsn = _swap_dbname(_BASE_DSN, dbname)
    try:
        result = apply_migrations.apply(dsn=test_dsn)  # unguarded local-throwaway path
        assert result["failed"] == [], f"migrations failed: {result['failed']}"
        yield test_dsn
    finally:
        with psycopg.connect(_BASE_DSN, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (dbname,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{dbname}"')


def _scope(conn, tenant_id) -> None:
    conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant_id),))


def test_model_pricing_seeded(throwaway_db):
    from orchestrator.llm import pricing as pricing_mod

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        rows = {
            r[0]: (r[1], r[2], r[3], r[4])
            for r in conn.execute(
                "SELECT model, usd_per_mtok_in, usd_per_mtok_out, "
                "       discount_multiplier, cached_in_multiplier FROM model_pricing"
            )
        }
    assert rows["claude-sonnet-5"][:2] == (Decimal("2.0000"), Decimal("10.0000"))
    assert rows["gpt-5.6-sol"][:2] == (Decimal("5.0000"), Decimal("30.0000"))
    assert rows["claude-sonnet-5"][2] == Decimal("0.500")  # discount_multiplier default
    assert rows["claude-sonnet-5"][3] == Decimal("0.100")  # cached_in_multiplier default

    # Authoritative drift guard: the fail-soft seed mirror must equal the migration
    # seed (in/out + discount + cached-in) row-for-row, so DB-down costing == live.
    for model, (usd_in, usd_out, disc, cached) in pricing_mod._SEED_PRICING.items():
        assert model in rows, f"seed mirror has {model!r} but the migration seed does not"
        assert rows[model] == (usd_in, usd_out, disc, cached), model


def test_tenant_insert_carries_cost_and_rls_isolates(throwaway_db):
    insert_sql = (
        "INSERT INTO llm_call_events "
        "  (tenant_id, agent, call_site, provider, model, service_tier, "
        "   tokens_in, tokens_out, cost_usd, request_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    cost = compute_cost_usd("claude-sonnet-5", "standard", 1000, 500)
    assert cost == Decimal("0.007")  # (1000*2 + 500*10) / 1e6 (sonnet-5 intro $2/$10)

    with psycopg.connect(throwaway_db, autocommit=True) as admin:
        tenant_a = admin.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Ledger A', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        tenant_b = admin.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Ledger B', 'standard', 'onboarding') RETURNING id"
        ).fetchone()[0]

    # Write + read as the production role (app_role), scoped by the GUC — the exact
    # posture tenant_connection sets up.
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute("SET ROLE app_role")
        _scope(conn, tenant_a)
        conn.execute(
            insert_sql,
            (str(tenant_a), "team_manager", "dispatch_brain", "anthropic",
             "claude-sonnet-5", "standard", 1000, 500, cost, "req_a"),
        )
        row = conn.execute(
            "SELECT tenant_id, cost_usd, tokens_in, tokens_out, provider "
            "FROM llm_call_events WHERE request_id = 'req_a'"
        ).fetchone()
        assert row is not None
        assert row[0] == tenant_a
        assert row[1] == Decimal("0.007000")  # stored at NUMERIC(12,6)
        assert (row[2], row[3], row[4]) == (1000, 500, "anthropic")

        # RLS: tenant B cannot see A's row (neither a blanket SELECT nor a targeted read).
        _scope(conn, tenant_b)
        assert conn.execute("SELECT count(*) FROM llm_call_events").fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM llm_call_events WHERE tenant_id = %s", (tenant_a,)
        ).fetchone()[0] == 0

        # WITH CHECK: scoped to B, inserting A's tenant_id is rejected.
        with pytest.raises(psycopg.Error):
            conn.execute(
                insert_sql,
                (str(tenant_a), "team_manager", "dispatch_brain", "anthropic",
                 "claude-sonnet-5", "standard", 1, 1, cost, "req_evil"),
            )


def test_platform_null_tenant_row_is_service_role_only(throwaway_db):
    insert_sql = (
        "INSERT INTO llm_call_events "
        "  (tenant_id, agent, call_site, provider, model, service_tier, "
        "   tokens_in, tokens_out, cost_usd, request_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    cost = compute_cost_usd("claude-opus-4-8", "standard", 800, 200)

    with psycopg.connect(throwaway_db, autocommit=True) as admin:
        # Platform path: privileged role (BYPASSRLS), NO tenant GUC, NULL tenant_id.
        admin.execute(
            insert_sql,
            (None, "judge", "blind_judge", "anthropic",
             "claude-opus-4-8", "standard", 800, 200, cost, "req_platform"),
        )
        # Service role sees it.
        assert admin.execute(
            "SELECT count(*) FROM llm_call_events WHERE request_id = 'req_platform'"
        ).fetchone()[0] == 1

        tenant = admin.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Ledger P', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]

    # app_role, scoped to a real tenant, cannot see the NULL-tenant platform row.
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute("SET ROLE app_role")
        _scope(conn, tenant)
        assert conn.execute(
            "SELECT count(*) FROM llm_call_events WHERE request_id = 'req_platform'"
        ).fetchone()[0] == 0
