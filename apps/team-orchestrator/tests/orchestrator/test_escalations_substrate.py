"""VT-292 — escalations + ops_audit substrate (Rule #15 canary, real Postgres).

Deny-all FORCE RLS (service-role only; app_role denied). record_escalation idempotent on
run_id; backfill seeds from pipeline_runs; record_ops_audit appends. CL-422 synthetic.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-292 escalations tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-292 test', 'founding', 'paid_active') RETURNING id"
        ).fetchone()[0])


def _run(dsn: str, tenant_id: str, status: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, status, trigger_kind) "
            "VALUES (%s, %s, 'test') RETURNING id",
            (tenant_id, status),
        ).fetchone()[0])


def test_record_escalation_idempotent_on_run(substrate):
    from orchestrator.escalations import record_escalation

    t = _tenant(substrate.dsn)
    run = _run(substrate.dsn, t, "running")
    record_escalation(t, "hard_limit", severity="high", run_id=run)
    record_escalation(t, "hard_limit", severity="high", run_id=run)  # idempotent
    with psycopg.connect(substrate.dsn, autocommit=True) as c:
        n = c.execute("SELECT count(*) FROM escalations WHERE run_id=%s", (run,)).fetchone()[0]
    assert n == 1


def test_record_escalation_rejects_bad_severity(substrate):
    from orchestrator.escalations import record_escalation

    with pytest.raises(ValueError, match="invalid severity"):
        record_escalation(_tenant(substrate.dsn), "x", severity="critical")


def test_backfill_from_pipeline_runs(substrate):
    from orchestrator.escalations import backfill_from_pipeline_runs

    t = _tenant(substrate.dsn)
    _run(substrate.dsn, t, "aborted_hard_limit")
    _run(substrate.dsn, t, "escalated")
    _run(substrate.dsn, t, "running")  # not an escalation
    n = backfill_from_pipeline_runs()
    assert n >= 2  # at least the two we just seeded (idempotent across re-runs)
    # the running one did NOT become an escalation
    with psycopg.connect(substrate.dsn, autocommit=True) as c:
        kinds = {
            r[0] for r in c.execute(
                "SELECT kind FROM escalations WHERE tenant_id=%s", (t,)
            ).fetchall()
        }
    assert "hard_limit" in kinds and "agent_escalated" in kinds


def test_record_ops_audit_appends(substrate):
    from orchestrator.escalations import record_ops_audit

    op = str(uuid4())
    t = _tenant(substrate.dsn)
    record_ops_audit(op, "resolve", "escalation", tenant_id=t, target_id="esc-1", detail="resolved")
    with psycopg.connect(substrate.dsn, autocommit=True) as c:
        n = c.execute(
            "SELECT count(*) FROM ops_audit WHERE operator_id=%s AND action='resolve'", (op,)
        ).fetchone()[0]
    assert n == 1


def test_deny_all_rls_blocks_app_role(substrate):
    """Both escalations + ops_audit are service-role only — app_role sees ZERO."""
    from orchestrator.db import tenant_connection
    from orchestrator.escalations import record_escalation, record_ops_audit

    t = _tenant(substrate.dsn)
    record_escalation(t, "agent_escalated")
    record_ops_audit(str(uuid4()), "override", "escalation", tenant_id=t)
    with tenant_connection(t) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM escalations")
        esc = (cur.fetchone() or {"n": -1})
        cur.execute("SELECT count(*) AS n FROM ops_audit")
        aud = (cur.fetchone() or {"n": -1})
    esc_n = esc["n"] if isinstance(esc, dict) else esc[0]
    aud_n = aud["n"] if isinstance(aud, dict) else aud[0]
    assert esc_n == 0 and aud_n == 0


# --- VT-357 part 2 — SLA-breach sweep --------------------------------------------------------
def _seed_escalation(dsn, tenant, *, opened_sql: str, status: str = "open") -> str:
    """Insert an escalation with an explicit opened_at (SQL expr) + status; return its id."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            f"INSERT INTO escalations (tenant_id, kind, severity, status, opened_at) "  # noqa: S608
            f"VALUES (%s, 'support_fallback', 'medium', %s, {opened_sql}) RETURNING id",
            (tenant, status),
        ).fetchone()[0])


def _sla_alerted_at(dsn, eid):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT sla_alerted_at FROM escalations WHERE id = %s", (eid,)
        ).fetchone()[0]


def test_sla_sweep_breaches_and_is_idempotent(substrate, monkeypatch):
    """VT-357: a business-hours escalation past 4h breaches → 2nd Fazal alert + marker set; the
    next sweep does NOT re-alert it (marker-gated). A fresh escalation does NOT breach."""
    from orchestrator import escalations as esc

    alerts: list[str] = []
    monkeypatch.setattr(
        "orchestrator.billing.refund_executor._alert_fazal", lambda text: alerts.append(text)
    )
    t = _tenant(substrate.dsn)
    # opened in business hours (12:30 IST) days ago → past the 4h SLA.
    breached = _seed_escalation(substrate.dsn, t, opened_sql="TIMESTAMPTZ '2026-06-01 12:30:00+05:30'")
    fresh = _seed_escalation(substrate.dsn, t, opened_sql="now()")  # 0 elapsed → not breached

    ids1 = esc.run_sla_breach_sweep_body()
    assert breached in ids1  # breached → alerted
    assert fresh not in ids1  # fresh → not breached
    assert _sla_alerted_at(substrate.dsn, breached) is not None  # marker set
    assert _sla_alerted_at(substrate.dsn, fresh) is None
    n_after_first = len([a for a in alerts if breached in a])

    ids2 = esc.run_sla_breach_sweep_body()  # re-run
    assert breached not in ids2  # marker gates → NOT re-alerted
    assert len([a for a in alerts if breached in a]) == n_after_first  # no second ping


