"""VT-73 — agent context isolation canary (live PG).

Each of the 3 independent layers catches an injected cross-tenant leak; L3/L4 are
exempt (sanctioned aggregates); a violation records a tenant_isolation_breach
pipeline_steps row that the VT-79 Detector-1 sweep picks up. CL-422 synthetic.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("pydantic")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — context-isolation tests skipped",
)


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
            "VALUES (%s, 'iso', 'founding', 'paid_active')", (tid,),
        )
    return tid


def _customer(pool, tid: str) -> str:
    cid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO customers (id, tenant_id) VALUES (%s, %s)", (cid, tid),
        )
    return cid


def _run(pool, tid: str) -> str:
    rid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'twilio_inbound', 'running')", (rid, tid),
        )
    return rid


def _ctx(tid: str, rid: str, *, top_spenders=None, l3=None, l4=None):
    from orchestrator.context_builder import L3Priors, L4Skills, LedgerSummary, SalesRecoveryContext

    return SalesRecoveryContext(
        tenant_id=UUID(tid), run_id=UUID(rid), user_request="re-engage dormants",
        customer_ledger_summary=LedgerSummary(top_spenders=[UUID(x) for x in (top_spenders or [])]),
        l3_priors=l3 or L3Priors(),
        l4_skills=l4 or L4Skills(),
    )


def _breaches(pool, rid: str) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'tenant_isolation_breach'", (rid,),
        ).fetchone()
    return int(dict(row)["n"])


# --- pre-flight (the load-bearing layer) -------------------------------------


def test_preflight_catches_cross_tenant_customer(pool):
    from orchestrator.context_validator import ContextIsolationViolation, validate_context_isolation

    a, b = _tenant(pool), _tenant(pool)
    b_cust = _customer(pool, b)        # belongs to tenant B
    rid = _run(pool, a)
    ctx = _ctx(a, rid, top_spenders=[b_cust])  # A's bundle carries B's customer

    with pytest.raises(ContextIsolationViolation):
        validate_context_isolation(ctx)
    assert _breaches(pool, rid) == 1   # recorded for the Detector-1 sweep


def test_preflight_passes_clean(pool):
    from orchestrator.context_validator import validate_context_isolation

    a = _tenant(pool)
    a_cust = _customer(pool, a)
    rid = _run(pool, a)
    validate_context_isolation(_ctx(a, rid, top_spenders=[a_cust]))  # no raise
    assert _breaches(pool, rid) == 0


def test_preflight_l3_l4_exempt(pool):
    """L3/L4 are sanctioned cross-tenant aggregates (no tenant ids) — never flagged."""
    from orchestrator.context_builder import L3Priors, L4Skills
    from orchestrator.context_validator import validate_context_isolation

    a = _tenant(pool)
    rid = _run(pool, a)
    l3 = L3Priors(available=True, patterns=[{"cohort_key": "cafe|tier_2|60_90d", "n_tenants": 12}])
    l4 = L4Skills(available=True, skills=[{"id": str(uuid4()), "title": "x", "tags": [], "excerpt": ""}])
    validate_context_isolation(_ctx(a, rid, top_spenders=[], l3=l3, l4=l4))  # no raise
    assert _breaches(pool, rid) == 0


# --- in-flight (decorator seam) ----------------------------------------------


def test_inflight_assert_blocks_mismatch(pool):
    from orchestrator.context_validator import ContextIsolationViolation
    from orchestrator.observability.decorators import ObservabilityContext, _assert_tool_tenant

    a, b = uuid4(), uuid4()
    ctx = ObservabilityContext(run_id=uuid4(), tenant_id=a)
    # tool arg names tenant B while the dispatch is tenant A → block.
    with pytest.raises(ContextIsolationViolation):
        _assert_tool_tenant(ctx, {"tenant_id": str(b)}, "some_tool")
    # matching tenant → no raise; no tenant arg → no raise.
    _assert_tool_tenant(ctx, {"tenant_id": str(a)}, "some_tool")
    _assert_tool_tenant(ctx, {"unrelated": 1}, "self_evaluate")


# --- post-flight (service-role scan) -----------------------------------------


def test_postflight_detects_stray_tenant(pool):
    from orchestrator.context_validator import audit_run_isolation

    a, b = _tenant(pool), _tenant(pool)
    rid = _run(pool, a)
    # Inject a stray step logged under tenant B for tenant A's run (the leak the
    # post-flight scan exists to catch).
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_steps (run_id, tenant_id, step_seq, step_kind, status) "
            "VALUES (%s, %s, 1, 'stray', 'completed')", (rid, b),
        )
    audit_run_isolation(UUID(rid), UUID(a))
    assert _breaches(pool, rid) >= 1   # post-flight recorded the breach

    clean = _run(pool, a)
    audit_run_isolation(UUID(clean), UUID(a))  # no stray → no breach
    assert _breaches(pool, clean) == 0


# --- G3: the breach fires the Detector-1 sweep -------------------------------


def test_breach_fires_detector_sweep(pool):
    from orchestrator.alerts.triggers import detect_slow_triggers
    from orchestrator.context_validator import ContextIsolationViolation, validate_context_isolation

    a, b = _tenant(pool), _tenant(pool)
    b_cust = _customer(pool, b)
    rid = _run(pool, a)
    with pytest.raises(ContextIsolationViolation):
        validate_context_isolation(_ctx(a, rid, top_spenders=[b_cust]))

    kinds = {t.trigger_kind for t in detect_slow_triggers(a)}
    assert "tenant_isolation_breach" in kinds
