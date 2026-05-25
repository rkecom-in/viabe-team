"""Tests for the cost dashboard module (VT-103).

Pure unit tests (formatter, plan-price lookup, bucket helper, env overrides,
model_pricing.yaml shape) run unconditionally. Database-backed integration
tests are marked with ``@pytest.mark.integration`` so they skip unless
``RUN_INTEGRATION_TESTS=1`` is set.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("yaml")

import yaml  # noqa: E402

from orchestrator.observability import cost_dashboard as cd  # noqa: E402
from orchestrator.observability.types import (  # noqa: E402
    TenantCostBreakdown,
)


CANARY_TENANT_A = UUID("00000000-0000-4000-8000-000000aaa103")
CANARY_TENANT_B = UUID("00000000-0000-4000-8000-000000bbb103")
CANARY_TENANT_C = UUID("00000000-0000-4000-8000-000000ccc103")
PRICING_YAML = (
    Path(__file__).resolve().parents[3] / "config" / "model_pricing.yaml"
)


# ---------------------------------------------------------------------------
# Pure — model_pricing.yaml shape + parse
# ---------------------------------------------------------------------------

def test_model_pricing_yaml_loads_and_has_required_top_level_keys() -> None:
    data = yaml.safe_load(PRICING_YAML.read_text())
    assert isinstance(data, dict)
    for key in ("effective_at", "llm", "twilio", "razorpay", "apify"):
        assert key in data, f"missing top-level pricing key: {key}"


def test_model_pricing_yaml_has_anthropic_models_with_input_and_output_rates() -> None:
    data = yaml.safe_load(PRICING_YAML.read_text())
    anthropic = data["llm"]["anthropic"]
    assert anthropic, "no anthropic models declared"
    for model, block in anthropic.items():
        assert "input_per_1m_paise" in block, f"{model} missing input rate"
        assert "output_per_1m_paise" in block, f"{model} missing output rate"
        assert isinstance(block["input_per_1m_paise"], int)
        assert isinstance(block["output_per_1m_paise"], int)


def test_model_pricing_yaml_razorpay_mdr_basis_points_is_positive_int() -> None:
    data = yaml.safe_load(PRICING_YAML.read_text())
    assert isinstance(data["razorpay"]["mdr_basis_points"], int)
    assert data["razorpay"]["mdr_basis_points"] > 0


# ---------------------------------------------------------------------------
# Pure — plan-price lookup + env override
# ---------------------------------------------------------------------------

def test_plan_price_paise_defaults_for_known_tiers() -> None:
    assert cd._plan_price_paise("founding") == 249_900
    assert cd._plan_price_paise("standard") == 499_900
    assert cd._plan_price_paise("pro") == 1_499_900


def test_plan_price_paise_env_override(monkeypatch) -> None:
    monkeypatch.setenv("STANDARD_PRICE_PAISE", "123456")
    assert cd._plan_price_paise("standard") == 123_456


def test_plan_price_paise_unknown_tier_returns_zero() -> None:
    assert cd._plan_price_paise("nonexistent_tier_xyz") == 0


# ---------------------------------------------------------------------------
# Pure — unknown-bucket fallback
# ---------------------------------------------------------------------------

def test_bucket_for_unknown_maps_substring_match() -> None:
    assert cd._bucket_for_unknown("anthropic_llm_v3") == "llm"
    assert cd._bucket_for_unknown("twilio_outbound") == "twilio"


def test_bucket_for_unknown_returns_input_when_no_match() -> None:
    assert cd._bucket_for_unknown("unknown_vendor_zzz") == "unknown_vendor_zzz"


# ---------------------------------------------------------------------------
# Pure — formatter
# ---------------------------------------------------------------------------

def test_format_cost_breakdown_for_ops_renders_markdown_block() -> None:
    since = datetime(2026, 5, 1, tzinfo=timezone.utc)
    until = datetime(2026, 5, 8, tzinfo=timezone.utc)
    out = cd.format_cost_breakdown_for_ops(
        TenantCostBreakdown(
            tenant_id=CANARY_TENANT_A,
            since=since,
            until=until,
            total_paise=12345,
            by_category={"llm": 10000, "twilio": 2345},
            event_count=7,
        )
    )
    assert str(CANARY_TENANT_A) in out
    assert "₹123.45" in out
    assert "llm" in out and "twilio" in out
    assert "7 events" in out


# ---------------------------------------------------------------------------
# Pure — anomaly + runaway threshold validation
# ---------------------------------------------------------------------------

def test_detect_cost_anomalies_rejects_bad_window() -> None:
    with pytest.raises(ValueError):
        cd.detect_cost_anomalies(reference_days=5, window_days=7)


def test_detect_cost_anomalies_rejects_zero_multiplier() -> None:
    with pytest.raises(ValueError):
        cd.detect_cost_anomalies(multiplier=0)


def test_runaway_alert_candidates_rejects_zero_threshold() -> None:
    with pytest.raises(ValueError):
        cd.runaway_alert_candidates(plan_pct_threshold=0)


def test_anomaly_min_window_paise_constant_defaults_to_10000() -> None:
    # Cowork condition #2 — the floor is ₹100 (10000 paise) by default.
    # We re-import to read the module-level constant rather than relying on
    # whatever monkeypatched env the previous test left behind.
    import importlib

    fresh = importlib.reload(cd)
    assert fresh._ANOMALY_MIN_WINDOW_PAISE == 10_000
    assert fresh._RUNAWAY_MIN_WINDOW_PAISE == 10_000


# ---------------------------------------------------------------------------
# Integration — DB-backed (gated)
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


def _seed_tenant(pool, tenant_id: UUID, plan_tier: str = "standard") -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, %s, 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-cost-{tenant_id}", plan_tier),
        )


def _seed_cost_event(
    pool,
    tenant_id: UUID,
    run_id: UUID,
    cost_paise: int,
    category: str = "llm",
    vendor: str = "anthropic",
    when: datetime | None = None,
) -> None:
    payload = {
        "vendor": vendor,
        "endpoint": "/v1/messages",
        "cost_paise": cost_paise,
        "cost_category": category,
    }
    ts = when or datetime.now(timezone.utc)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_log "
            "(run_id, tenant_id, event_type, severity, component, payload, created_at) "
            "VALUES (%s, %s, 'external_api_call', 'info', 'canary', %s::jsonb, %s)",
            (str(run_id), str(tenant_id), json.dumps(payload), ts),
        )


@pytest.mark.integration
def test_get_tenant_cost_aggregates_and_buckets(_dbpool) -> None:
    _seed_tenant(_dbpool, CANARY_TENANT_A)
    run_id = uuid4()
    for _ in range(3):
        _seed_cost_event(_dbpool, CANARY_TENANT_A, run_id, 100, "llm")
    for _ in range(2):
        _seed_cost_event(_dbpool, CANARY_TENANT_A, run_id, 50, "twilio")

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    until = datetime.now(timezone.utc) + timedelta(hours=1)
    out = cd.get_tenant_cost(CANARY_TENANT_A, since, until)
    assert out.total_paise == 3 * 100 + 2 * 50
    assert out.by_category["llm"] == 300
    assert out.by_category["twilio"] == 100
    assert out.event_count == 5


@pytest.mark.integration
def test_get_tenant_cost_cross_tenant_isolation(_dbpool) -> None:
    _seed_tenant(_dbpool, CANARY_TENANT_A)
    _seed_tenant(_dbpool, CANARY_TENANT_B)
    run_a, run_b = uuid4(), uuid4()
    _seed_cost_event(_dbpool, CANARY_TENANT_A, run_a, 500)
    _seed_cost_event(_dbpool, CANARY_TENANT_B, run_b, 9999)

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    until = datetime.now(timezone.utc) + timedelta(hours=1)
    a = cd.get_tenant_cost(CANARY_TENANT_A, since, until)
    assert a.total_paise == 500  # not inflated by B


@pytest.mark.integration
def test_get_workspace_cost_summary_top_n_ranking(_dbpool) -> None:
    # Three tenants with distinct totals.
    _seed_tenant(_dbpool, CANARY_TENANT_A)
    _seed_tenant(_dbpool, CANARY_TENANT_B)
    _seed_tenant(_dbpool, CANARY_TENANT_C)
    _seed_cost_event(_dbpool, CANARY_TENANT_A, uuid4(), 200)
    _seed_cost_event(_dbpool, CANARY_TENANT_B, uuid4(), 800)
    _seed_cost_event(_dbpool, CANARY_TENANT_C, uuid4(), 400)
    # Refresh the materialised view so the day-bucketed query sees the rows.
    with _dbpool.connection() as conn, conn.cursor() as cur:
        cur.execute("REFRESH MATERIALIZED VIEW tenant_cost_daily")

    since = datetime.now(timezone.utc) - timedelta(days=1)
    until = datetime.now(timezone.utc) + timedelta(days=1)
    summary = cd.get_workspace_cost_summary(since, until, top_n=10)
    ranked = {tid: paise for tid, paise in summary.top_tenants}
    # Order: B > C > A.
    keys_in_order = [tid for tid, _ in summary.top_tenants]
    assert keys_in_order.index(CANARY_TENANT_B) < keys_in_order.index(CANARY_TENANT_C)
    assert keys_in_order.index(CANARY_TENANT_C) < keys_in_order.index(CANARY_TENANT_A)
    assert ranked[CANARY_TENANT_B] >= 800


@pytest.mark.integration
def test_get_tenant_unit_economics_pro_rates_to_window(_dbpool, monkeypatch) -> None:
    monkeypatch.setenv("STANDARD_PRICE_PAISE", "100000")  # ₹1000/month
    _seed_tenant(_dbpool, CANARY_TENANT_A, plan_tier="standard")
    _seed_cost_event(_dbpool, CANARY_TENANT_A, uuid4(), 25000)  # ₹250 cost

    since = datetime.now(timezone.utc) - timedelta(days=30)
    until = datetime.now(timezone.utc)
    ue = cd.get_tenant_unit_economics(CANARY_TENANT_A, since, until)
    # arrr_paise pro-rated to 30 days = 100000 paise, cost = 25000 paise → ratio = 4.0
    assert math.isclose(ue.ratio, 4.0, rel_tol=0.05)
