"""VT-289 — OAuth-install state nonce hardening (Rule #15 canary, real Postgres).

Exercises the mint/claim helper against a real DB (no mock cursors). The atomic
single-use claim is the security primitive: a forged/replayed/expired/connector-
mismatched state must NOT yield a tenant, and a valid claim must return the tenant
from the STORED record (never the URL).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-289 oauth_state substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def test_mint_then_claim_returns_tenant_from_record(substrate):
    from orchestrator.integrations.oauth_state import (
        claim_install_state,
        mint_install_state,
    )

    tenant = uuid4()
    state = mint_install_state(tenant, "google_sheet", target="sheet-x")
    claimed = claim_install_state(state, "google_sheet")
    assert claimed is not None
    assert claimed.tenant_id == tenant          # authoritative, from the stored row
    assert claimed.connector_id == "google_sheet"
    assert claimed.target == "sheet-x"


def test_single_use(substrate):
    from orchestrator.integrations.oauth_state import (
        claim_install_state,
        mint_install_state,
    )

    state = mint_install_state(uuid4(), "google_sheet")
    assert claim_install_state(state, "google_sheet") is not None  # first claim ok
    assert claim_install_state(state, "google_sheet") is None      # replay rejected


def test_forged_unknown_state_rejected(substrate):
    from orchestrator.integrations.oauth_state import claim_install_state

    assert claim_install_state("never-minted-this", "google_sheet") is None
    assert claim_install_state("", "google_sheet") is None


def test_expired_state_rejected(substrate):
    from orchestrator.integrations.oauth_state import (
        claim_install_state,
        mint_install_state,
    )

    state = mint_install_state(uuid4(), "google_sheet")
    # force past expiry
    with psycopg.connect(substrate.dsn, autocommit=True) as c:
        c.execute(
            "UPDATE oauth_install_state SET expires_at = now() - interval '1 minute' "
            "WHERE state = %s",
            (state,),
        )
    assert claim_install_state(state, "google_sheet") is None


def test_connector_mismatch_rejected(substrate):
    from orchestrator.integrations.oauth_state import (
        claim_install_state,
        mint_install_state,
    )

    state = mint_install_state(uuid4(), "google_sheet")
    # a shopify callback must NOT be able to claim a google_sheet nonce
    assert claim_install_state(state, "shopify") is None
    # the correct connector still can (claim wasn't consumed by the mismatch)
    assert claim_install_state(state, "google_sheet") is not None


def test_cross_tenant_isolation(substrate):
    from orchestrator.integrations.oauth_state import (
        claim_install_state,
        mint_install_state,
    )

    tenant_a, tenant_b = uuid4(), uuid4()
    state_a = mint_install_state(tenant_a, "shopify")
    state_b = mint_install_state(tenant_b, "shopify")
    assert claim_install_state(state_a, "shopify").tenant_id == tenant_a
    assert claim_install_state(state_b, "shopify").tenant_id == tenant_b


def test_deny_all_rls_blocks_tenant_connection(substrate):
    """oauth_install_state is service-role-only: a tenant_connection (app_role) sees
    nothing (deny-all RLS), proving no tenant client can read/forge nonces."""
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.oauth_state import mint_install_state

    tenant = uuid4()
    mint_install_state(tenant, "google_sheet")
    with tenant_connection(tenant) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM oauth_install_state")
        row = cur.fetchone()
    n = row["n"] if isinstance(row, dict) else row[0]
    assert n == 0  # deny-all: app_role sees zero rows
