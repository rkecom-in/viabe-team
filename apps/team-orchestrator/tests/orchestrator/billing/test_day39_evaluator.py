"""Tests for the day-39 evaluator (VT-175).

Pure tests cover the verdict dataclass + the rule constant. Integration-
gated tests exercise both branches against real Supabase.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.billing.day39_evaluator import (  # noqa: E402
    DAY39_WINDOW,
    FEES_PER_ARRR_MULTIPLIER,
    evaluate_day39,
)
from orchestrator.billing.types import Day39Verdict  # noqa: E402


@pytest.fixture(autouse=True)
def _phone_salt(monkeypatch):
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "test-salt-vt175")


def test_day39_window_is_39_days() -> None:
    assert DAY39_WINDOW == timedelta(days=39)


def test_multiplier_is_two() -> None:
    assert FEES_PER_ARRR_MULTIPLIER == 2


def test_day39_verdict_dataclass_immutable() -> None:
    v = Day39Verdict(
        tenant_id=uuid4(),
        verdict="continue",
        arrr_paise=2000,
        cumulative_fees_paise=500,
        decided_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
    )
    assert v.verdict == "continue"
    with pytest.raises(Exception):  # frozen
        v.verdict = "refund_triggered"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Integration tests — gated on RUN_INTEGRATION_TESTS=1
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


def _seed_tenant(pool, tenant_id: UUID, *, paid_days_ago: int) -> None:
    paid_at = datetime.now(timezone.utc) - timedelta(days=paid_days_ago)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, paid_conversion_at) "
            "VALUES (%s, %s, 'standard', 'paid_active', %s) ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-day39-{tenant_id}", paid_at),
        )


def _seed_subscription(pool, tenant_id: UUID, fees_paise: int) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO subscriptions (tenant_id, status, started_at, cumulative_fees_paid_paise) "
            "VALUES (%s, 'active', now() - interval '40 days', %s)",
            (str(tenant_id), fees_paise),
        )


def _seed_attribution(pool, tenant_id: UUID, arrr_paise: int) -> None:
    """Seed via service role — bypasses RLS (no GUC needed for the seed)."""
    with pool.connection() as conn, conn.cursor() as cur:
        # Need a campaign FK; use a synthetic one.
        cur.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status, started_at) "
            "VALUES (gen_random_uuid(), %s, 'completed', now() - interval '40 days') "
            "RETURNING id",
            (str(tenant_id),),
        )
        run_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO campaigns (id, tenant_id, run_id, plan_json, status, generated_at) "
            "VALUES (gen_random_uuid(), %s, %s, %s::jsonb, 'sent', now() - interval '20 days') "
            "RETURNING id",
            (str(tenant_id), str(run_id), json.dumps({"canary": True})),
        )
        campaign_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise, attribution_at) "
            "VALUES (%s, %s, %s, now() - interval '20 days')",
            (str(tenant_id), str(campaign_id), arrr_paise),
        )


@pytest.mark.integration
def test_day39_continue_branch(_dbpool) -> None:
    tenant = uuid4()
    _seed_tenant(_dbpool, tenant, paid_days_ago=40)
    _seed_subscription(_dbpool, tenant, fees_paise=500)
    _seed_attribution(_dbpool, tenant, arrr_paise=2000)  # 2000 >= 2*500

    out = evaluate_day39(tenant)
    assert out.verdict == "continue"
    assert out.arrr_paise == 2000
    assert out.cumulative_fees_paise == 500
    assert out.already_decided is False


@pytest.mark.integration
def test_day39_refund_triggered_branch(_dbpool) -> None:
    tenant = uuid4()
    _seed_tenant(_dbpool, tenant, paid_days_ago=40)
    _seed_subscription(_dbpool, tenant, fees_paise=500)
    _seed_attribution(_dbpool, tenant, arrr_paise=100)  # 100 < 2*500

    out = evaluate_day39(tenant)
    assert out.verdict == "refund_triggered"
    assert out.arrr_paise == 100
    assert out.cumulative_fees_paise == 500


@pytest.mark.integration
def test_day39_not_eligible_when_window_unmet(_dbpool) -> None:
    tenant = uuid4()
    _seed_tenant(_dbpool, tenant, paid_days_ago=10)  # < 39 days
    out = evaluate_day39(tenant)
    assert out.verdict == "not_eligible"


@pytest.mark.integration
def test_day39_idempotency_replays_prior_verdict(_dbpool) -> None:
    tenant = uuid4()
    _seed_tenant(_dbpool, tenant, paid_days_ago=40)
    _seed_subscription(_dbpool, tenant, fees_paise=500)
    _seed_attribution(_dbpool, tenant, arrr_paise=2000)

    first = evaluate_day39(tenant)
    import time

    time.sleep(0.5)  # let log_event's daemon thread flush.
    second = evaluate_day39(tenant)

    assert first.verdict == "continue"
    assert second.verdict == "continue"
    assert second.already_decided is True