def test_sla_sweep_offhours_breaches_past_24h(substrate, monkeypatch):
    """VT-357: an OFF-hours escalation (02:00 IST) days ago breaches the 24h window."""
    from orchestrator import escalations as esc

    monkeypatch.setattr("orchestrator.billing.refund_executor._alert_fazal", lambda text: None)
    t = _tenant(substrate.dsn)
    old_off = _seed_escalation(substrate.dsn, t, opened_sql="TIMESTAMPTZ '2026-06-01 02:00:00+05:30'")
    assert old_off in esc.run_sla_breach_sweep_body()
    assert _sla_alerted_at(substrate.dsn, old_off) is not None


def test_sla_sweep_skips_resolved(substrate, monkeypatch):
    """VT-357: a RESOLVED escalation past SLA is NOT alerted (status='open' filter)."""
    from orchestrator import escalations as esc

    monkeypatch.setattr("orchestrator.billing.refund_executor._alert_fazal", lambda text: None)
    t = _tenant(substrate.dsn)
    resolved = _seed_escalation(
        substrate.dsn, t, opened_sql="TIMESTAMPTZ '2026-06-01 12:30:00+05:30'", status="resolved"
    )
    assert resolved not in esc.run_sla_breach_sweep_body()
    assert _sla_alerted_at(substrate.dsn, resolved) is None


# --- VT-282 — escalation-rate + decay metric ----------------------------------------------------
def _seed_at(dsn, tenant, kind, *, days_ago: int, n: int = 1) -> None:
    """Insert n escalations of `kind` opened `days_ago` days ago (for decay-window seeding)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        for _ in range(n):
            conn.execute(
                "INSERT INTO escalations (tenant_id, kind, severity, status, opened_at) "
                "VALUES (%s, %s, 'medium', 'open', now() - make_interval(days => %s))",
                (tenant, kind, days_ago),
            )


def test_escalation_decay_trends(substrate):
    """VT-282: per (tenant, kind) recent-vs-prior decay — declining=healthy; flat/rising flag the
    CL-426 product-bug signal. Real-PG, windows recent=7d / prior=7d."""
    from orchestrator.owner_surface.escalation_metrics import (
        escalation_decay,
        escalation_rate_by_category,
    )

    t = _tenant(substrate.dsn)
    # declining: 1 recent vs 5 prior
    _seed_at(substrate.dsn, t, "declining_kind", days_ago=1, n=1)
    _seed_at(substrate.dsn, t, "declining_kind", days_ago=10, n=5)
    # flat: 3 recent vs 3 prior
    _seed_at(substrate.dsn, t, "flat_kind", days_ago=1, n=3)
    _seed_at(substrate.dsn, t, "flat_kind", days_ago=10, n=3)
    # rising: 3 recent vs 1 prior
    _seed_at(substrate.dsn, t, "rising_kind", days_ago=1, n=3)
    _seed_at(substrate.dsn, t, "rising_kind", days_ago=10, n=1)

    decay = {d["kind"]: d for d in escalation_decay(tenant_id=t)}
    assert decay["declining_kind"]["trend"] == "declining" and decay["declining_kind"]["healthy"]
    assert decay["flat_kind"]["trend"] == "flat" and not decay["flat_kind"]["healthy"]
    assert decay["rising_kind"]["trend"] == "rising" and not decay["rising_kind"]["healthy"]

    # rate-by-category counts the recent (default 7d) window only.
    rate = {r["kind"]: r["count"] for r in escalation_rate_by_category(tenant_id=t)}
    assert rate["declining_kind"] == 1 and rate["flat_kind"] == 3 and rate["rising_kind"] == 3


# --- VT-279 — record_escalation stores the deterministic VTR/OWNER route -------------------------
def test_record_escalation_stores_route(substrate):
    """VT-279: record_escalation classifies + stores `route` — knowledge-gap → vtr; authority →
    owner; identity (phone) → owner (VT-281). Real-PG."""
    from uuid import uuid4

    from orchestrator.escalations import record_escalation

    t = _tenant(substrate.dsn)
    cases = {
        "how does the ledger reconciliation work?": ("vtr", uuid4()),
        "should I approve a refund for this order?": ("owner", uuid4()),
        "customer +91 98765 43210 wants a callback": ("owner", uuid4()),
    }
    for notes, (_expect, rid) in cases.items():
        record_escalation(t, "support_fallback", notes=notes, run_id=rid)

    with psycopg.connect(substrate.dsn, autocommit=True) as c:
        rows = c.execute(
            "SELECT notes, route FROM escalations WHERE tenant_id = %s", (t,)
        ).fetchall()
    got = {(r["notes"] if isinstance(r, dict) else r[0]): (r["route"] if isinstance(r, dict) else r[1]) for r in rows}
    for notes, (expect, _rid) in cases.items():
        assert got.get(notes) == expect, f"{notes!r} → {got.get(notes)} (expected {expect})"
