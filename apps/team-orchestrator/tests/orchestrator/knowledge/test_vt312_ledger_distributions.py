"""VT-312 canary — ``_build_ledger_summary`` brain-decides distributions.

Live Postgres via DATABASE_URL (CL-422 synthetic; gate + skip when unset, like
the other substrate suites). Seeds a synthetic tenant + several customers with
varied ``last_inbound_at`` and ``customer_ledger_entries`` ('sale') amounts, then
calls the REAL ``_build_ledger_summary`` and asserts:

  * recency_days_pctl p50 ≈ the synthetic recency median,
  * spend_paise_pctl  p50 ≈ the synthetic per-customer spend median,
  * business_type is surfaced from the tenants row,
  * total_customers is correct,
  * it returns cleanly with EMPTY pctl maps when there are zero customers /
    ledger rows (NO threshold-event dependency — the old L2 coupling is gone).

This is the VT-312 acceptance canary: a real per-tenant SQL read through
``tenant_connection`` (RLS), not a monkeypatched stub.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("pydantic")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-312 ledger-distribution canary skipped",
)


@pytest.fixture(scope="module")
def pool():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _tenant(pool, *, business_type: str = "cafe") -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, business_type, plan_tier, phase) "
            "VALUES (%s, 'vt312 canary', %s, 'founding', 'paid_active')",
            (tid, business_type),
        )
    return tid


def _customer(pool, tid: str, *, days_ago: int | None) -> str:
    """Seed one customer; ``days_ago`` sets last_inbound_at = now - days_ago
    (None → NULL, excluded from the recency percentile by the WHERE clause)."""
    cid = str(uuid4())
    with pool.connection() as conn:
        if days_ago is None:
            conn.execute(
                "INSERT INTO customers (id, tenant_id, last_inbound_at) "
                "VALUES (%s, %s, NULL)",
                (cid, tid),
            )
        else:
            conn.execute(
                "INSERT INTO customers (id, tenant_id, last_inbound_at) "
                "VALUES (%s, %s, (now() - make_interval(days => %s)))",
                (cid, tid, days_ago),
            )
    return cid


def _sale(pool, tid: str, cid: str, amount_paise: int) -> None:
    """Seed one 'sale' ledger entry for a customer (idempotency key unique)."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO customer_ledger_entries "
            "(tenant_id, customer_id, amount_paise, entry_type, entry_date, "
            " acquired_via, source_confidence, entry_key) "
            "VALUES (%s, %s, %s, 'sale', now()::date, 'manual_entry', 1.0, %s)",
            (tid, cid, amount_paise, uuid4().hex),
        )


# --- the canary --------------------------------------------------------------


def test_ledger_summary_surfaces_raw_distributions(pool):
    """Populated tenant → percentile distributions + business_type + count."""
    from orchestrator.context_builder import _build_ledger_summary

    tid = _tenant(pool, business_type="bakery")

    # 5 customers, recency days-since-last-inbound = {5, 15, 30, 60, 95}.
    # percentile_cont(0.5) over a 5-element sorted set = the 3rd value = 30.
    recencies = [5, 15, 30, 60, 95]
    spends = [10_000, 25_000, 50_000, 120_000, 400_000]
    # per-customer spend totals = {10k, 25k, 50k, 120k, 400k}; p50 = 50_000.
    for days, amt in zip(recencies, spends, strict=True):
        cid = _customer(pool, tid, days_ago=days)
        _sale(pool, tid, cid, amt)

    summary, ok = _build_ledger_summary(UUID(tid))

    assert ok is True  # raw read always available
    assert summary.total_customers == 5
    assert summary.business_type == "bakery"

    # p50 lands on the synthetic median exactly (odd count, no interpolation).
    assert summary.recency_days_pctl["p50"] == 30
    assert summary.spend_paise_pctl["p50"] == 50_000
    # Distribution shape: keys present + monotonic non-decreasing.
    assert set(summary.recency_days_pctl) == {"p25", "p50", "p75", "p90"}
    assert set(summary.spend_paise_pctl) == {"p25", "p50", "p75", "p90"}
    r = summary.recency_days_pctl
    assert r["p25"] <= r["p50"] <= r["p75"] <= r["p90"]
    s = summary.spend_paise_pctl
    assert s["p25"] <= s["p50"] <= s["p75"] <= s["p90"]


def test_ledger_summary_empty_tenant_returns_empty_maps(pool):
    """Zero customers / zero ledger rows → clean return, EMPTY pctl maps, no
    threshold-event dependency. business_type still surfaces; count is 0."""
    from orchestrator.context_builder import _build_ledger_summary

    tid = _tenant(pool, business_type="salon")
    summary, ok = _build_ledger_summary(UUID(tid))

    assert ok is True
    assert summary.total_customers == 0
    assert summary.business_type == "salon"
    assert summary.recency_days_pctl == {}
    assert summary.spend_paise_pctl == {}


def test_ledger_summary_customers_without_sales_have_empty_spend(pool):
    """Customers exist (recency populates) but no 'sale' ledger rows → recency
    pctl present, spend pctl empty. Proves the two distributions are independent
    reads (no cross-contamination)."""
    from orchestrator.context_builder import _build_ledger_summary

    tid = _tenant(pool, business_type="cafe")
    for days in (10, 20, 40):
        _customer(pool, tid, days_ago=days)

    summary, ok = _build_ledger_summary(UUID(tid))

    assert ok is True
    assert summary.total_customers == 3
    assert summary.recency_days_pctl["p50"] == 20  # 3-element median
    assert summary.spend_paise_pctl == {}  # no sales → empty spend distribution


def test_ledger_summary_null_recency_excluded_from_recency_pctl(pool):
    """Customers with NULL last_inbound_at are counted in total_customers but
    excluded from the recency percentile (WHERE last_inbound_at IS NOT NULL)."""
    from orchestrator.context_builder import _build_ledger_summary

    tid = _tenant(pool, business_type="cafe")
    # 2 with recency, 1 NULL → total=3, recency p50 over {12, 24} interpolates 18.
    _customer(pool, tid, days_ago=12)
    _customer(pool, tid, days_ago=24)
    _customer(pool, tid, days_ago=None)

    summary, ok = _build_ledger_summary(UUID(tid))

    assert ok is True
    assert summary.total_customers == 3
    # percentile_cont(0.5) over {12, 24} = 18 (linear interpolation, rounded).
    assert summary.recency_days_pctl["p50"] == 18
