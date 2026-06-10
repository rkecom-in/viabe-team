"""VT-86 — monthly impact report generator tests.

Pure-unit coverage (month math + skip logic + honesty flags) always runs;
the SQL-aggregation tests are DATABASE_URL-gated and seed SYNTHETIC data only
(CL-422) against a tenant-scoped direct connection. The generator filters
tenant_id explicitly, so a superuser test connection still gets correct
per-tenant results; the cross-tenant test asserts that isolation.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.owner_surface.monthly_report import (  # noqa: E402
    MonthlyReport,
    generate_monthly_report,
    month_bounds,
    should_skip,
)


# --------------------------- pure unit (no DB) ----------------------------


def test_month_bounds_mid_year():
    start, end = month_bounds("2026-04")
    assert start == datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_month_bounds_december_wraps_year():
    start, end = month_bounds("2026-12")
    assert start == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_should_skip_cancelled_and_lapsed():
    end = datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert should_skip(phase="cancelled", signed_up_at=None, period_end=end) == "phase=cancelled"
    assert should_skip(phase="lapsed", signed_up_at=None, period_end=end) == "phase=lapsed"


def test_should_skip_recent_signup():
    end = datetime(2026, 5, 1, tzinfo=timezone.utc)
    # signed up 2026-04-20 → < 30 days before period end → skip.
    recent = datetime(2026, 4, 20, tzinfo=timezone.utc)
    assert should_skip(phase="paid_active", signed_up_at=recent, period_end=end) == "signed_up_lt_30d"


def test_should_not_skip_established_or_trial():
    end = datetime(2026, 5, 1, tzinfo=timezone.utc)
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert should_skip(phase="paid_active", signed_up_at=old, period_end=end) is None
    # trial is NOT a skip — it gets trial framing.
    assert should_skip(phase="trial", signed_up_at=old, period_end=end) is None


def _report(**over):
    base = dict(
        tenant_id=str(uuid4()), year_month="2026-04", business_name="Cafe",
        language="en", trial_framing=False,
        campaign_status_counts={"proposed": 0, "approved": 0, "rejected": 0,
                                "sent": 0, "failed": 0},
        approved_count=0, rejected_count=0, pending_count=0,
        arrr_paise=0, top_campaigns=[], customers_added=0,
        customers_added_prior_month=0,
    )
    base.update(over)
    return MonthlyReport(**base)


def test_honesty_flags():
    zero = _report(arrr_paise=0, approved_count=0)
    assert zero.zero_arrr is True
    assert zero.low_engagement is True
    healthy = _report(arrr_paise=50000, approved_count=5,
                       campaign_status_counts={"proposed": 0, "approved": 5,
                                               "rejected": 0, "sent": 4, "failed": 0})
    assert healthy.zero_arrr is False
    assert healthy.low_engagement is False
    assert healthy.campaigns_sent == 4


def test_fees_descoped_default_none():
    r = _report()
    assert r.fees_paid_paise is None
    assert r.net_value_paise is None


# --------------------------- DB-backed aggregation ------------------------

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — generator DB tests skipped",
)

APR_START = datetime(2026, 4, 10, tzinfo=timezone.utc)   # in 2026-04
MAR_DATE = datetime(2026, 3, 15, tzinfo=timezone.utc)    # prior month
OLD_SIGNUP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _conn():
    # VT-306: dict_row matches the production pool (tenant_connection) — the
    # wrappers generate_monthly_report now uses assume dict rows.
    from psycopg.rows import dict_row

    return psycopg.connect(
        os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row
    )


def _tenant(conn, *, phase="paid_active", signed_up_at=OLD_SIGNUP, lang="en"):
    return conn.execute(
        "INSERT INTO tenants (business_name, plan_tier, phase, signed_up_at, "
        "preferred_language) VALUES ('vt86-syn', 'founding', %s, %s, %s) RETURNING id",
        (phase, signed_up_at, lang),
    ).fetchone()["id"]


def _run(conn, tenant):
    return conn.execute(
        "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
        "VALUES (%s, 'orchestrator', 'running') RETURNING id", (tenant,),
    ).fetchone()["id"]


def _campaign(conn, tenant, run, *, status, generated_at, closed_at=None):
    return conn.execute(
        "INSERT INTO campaigns (tenant_id, run_id, plan_json, status, generated_at, "
        "attribution_closed_at) VALUES (%s, %s, '{}'::jsonb, %s, %s, %s) RETURNING id",
        (tenant, run, status, generated_at, closed_at),
    ).fetchone()["id"]


def _attr(conn, tenant, campaign, paise):
    conn.execute(
        "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise) "
        "VALUES (%s, %s, %s)", (tenant, campaign, paise),
    )


def _customer(conn, tenant, created_at):
    conn.execute(
        "INSERT INTO customers (tenant_id, display_name, created_at) "
        "VALUES (%s, 'vt86-syn-cust', %s)", (tenant, created_at),
    )


def _as_app_role(conn, tenant_id):
    """VT-306: switch the seeded conn to app_role + GUC, mirroring prod (the sweep
    runs generate_monthly_report inside a tenant_connection). The wrappers reject a
    non-app_role conn (defense-in-depth), so the skip-check tests that never reach a
    wrapper stay on the plain superuser conn."""
    conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant_id),))
    conn.execute("SET ROLE app_role")


def test_full_month_metrics_match_sql():
    with _conn() as conn:
        t = _tenant(conn)
        run = _run(conn, t)
        # 2 approved, 1 rejected, 1 proposed, 2 sent — all generated in April.
        for st in ("approved", "approved", "rejected", "proposed", "sent", "sent"):
            _campaign(conn, t, run, status=st, generated_at=APR_START)
        # A campaign generated in March must NOT count in April's status counts.
        _campaign(conn, t, run, status="approved", generated_at=MAR_DATE)
        # Two campaigns closing in April with attributions → month ARRR.
        c1 = _campaign(conn, t, run, status="sent", generated_at=APR_START,
                       closed_at=APR_START)
        c2 = _campaign(conn, t, run, status="sent", generated_at=APR_START,
                       closed_at=APR_START)
        _attr(conn, t, c1, 30000)
        _attr(conn, t, c2, 12000)
        # A campaign that closed in MARCH must not count in April ARRR.
        c3 = _campaign(conn, t, run, status="sent", generated_at=MAR_DATE,
                       closed_at=MAR_DATE)
        _attr(conn, t, c3, 99999)
        # Customers: 3 in April, 1 in March.
        for _ in range(3):
            _customer(conn, t, APR_START)
        _customer(conn, t, MAR_DATE)

        _as_app_role(conn, t)
        report = generate_monthly_report(str(t), "2026-04", conn=conn)

    assert report is not None
    # April-generated campaigns: approved=2(+1 sent-closed×2 also 'sent') —
    # count only by status within April. approved generated in April = 2.
    assert report.approved_count == 2
    assert report.rejected_count == 1
    assert report.pending_count == 1
    # ARRR = 30000 + 12000 (March close excluded).
    assert report.arrr_paise == 42000
    assert report.zero_arrr is False
    # Top campaigns ranked desc; the 30000 one first.
    assert [c.arrr_paise for c in report.top_campaigns][:2] == [30000, 12000]
    assert report.customers_added == 3
    assert report.customers_added_prior_month == 1
    assert report.fees_paid_paise is None


def test_zero_arrr_month_is_honest():
    with _conn() as conn:
        t = _tenant(conn)
        run = _run(conn, t)
        _campaign(conn, t, run, status="proposed", generated_at=APR_START)
        _as_app_role(conn, t)
        report = generate_monthly_report(str(t), "2026-04", conn=conn)
    assert report is not None
    assert report.arrr_paise == 0
    assert report.zero_arrr is True
    assert report.low_engagement is True  # approved < 2


def test_trial_tenant_gets_framing_not_skip():
    with _conn() as conn:
        t = _tenant(conn, phase="trial")
        _as_app_role(conn, t)
        report = generate_monthly_report(str(t), "2026-04", conn=conn)
    assert report is not None
    assert report.trial_framing is True


def test_recent_signup_skipped():
    with _conn() as conn:
        t = _tenant(conn, signed_up_at=datetime(2026, 4, 25, tzinfo=timezone.utc))
        report = generate_monthly_report(str(t), "2026-04", conn=conn)
    assert report is None


def test_lapsed_tenant_skipped():
    with _conn() as conn:
        t = _tenant(conn, phase="lapsed")
        report = generate_monthly_report(str(t), "2026-04", conn=conn)
    assert report is None


def test_cross_tenant_isolation():
    with _conn() as conn:
        t_a = _tenant(conn)
        t_b = _tenant(conn)
        run_b = _run(conn, t_b)
        cb = _campaign(conn, t_b, run_b, status="sent", generated_at=APR_START,
                       closed_at=APR_START)
        _attr(conn, t_b, cb, 77777)
        _customer(conn, t_b, APR_START)
        # Tenant A has no activity → its report must not see B's ARRR/customers.
        # VT-306: under A's tenant_connection scope, RLS (not just the WHERE) hides B.
        _as_app_role(conn, t_a)
        report = generate_monthly_report(str(t_a), "2026-04", conn=conn)
    assert report is not None
    assert report.arrr_paise == 0
    assert report.customers_added == 0
    assert report.top_campaigns == []


def test_hindi_language_propagates():
    with _conn() as conn:
        t = _tenant(conn, lang="hi")
        _as_app_role(conn, t)
        report = generate_monthly_report(str(t), "2026-04", conn=conn)
    assert report is not None
    assert report.language == "hi"
