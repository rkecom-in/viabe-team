"""VT-359 — trial-end trial_subscribe_link compose (dormant-gated). Real-PG.

The send STAYS gated at the notify seam; this proves the COMPOSITION (the VT-332 deep-link with a
single-use token + owner_name) is correct when minting is possible, and degrades to None (skip)
when the mint secret is absent.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


@pytest.fixture(scope="module")
def _dbpool():
    db_url = os.environ["DATABASE_URL"]
    import apply_migrations

    assert not apply_migrations.apply(dsn=db_url)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = db_url
    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            db_url, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _tenant(pool) -> str:
    with pool.connection() as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Acme Co', 'founding', 'trial') RETURNING id"
        ).fetchone()["id"])


def test_compose_has_deeplink_and_owner_name(_dbpool, monkeypatch):
    monkeypatch.setenv("OWNER_JWT_SECRET", "vt359-canary-secret")
    monkeypatch.setenv("OWNER_PORTAL_URL", "https://viabe.ai/team")
    from orchestrator.billing.trial_sweep import _compose_trial_subscribe_link

    tid = _tenant(_dbpool)
    params = _compose_trial_subscribe_link(tid)  # type: ignore[arg-type]
    assert params is not None
    assert params["owner_name"] == "Acme Co"
    link = params["subscribe_link"]
    assert link.startswith("https://viabe.ai/team/subscribe?") and "token=" in link
    assert "/team/team/" not in link  # no doubled path


def test_compose_dormant_without_secret(_dbpool, monkeypatch):
    """No OWNER_JWT_SECRET → mint fails → compose returns None (caller skips the send)."""
    monkeypatch.delenv("OWNER_JWT_SECRET", raising=False)
    from orchestrator.billing.trial_sweep import _compose_trial_subscribe_link

    assert _compose_trial_subscribe_link(uuid4()) is None  # type: ignore[arg-type]
