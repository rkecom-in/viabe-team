"""VT-94 — founding-tier counter. Real-PG canary.

Keystone: 50 concurrent claims at claimed_count=99 → EXACTLY ONE wins, final count == 100
(never 101) — the atomic UPDATE + row-lock race-safety. Plus: cap gate, one-slot-per-tenant
(no double-count), audit-only release (no decrement), in-txn atomicity (rolled-back signup
leaks no slot), DSR hard-delete, and the cached public endpoint.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.billing.founding_counter import (  # noqa: E402
    get_founding_status,
    release_founding_slot,
    try_claim_founding_slot,
)


@pytest.fixture
def _dbpool():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL not set; integration test requires real DB")
    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            db_url, min_size=2, max_size=20,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return get_pool()


def _reset(pool, count: int = 0) -> None:
    with pool.connection() as conn:
        conn.execute("DELETE FROM founding_tier_claims")
        conn.execute("UPDATE founding_tier_counter SET claimed_count=%s WHERE id=1", (count,))


def _seed_tenant(pool, tid: UUID) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tid), f"vt94-{tid}"),
        )


def _claim_in_txn(pool, tid: UUID):
    with pool.connection() as conn, conn.transaction():
        return try_claim_founding_slot(conn, tid)


@pytest.mark.integration
def test_claim_then_cap(_dbpool) -> None:
    _reset(_dbpool, 99)
    a, b = uuid4(), uuid4()
    _seed_tenant(_dbpool, a)
    _seed_tenant(_dbpool, b)
    assert _claim_in_txn(_dbpool, a).claimed is True
    assert _claim_in_txn(_dbpool, b).claimed is False  # cap reached
    with _dbpool.connection() as conn:
        st = get_founding_status(conn)
    assert (st.claimed_count, st.remaining, st.all_claimed) == (100, 0, True)


@pytest.mark.integration
def test_concurrent_claim_exactly_one_wins(_dbpool) -> None:
    """KEYSTONE (VT-94): 50 concurrent claims at 99/100 → EXACTLY ONE succeeds, final
    count == 100 (never 101). The atomic UPDATE + row lock serialize the race."""
    _reset(_dbpool, 99)
    tids = [uuid4() for _ in range(50)]
    for t in tids:
        _seed_tenant(_dbpool, t)
    with ThreadPoolExecutor(max_workers=50) as ex:
        results = [f.result() for f in [ex.submit(_claim_in_txn, _dbpool, t) for t in tids]]
    wins = sum(1 for r in results if r.claimed)
    assert wins == 1, f"exactly 1 should win; got {wins}"
    with _dbpool.connection() as conn:
        st = get_founding_status(conn)
    assert st.claimed_count == 100  # never 101
    # exactly one audit row added
    with _dbpool.connection() as conn:
        n = conn.execute("SELECT count(*) AS n FROM founding_tier_claims").fetchone()["n"]
    assert n == 1


@pytest.mark.integration
def test_one_slot_per_tenant_no_double_count(_dbpool) -> None:
    """A re-claim for the same tenant BELOW cap must not double-count (audit UNIQUE)."""
    _reset(_dbpool, 0)
    tid = uuid4()
    _seed_tenant(_dbpool, tid)
    assert _claim_in_txn(_dbpool, tid).claimed is True
    _claim_in_txn(_dbpool, tid)  # re-claim
    with _dbpool.connection() as conn:
        st = get_founding_status(conn)
        n = conn.execute(
            "SELECT count(*) AS n FROM founding_tier_claims WHERE tenant_id=%s", (str(tid),)
        ).fetchone()["n"]
    assert st.claimed_count == 1  # NOT 2
    assert n == 1


@pytest.mark.integration
def test_release_is_audit_only_no_decrement(_dbpool) -> None:
    _reset(_dbpool, 0)
    tid = uuid4()
    _seed_tenant(_dbpool, tid)
    _claim_in_txn(_dbpool, tid)
    with _dbpool.connection() as conn:
        release_founding_slot(conn, tid)
        st = get_founding_status(conn)
        row = conn.execute(
            "SELECT released_at FROM founding_tier_claims WHERE tenant_id=%s", (str(tid),)
        ).fetchone()
    assert st.claimed_count == 1  # NO decrement
    assert row["released_at"] is not None  # audit stamped


@pytest.mark.integration
def test_in_txn_rollback_leaks_no_slot(_dbpool) -> None:
    """A signup that rolls back AFTER claiming must not leak a permanent slot."""
    _reset(_dbpool, 0)
    tid = uuid4()
    _seed_tenant(_dbpool, tid)
    try:
        with _dbpool.connection() as conn, conn.transaction():
            try_claim_founding_slot(conn, tid)
            raise RuntimeError("simulated post-claim signup failure")
    except RuntimeError:
        pass
    with _dbpool.connection() as conn:
        st = get_founding_status(conn)
        n = conn.execute("SELECT count(*) AS n FROM founding_tier_claims").fetchone()["n"]
    assert st.claimed_count == 0  # the claim rolled back with the txn — no leak
    assert n == 0


@pytest.mark.integration
def test_dsr_hard_delete_claim_counter_untouched(_dbpool) -> None:
    _reset(_dbpool, 0)
    tid = uuid4()
    _seed_tenant(_dbpool, tid)
    _claim_in_txn(_dbpool, tid)
    # founding_tier_claims is in dsr_purge._PURGE_ORDER (hard-delete). Simulate the purge
    # delete (service-role); the workspace counter must be untouched.
    with _dbpool.connection() as conn:
        conn.execute("DELETE FROM founding_tier_claims WHERE tenant_id=%s", (str(tid),))
        st = get_founding_status(conn)
        n = conn.execute(
            "SELECT count(*) AS n FROM founding_tier_claims WHERE tenant_id=%s", (str(tid),)
        ).fetchone()["n"]
    assert n == 0  # claim hard-deleted
    assert st.claimed_count == 1  # counter untouched (workspace singleton)


@pytest.mark.integration
def test_public_endpoint_cached_then_refreshes(_dbpool) -> None:
    """The public endpoint serves a cached value, then refreshes after the TTL window."""
    import orchestrator.api.signup as sig

    _reset(_dbpool, 5)
    sig._founding_cache["value"] = None
    sig._founding_cache["expiry"] = 0.0
    first = sig.founding_status()
    assert first["public_count"] == 5
    # a claim happens; the cache is still warm -> stale value served (by design)
    t = uuid4()
    _seed_tenant(_dbpool, t)
    _claim_in_txn(_dbpool, t)
    assert sig.founding_status()["public_count"] == 5  # cached (stale within TTL)
    # force the TTL to expire -> fresh read reflects the claim
    sig._founding_cache["expiry"] = 0.0
    assert sig.founding_status()["public_count"] == 6
