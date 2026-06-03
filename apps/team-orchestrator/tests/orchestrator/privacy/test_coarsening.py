"""VT-75 — locality coarsening canary.

Pure-function tier mapping + variants + disambiguation + the locality-drop
guarantee run with no DB. ``set_tenant_city_tier`` (DB) is gated on DATABASE_URL
and asserts ONLY the tier is persisted (raw city discarded). CL-422 synthetic.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("yaml")

from orchestrator.privacy.coarsening import (  # noqa: E402
    coarsen_city,
    coarsen_locality,
)

_TIER1 = ["Mumbai", "Delhi", "Bangalore", "Chennai", "Kolkata", "Hyderabad", "Pune", "Ahmedabad"]


@pytest.mark.parametrize("city", _TIER1)
def test_tier1_metros(city):
    assert coarsen_city(city) == "tier_1"


@pytest.mark.parametrize("city", ["Jaipur", "Surat", "Lucknow", "Indore", "Patna", "Coimbatore"])
def test_tier2_cities(city):
    assert coarsen_city(city) == "tier_2"


@pytest.mark.parametrize("city", ["Karjat", "Palghar", "Some Tiny Village", "Wai"])
def test_unknown_small_towns_default_tier3(city):
    assert coarsen_city(city) == "tier_3"


@pytest.mark.parametrize("variant,expected", [
    ("Bengaluru", "tier_1"), ("Bombay", "tier_1"), ("Calcutta", "tier_1"),
    ("Madras", "tier_1"), ("Vizag", "tier_2"), ("Gurugram", "tier_2"),
])
def test_variants(variant, expected):
    assert coarsen_city(variant) == expected


def test_whitespace_and_case():
    assert coarsen_city("  mUmBaI  ") == "tier_1"


def test_same_name_disambiguation():
    assert coarsen_city("Hyderabad", "Telangana") == "tier_1"   # the Indian metro
    assert coarsen_city("Hyderabad", "Sindh") == "tier_3"       # Pakistan → conservative
    assert coarsen_city("Hyderabad") == "tier_1"                # no state → assume India


def test_locality_is_dropped():
    """coarsen_locality returns the CITY's tier with the locality discarded — the
    locality string never influences or appears in the result."""
    for locality in ("Andheri", "Bandra", "Koramangala", None):
        result = coarsen_locality(locality, "Mumbai")
        assert result == "tier_1"
        if locality:
            assert locality not in result  # locality never in the output


# --- DB: set_tenant_city_tier persists tier-only -----------------------------


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")
def test_set_tenant_city_tier_persists_tier_only():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=2,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    from orchestrator.privacy.coarsening import set_tenant_city_tier

    pool = graph_mod.get_pool()
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, 'coarsen', 'founding', 'onboarding')", (tid,),
        )
    tier = set_tenant_city_tier(tid, "Bengaluru")   # variant → tier_1
    assert tier == "tier_1"
    with pool.connection() as conn:
        row = dict(conn.execute("SELECT * FROM tenants WHERE id = %s", (tid,)).fetchone())
    assert row["city_tier"] == "tier_1"
    # raw city never persisted: no column holds "Bengaluru"/"Bangalore".
    blob = str(row)
    assert "Bengaluru" not in blob and "Bangalore" not in blob
