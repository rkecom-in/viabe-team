"""VT-74 — k-anonymity admission gate canary + substrate tests (live PG).

Proves the build-time invariant (Pillar 6), the predicate allowlist + quarantine,
the k_min≥10 locked floor (CL-28), and the Cowork-mandated guardrails: the gate
returns tenant UUIDs + a count ONLY (no customer PII), never logs
eligible_tenant_ids, and never trips VT-79 Detector-1 (tenant_isolation_breach).
"""

from __future__ import annotations

import dataclasses
import os
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — k-anonymity tests skipped",
)

_CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)
_BEFORE = _CUTOFF - timedelta(days=30)   # signed up before the quarantine boundary
_AFTER = _CUTOFF + timedelta(days=30)    # still quarantined (signed up too recently)


@pytest.fixture(scope="module")
def pool():
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


def _unique_bt() -> str:
    """A per-test business_type so each test's predicate matches only its own
    seeded tenants (no cross-test interference on the shared `tenants` table)."""
    return f"bt_{uuid4().hex[:10]}"


def _seed(pool, business_type: str, n: int, *, signed_up_at: datetime, city_tier: str = "tier_2") -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, business_type, city_tier, signed_up_at) "
            "SELECT 'k-anon test', 'founding', 'onboarding', %s, %s, %s "
            "FROM generate_series(1, %s)",
            (business_type, city_tier, signed_up_at, n),
        )


def _predicate(business_type: str, **over):
    from orchestrator.privacy.k_anonymity import CohortPredicate

    base = {
        "business_type": business_type, "city_tier": "tier_2",
        "recency_band": "60_90d_dormant", "signed_up_before": _CUTOFF,
    }
    base.update(over)
    return CohortPredicate(**base)


# --- k_min boundary + below/above --------------------------------------------


def test_below_k_min_rejected(pool):
    from orchestrator.privacy.k_anonymity import check_admission

    bt = _unique_bt()
    _seed(pool, bt, 9, signed_up_at=_BEFORE)
    res = check_admission(_predicate(bt))
    assert res.admitted is False
    assert res.reason == "below_k_min"
    assert res.tenant_count == 9
    assert res.eligible_tenant_ids == []  # never leak ids on a reject


def test_boundary_exactly_10_admitted(pool):
    from orchestrator.privacy.k_anonymity import check_admission

    bt = _unique_bt()
    _seed(pool, bt, 10, signed_up_at=_BEFORE)
    res = check_admission(_predicate(bt))
    assert res.admitted is True
    assert res.reason == "admitted"
    assert res.tenant_count == 10
    assert len(res.eligible_tenant_ids) == 10


def test_admitted_15(pool):
    from orchestrator.privacy.k_anonymity import check_admission

    bt = _unique_bt()
    _seed(pool, bt, 15, signed_up_at=_BEFORE)
    res = check_admission(_predicate(bt))
    assert res.admitted is True
    assert res.tenant_count == 15
    assert len(res.eligible_tenant_ids) == 15


# --- predicate validation ----------------------------------------------------


def test_missing_signed_up_before_is_predicate_invalid(pool):
    from orchestrator.privacy.k_anonymity import check_admission

    res = check_admission({
        "business_type": "cafe", "city_tier": "tier_2", "recency_band": "60_90d",
    })  # no signed_up_before
    assert res.reason == "predicate_invalid"
    assert res.admitted is False
    assert res.eligible_tenant_ids == []


def test_forbidden_field_is_predicate_invalid(pool):
    from orchestrator.privacy.k_anonymity import check_admission

    res = check_admission({
        "business_type": "cafe", "city_tier": "tier_2", "recency_band": "60_90d",
        "signed_up_before": _CUTOFF, "city": "Mumbai",  # forbidden — defeats anonymity
    })
    assert res.reason == "predicate_invalid"
    # No customer/tenant data ever returned on an invalid predicate.
    assert res.eligible_tenant_ids == []


def test_k_min_below_floor_asserts(pool):
    from orchestrator.privacy.k_anonymity import check_admission

    with pytest.raises(AssertionError):
        check_admission(_predicate(_unique_bt()), k_min=5)  # CL-28 floor is 10


# --- quarantine (signed_up_before) -------------------------------------------


def test_quarantine_excludes_too_recent(pool):
    from orchestrator.privacy.k_anonymity import check_admission

    bt = _unique_bt()
    _seed(pool, bt, 10, signed_up_at=_BEFORE)   # past the quarantine → counted
    _seed(pool, bt, 3, signed_up_at=_AFTER)     # too recent → excluded
    res = check_admission(_predicate(bt))
    assert res.tenant_count == 10  # only the 10 past the boundary
    assert res.admitted is True


# --- guardrails: no PII leak / no id logging / no isolation-breach -----------


def test_result_carries_only_sanctioned_fields(pool):
    from orchestrator.privacy.k_anonymity import AdmissionResult, check_admission

    bt = _unique_bt()
    _seed(pool, bt, 10, signed_up_at=_BEFORE)
    res = check_admission(_predicate(bt))
    # The result type structurally cannot carry customer PII / tenant business data.
    names = {f.name for f in dataclasses.fields(AdmissionResult)}
    assert names == {"admitted", "tenant_count", "reason", "eligible_tenant_ids"}
    # eligible_tenant_ids are UUIDs only, not rows/dicts.
    assert all(hasattr(x, "hex") for x in res.eligible_tenant_ids)


def test_audit_logged_without_tenant_ids(pool):
    from orchestrator.privacy.k_anonymity import check_admission

    bt = _unique_bt()
    _seed(pool, bt, 11, signed_up_at=_BEFORE)
    check_admission(_predicate(bt))
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT payload FROM pipeline_log "
            "WHERE event_type = 'k_anonymity_check' AND tenant_id IS NULL "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchall()
    assert rows, "k_anonymity_check audit row not written"
    payload = dict(rows[0])["payload"]
    assert set(payload) <= {"predicate_hash", "k_min", "tenant_count", "admitted"}
    assert "eligible_tenant_ids" not in payload  # CL-390 — ids never logged
    blob = str(payload)
    assert "eligible" not in blob


def test_no_tenant_isolation_breach_emitted(pool):
    """VT-79 Detector-1 must NOT fire: the gate is sanctioned cross-tenant, so it
    never calls assert_tenant_scoped → no tenant_isolation_breach step."""
    from orchestrator.privacy.k_anonymity import check_admission

    bt = _unique_bt()
    _seed(pool, bt, 12, signed_up_at=_BEFORE)
    check_admission(_predicate(bt))
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM pipeline_steps "
            "WHERE step_kind = 'tenant_isolation_breach'"
        ).fetchone()
    assert int(dict(row)["n"]) == 0


# --- performance -------------------------------------------------------------


def test_10k_tenant_query_is_fast(pool):
    from orchestrator.privacy.k_anonymity import check_admission

    bt = _unique_bt()
    _seed(pool, bt, 10_000, signed_up_at=_BEFORE)
    t0 = time.monotonic()
    res = check_admission(_predicate(bt))
    elapsed = time.monotonic() - t0
    assert res.admitted is True
    assert res.tenant_count == 10_000  # at the cap
    assert elapsed < 2.0, f"k-anon 10k query took {elapsed:.3f}s (spec target <1s)"
