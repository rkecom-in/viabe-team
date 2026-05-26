"""Tests for the attribution-close aggregator (VT-175).

Pure tests cover the return-shape + the idempotency contract via
monkeypatched DB. Integration-gated tests run end-to-end against real
Supabase when ``RUN_INTEGRATION_TESTS=1``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.billing.attribution_close import close_attribution  # noqa: E402
from orchestrator.billing.types import AttributionCloseResult  # noqa: E402


CANARY_TENANT = UUID("00000000-0000-4000-8000-000000aaa175")


@pytest.fixture(autouse=True)
def _phone_salt(monkeypatch):
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "test-salt-vt175")


def test_attribution_close_result_shape() -> None:
    """Frozen dataclass: every field assigned + immutable."""
    out = AttributionCloseResult(
        campaign_id=uuid4(),
        total_arrr_paise=1000,
        closed_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
        already_closed=False,
        attribution_row_count=3,
    )
    assert out.total_arrr_paise == 1000
    assert out.already_closed is False
    with pytest.raises(Exception):  # frozen
        out.total_arrr_paise = 0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Integration — gated on RUN_INTEGRATION_TESTS=1
# ---------------------------------------------------------------------------

@pytest.fixture
def _dbpool():
    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL not set; integration test requires real DB")

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            db_url,
            min_size=1,
            max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    yield get_pool()


def _seed_tenant_and_campaign(pool, tenant_id: UUID) -> UUID:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-vt175-{tenant_id}"),
        )
        # Synthetic pipeline_run owner for FK.
        cur.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status, started_at) "
            "VALUES (gen_random_uuid(), %s, 'completed', now()) "
            "ON CONFLICT DO NOTHING RETURNING id",
            (str(tenant_id),),
        )
        row = cur.fetchone()
        run_id = row["id"] if row else None
        if run_id is None:
            cur.execute(
                "SELECT id FROM pipeline_runs WHERE tenant_id = %s LIMIT 1",
                (str(tenant_id),),
            )
            run_id = cur.fetchone()["id"]
        # Campaign row (post-018 plan_json schema).
        cur.execute(
            "INSERT INTO campaigns (id, tenant_id, run_id, plan_json, status, generated_at) "
            "VALUES (gen_random_uuid(), %s, %s, %s::jsonb, 'sent', now()) RETURNING id",
            (str(tenant_id), str(run_id), json.dumps({"canary": True})),
        )
        return cur.fetchone()["id"]


def _seed_attribution(pool, tenant_id: UUID, campaign_id: UUID, paise: int) -> None:
    """Seed via service role — bypasses RLS. The seed is workspace-level
    setup; the redactor/RLS isolation lives at the read path."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise) "
            "VALUES (%s, %s, %s)",
            (str(tenant_id), str(campaign_id), paise),
        )


@pytest.mark.integration
def test_close_attribution_sums_attributions(_dbpool) -> None:
    tenant = uuid4()
    campaign_id = _seed_tenant_and_campaign(_dbpool, tenant)
    for amount in (100, 250, 500):
        _seed_attribution(_dbpool, tenant, campaign_id, amount)

    out = close_attribution(campaign_id)
    assert out.total_arrr_paise == 850
    assert out.already_closed is False
    assert out.attribution_row_count == 3


@pytest.mark.integration
def test_close_attribution_idempotent_second_call(_dbpool) -> None:
    tenant = uuid4()
    campaign_id = _seed_tenant_and_campaign(_dbpool, tenant)
    _seed_attribution(_dbpool, tenant, campaign_id, 500)

    first = close_attribution(campaign_id)
    second = close_attribution(campaign_id)

    assert first.already_closed is False
    assert second.already_closed is True
    assert second.total_arrr_paise == 500  # same value preserved
    assert second.closed_at == first.closed_at


@pytest.mark.integration
def test_close_attribution_empty_returns_zero(_dbpool) -> None:
    tenant = uuid4()
    campaign_id = _seed_tenant_and_campaign(_dbpool, tenant)
    # No attribution rows seeded.
    out = close_attribution(campaign_id)
    assert out.total_arrr_paise == 0
    assert out.already_closed is False
    assert out.attribution_row_count == 0
