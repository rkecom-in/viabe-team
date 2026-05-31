"""PR-fix-7 (CL-71) — proof that tenant_connection enforces FORCE RLS.

The whole orchestrator suite passes today only because CI connects as a
Postgres superuser, which bypasses FORCE ROW LEVEL SECURITY. tenant_connection
does SET ROLE app_role — a non-superuser, no-BYPASSRLS role — so RLS is
genuinely enforced inside the wrapper. These tests would false-pass if that
SET ROLE were removed (superuser bypass); they are the real proof surface.

Require a live Postgres via DATABASE_URL + the dbos stack; run in the CI
``orchestrator`` job.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402 — imported after the dependency skip guard

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — tenant isolation tests skipped",
)


@pytest.fixture(scope="module")
def rls_ctx():
    """Apply migrations (incl. 015 app_role) + launch DBOS so the pool exists."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str) -> str:
    """Seed a tenant via a direct superuser connection (RLS bypassed)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('PR-fix-7 RLS Test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_tenant_connection_blocks_cross_tenant_read(rls_ctx):
    """A row written under tenant A is invisible to tenant B's wrapper."""
    from orchestrator.db import tenant_connection

    tenant_a = _new_tenant(rls_ctx.dsn)
    tenant_b = _new_tenant(rls_ctx.dsn)
    run_id = str(uuid4())

    with tenant_connection(tenant_a) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running')",
            (run_id, tenant_a),
        )

    with tenant_connection(tenant_a) as conn:
        seen_a = conn.execute(
            "SELECT count(*) AS n FROM pipeline_runs WHERE id = %s", (run_id,)
        ).fetchone()["n"]
    assert seen_a == 1, "tenant A cannot see its own row — RLS / GUC misconfigured"

    with tenant_connection(tenant_b) as conn:
        seen_b = conn.execute(
            "SELECT count(*) AS n FROM pipeline_runs WHERE id = %s", (run_id,)
        ).fetchone()["n"]
    assert seen_b == 0, "RLS leak: tenant B saw tenant A's pipeline_runs row"


def test_tenant_connection_blocks_cross_tenant_write(rls_ctx):
    """An INSERT naming another tenant is rejected by the RLS WITH CHECK clause."""
    from orchestrator.db import tenant_connection

    tenant_a = _new_tenant(rls_ctx.dsn)
    tenant_b = _new_tenant(rls_ctx.dsn)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tenant_connection(tenant_a) as conn:
            # Scoped to A, but the row claims tenant B — WITH CHECK rejects it.
            conn.execute(
                "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
                "VALUES (%s, %s, 'orchestrator', 'running')",
                (str(uuid4()), tenant_b),
            )
