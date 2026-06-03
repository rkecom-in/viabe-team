"""VT-225 — L0 per-tenant k-anonymity admission canary (live PG).

The read gate now counts DISTINCT contributing tenants (l0_cell_contributors),
not row-level observation_count. Proves the anti-poisoning guarantee + the
DSR-cascade re-evaluation (Option B + strategy (c), Fazal-locked).

Brief A7-A10: 9 distinct → reject; 10 → admit; same tenant twice → count
unchanged; concurrent → no over-count (idempotent insert); + DSR-cascade.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — L0 k-anon admission tests skipped",
)

_FT = "routing_decision"


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


def _tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, 'l0 test', 'founding', 'onboarding')",
            (tid,),
        )
    return tid


def _write(cohort: str, tenant_id: str, content: dict | None = None) -> dict:
    from orchestrator.observability.l0_memory import write_l0_fragment

    return write_l0_fragment(
        fragment_type=_FT, cohort_key=cohort,
        content=content or {"signal": "x"}, tenant_id=tenant_id,
    )


def _query(cohort: str) -> int:
    from orchestrator.observability.l0_memory import query_l0

    return query_l0(fragment_type=_FT, cohort_key=cohort)["matched_count"]


def test_below_k_rejected_at_k_admitted(pool):
    """A7/A8 — 9 distinct contributors → query returns []; the 10th admits it."""
    cohort = f"c_{uuid4().hex[:10]}"
    for i in range(9):
        res = _write(cohort, _tenant(pool))
        assert res["contributor_count"] == i + 1
    assert _query(cohort) == 0  # 9 < 10 — rejected
    _write(cohort, _tenant(pool))  # 10th distinct tenant
    assert _query(cohort) == 1  # admitted


def test_same_tenant_twice_does_not_double_count(pool):
    """A9 / strategy (c) — a repeated (fragment, tenant) is idempotent: the PK +
    ON CONFLICT DO NOTHING keeps the distinct-contributor count flat."""
    cohort = f"c_{uuid4().hex[:10]}"
    t = _tenant(pool)
    first = _write(cohort, t)
    second = _write(cohort, t)
    assert first["contributor_count"] == 1
    assert second["contributor_count"] == 1  # unchanged
    assert second["observation_count"] == 2  # but observations still increment


def test_single_tenant_poisoning_rejected(pool):
    """The load-bearing case: one tenant writing 12 observations inflates
    observation_count to 12 but contributor_count stays 1 → NOT admitted."""
    cohort = f"c_{uuid4().hex[:10]}"
    t = _tenant(pool)
    last = {}
    for _ in range(12):
        last = _write(cohort, t)
    assert last["observation_count"] == 12
    assert last["contributor_count"] == 1
    assert _query(cohort) == 0  # poisoning rejected by the contributor gate


def test_dsr_cascade_drops_contributor_and_re_evaluates(pool):
    """A10 / DSR-cascade — purging a tenant drops its contributor row (ON DELETE
    CASCADE) and admission re-evaluates: 10 → admitted, delete 1 → 9 → rejected."""
    cohort = f"c_{uuid4().hex[:10]}"
    tenants = [_tenant(pool) for _ in range(10)]
    for t in tenants:
        _write(cohort, t)
    assert _query(cohort) == 1  # 10 contributors → admitted

    with pool.connection() as conn:
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenants[0],))
        remaining = conn.execute(
            "SELECT count(*) AS n FROM l0_cell_contributors c "
            "JOIN l0_fragments f ON f.id = c.fragment_id WHERE f.cohort_key = %s",
            (cohort,),
        ).fetchone()
    assert int(dict(remaining)["n"]) == 9  # cascade dropped the purged tenant's row
    assert _query(cohort) == 0  # re-evaluated: 9 < 10 → rejected
