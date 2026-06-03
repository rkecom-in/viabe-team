"""VT-68/69 — L3 construction + retrieval + quarantine canary (live PG).

Proves the moat-critical guarantees (Cowork 20260604T004000Z):
- Contributor-set k-anon: a cohort with MANY attribute-matchers but <10 actual
  CONTRIBUTORS is REJECTED (the weakening that gating on attributes would cause).
- No PII / cross-tenant-impossibility: a pattern row carries only coarse
  aggregates — no tenant id, customer id, phone, or city.
- 180-day quarantine on retrieval (no override); reconstruction idempotency;
  the Composer reflects priors / the no-prior marker.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

from psycopg.types.json import Jsonb  # noqa: E402 — after importorskip

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — L3 tests skipped",
)

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_PROPOSED = _NOW - timedelta(days=10)          # in the 90d window
_PAST_QUARANTINE = _NOW - timedelta(days=400)  # signed up long ago → contributes


@pytest.fixture(scope="module")
def pool():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt68-salt")

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _new_tenant(pool, bt: str, tier: str, *, signed_up_at: datetime) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, business_type, city_tier, signed_up_at) "
            "VALUES ('l3 test', 'founding', 'paid_active', %s, %s, %s) RETURNING id",
            (bt, tier, signed_up_at),
        ).fetchone()
    return str(row["id"])


def _seed_contributor(pool, bt: str, tier: str, *, recency_days: int, converted: bool,
                      signed_up_at: datetime = _PAST_QUARANTINE) -> str:
    """A tenant that CONTRIBUTES one sent campaign to the cohort: tenant + run +
    campaign(sent) + recipient customer (last_inbound recency) + opt. attribution."""
    tid = _new_tenant(pool, bt, tier, signed_up_at=signed_up_at)
    with pool.connection() as conn:
        run_id = str(uuid4())
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'scheduled_cadence', 'completed')",
            (run_id, tid),
        )
        camp = conn.execute(
            "INSERT INTO campaigns (tenant_id, run_id, status, generated_at, plan_json) "
            "VALUES (%s, %s, 'sent', %s, %s) RETURNING id",
            (tid, run_id, _PROPOSED,
             Jsonb({"message_plan": {"template_id": "tmpl_a"}})),
        ).fetchone()
        cid = str(camp["id"])
        cust = conn.execute(
            "INSERT INTO customers (tenant_id, last_inbound_at) VALUES (%s, %s) RETURNING id",
            (tid, _PROPOSED - timedelta(days=recency_days)),
        ).fetchone()
        cust_id = str(cust["id"])
        conn.execute(
            "INSERT INTO campaign_recipients (campaign_id, customer_id, tenant_id) VALUES (%s, %s, %s)",
            (cid, cust_id, tid),
        )
        if converted:
            conn.execute(
                "INSERT INTO attributions (tenant_id, campaign_id, customer_id, attributed_paise) "
                "VALUES (%s, %s, %s, 50000)",
                (tid, cid, cust_id),
            )
    return tid


def _pattern_rows(pool, cohort_key: str):
    with pool.connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM l3_patterns WHERE pattern_type='cohort_response_rate' AND cohort_key=%s",
            (cohort_key,),
        ).fetchall()]


# --- contributor-set k-anon (the headline guarantee) -------------------------


def test_contributor_gate_rejects_below_k_despite_many_attribute_matchers(pool):
    """50 tenants with the cohort's ATTRIBUTES but only 9 CONTRIBUTORS → the
    cohort is DROPPED. Gating on attribute-matchers would wrongly admit it."""
    from orchestrator.knowledge.l3_construction import construct_l3_patterns

    bt = f"cafe_{uuid4().hex[:8]}"
    # 50 attribute-matchers with NO campaign (not contributors).
    for _ in range(50):
        _new_tenant(pool, bt, "tier_2", signed_up_at=_PAST_QUARANTINE)
    # only 9 actual contributors (recency 75 → 60_90d band).
    for _ in range(9):
        _seed_contributor(pool, bt, "tier_2", recency_days=75, converted=True)

    construct_l3_patterns(now=_NOW)
    assert _pattern_rows(pool, f"{bt}|tier_2|60_90d") == []  # dropped: <10 contributors


def test_contributor_gate_admits_at_10(pool):
    from orchestrator.knowledge.l3_construction import construct_l3_patterns

    bt = f"cafe_{uuid4().hex[:8]}"
    for i in range(10):
        _seed_contributor(pool, bt, "tier_2", recency_days=75, converted=(i < 4))

    construct_l3_patterns(now=_NOW)
    rows = _pattern_rows(pool, f"{bt}|tier_2|60_90d")
    assert len(rows) == 1
    assert rows[0]["n_tenants"] == 10
    assert rows[0]["metrics"]["response_rate"] == 0.4  # 4/10 converted


# --- no PII / cross-tenant impossibility -------------------------------------


def test_pattern_rows_carry_no_pii(pool):
    from orchestrator.knowledge.l3_construction import construct_l3_patterns

    bt = f"cafe_{uuid4().hex[:8]}"
    seeded = [_seed_contributor(pool, bt, "tier_2", recency_days=75, converted=True) for _ in range(10)]
    construct_l3_patterns(now=_NOW)
    with pool.connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, pattern_type, cohort_key, n_tenants, n_campaigns, metrics, confidence_band "
            "FROM l3_patterns"
        ).fetchall()]
    blob = str(rows)
    # No contributing tenant id can appear in any pattern row.
    for tid in seeded:
        assert tid not in blob
    # No phone-shaped digit runs; cohort_key is coarse (no raw city).
    assert not re.search(r"\b\d{10}\b", blob)
    assert "tier_2" in blob and "Mumbai" not in blob


# --- 180-day quarantine (retrieval) ------------------------------------------


def test_quarantine_blocks_young_tenant(pool):
    from orchestrator.knowledge.l3_construction import construct_l3_patterns
    from orchestrator.knowledge.l3_query import lookup_pattern

    bt = f"cafe_{uuid4().hex[:8]}"
    for _ in range(10):
        _seed_contributor(pool, bt, "tier_2", recency_days=75, converted=True)
    construct_l3_patterns(now=_NOW)
    ckey = f"{bt}|tier_2|60_90d"

    young = _new_tenant(pool, bt, "tier_2", signed_up_at=_NOW - timedelta(days=179))
    assert lookup_pattern(young, "cohort_response_rate", ckey, now=_NOW) is None  # quarantined
    old = _new_tenant(pool, bt, "tier_2", signed_up_at=_NOW - timedelta(days=180))
    assert lookup_pattern(old, "cohort_response_rate", ckey, now=_NOW) is not None  # 180d eligible


# --- reconstruction idempotency ----------------------------------------------


def test_reconstruction_is_idempotent(pool):
    from orchestrator.knowledge.l3_construction import construct_l3_patterns

    bt = f"cafe_{uuid4().hex[:8]}"
    for i in range(11):
        _seed_contributor(pool, bt, "tier_2", recency_days=75, converted=(i < 5))
    construct_l3_patterns(now=_NOW)
    first = _pattern_rows(pool, f"{bt}|tier_2|60_90d")[0]
    construct_l3_patterns(now=_NOW)  # full rebuild
    second = _pattern_rows(pool, f"{bt}|tier_2|60_90d")[0]
    assert first["n_tenants"] == second["n_tenants"]
    assert first["metrics"] == second["metrics"]


# --- Composer wire reflects priors / no-prior marker -------------------------


def test_composer_reflects_prior_and_no_prior(pool):
    from orchestrator.context_builder import _build_l3_priors
    from orchestrator.knowledge.l3_construction import construct_l3_patterns

    bt = f"cafe_{uuid4().hex[:8]}"
    contributors = [_seed_contributor(pool, bt, "tier_2", recency_days=75, converted=True) for _ in range(10)]
    construct_l3_patterns(now=_NOW)

    # An eligible (past-quarantine) tenant in the segment sees the prior.
    eligible = contributors[0]
    priors, ok = _build_l3_priors(UUID(eligible), uuid4())
    assert ok is True
    assert priors.available is True
    assert any(p["cohort_key"] == f"{bt}|tier_2|60_90d" for p in priors.patterns)

    # A quarantined tenant in the same segment gets the structured no-prior marker.
    young = _new_tenant(pool, bt, "tier_2", signed_up_at=_NOW - timedelta(days=30))
    priors2, ok2 = _build_l3_priors(UUID(young), uuid4())
    assert ok2 is False
    assert priors2.available is False
    assert "no L3 prior" in priors2.note
