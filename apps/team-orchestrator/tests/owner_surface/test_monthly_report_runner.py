"""VT-86 — monthly report runner (orchestration) tests.

Real SQL generate + persist on pg16; render/store/send injected as fakes so no
weasyprint/Supabase/Resend needed. DATABASE_URL-gated."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

pytest.importorskip("pydantic")
psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — runner tests skipped",
)

from orchestrator.owner_surface.monthly_report_runner import (  # noqa: E402
    run_monthly_report,
)

OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)
APR = datetime(2026, 4, 10, tzinfo=timezone.utc)


def _conn():
    # VT-306: dict_row matches the production pool (the wrappers
    # generate_monthly_report now uses assume dict rows).
    from psycopg.rows import dict_row

    return psycopg.connect(
        os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row
    )


def _tenant(conn, *, phase="paid_active"):
    return conn.execute(
        "INSERT INTO tenants (business_name, plan_tier, phase, signed_up_at, "
        "preferred_language) VALUES ('vt86-syn', 'founding', %s, %s, 'en') RETURNING id",
        (phase, OLD),
    ).fetchone()["id"]


def _seed_activity(conn, tenant):
    run = conn.execute(
        "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
        "VALUES (%s, 'orchestrator', 'running') RETURNING id", (tenant,),
    ).fetchone()["id"]
    c = conn.execute(
        "INSERT INTO campaigns (tenant_id, run_id, plan_json, status, generated_at, "
        "attribution_closed_at) VALUES (%s, %s, '{}'::jsonb, 'sent', %s, %s) RETURNING id",
        (tenant, run, APR, APR),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise) "
        "VALUES (%s, %s, 25000)", (tenant, c),
    )


def _as_app_role(conn, tenant_id):
    """VT-306: switch the seeded conn to app_role + GUC, mirroring prod — the
    scheduled sweep calls run_monthly_report inside a tenant_connection. The
    wrappers reject a non-app_role conn (defense-in-depth)."""
    conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant_id),))
    conn.execute("SET ROLE app_role")


def _fakes(send_result=True):
    calls = {"sent": 0}

    def render(report):
        return b"%PDF-fake"

    def store(tenant_id, year_month, pdf):
        calls["stored"] = (tenant_id, year_month, pdf)
        return f"{tenant_id}/{year_month}.pdf"

    def send(report, pdf, *, to_addr, portal_url):
        calls["sent"] += 1
        calls["to"] = to_addr
        return send_result

    return render, store, send, calls


def _row(conn, tenant):
    return conn.execute(
        "SELECT pdf_storage_path, arrr_paise, email_sent_at, email_failure_count "
        "FROM monthly_reports WHERE tenant_id = %s", (tenant,),
    ).fetchone()


def test_skip_refunded_no_row():
    with _conn() as conn:
        t = _tenant(conn, phase="refunded")
        render, store, send, _ = _fakes()
        res = run_monthly_report(str(t), "2026-04", conn=conn, owner_email="o@x.com",
                                 render=render, store=store, send=send)
        assert res["status"] == "skipped"
        assert _row(conn, t) is None


def test_generated_email_success_persists_row():
    with _conn() as conn:
        t = _tenant(conn)
        _seed_activity(conn, t)
        _as_app_role(conn, t)
        render, store, send, calls = _fakes(send_result=True)
        res = run_monthly_report(str(t), "2026-04", conn=conn, owner_email="o@x.com",
                                 render=render, store=store, send=send)
        assert res["status"] == "generated"
        assert res["arrr_paise"] == 25000
        assert res["email_sent"] is True
        assert calls["sent"] == 1
        row = _row(conn, t)
        assert row["pdf_storage_path"] == f"{t}/2026-04.pdf"      # pdf_storage_path
        assert row["arrr_paise"] == 25000                    # arrr_paise
        assert row["email_sent_at"] is not None                 # email_sent_at set
        assert row["email_failure_count"] == 0                        # no failures


def test_email_failure_bumps_count():
    with _conn() as conn:
        t = _tenant(conn)
        _seed_activity(conn, t)
        _as_app_role(conn, t)
        render, store, send, _ = _fakes(send_result=False)
        run_monthly_report(str(t), "2026-04", conn=conn, owner_email="o@x.com",
                           render=render, store=store, send=send)
        row = _row(conn, t)
        assert row["email_sent_at"] is None    # email_sent_at NULL
        assert row["email_failure_count"] == 1       # failure count bumped


def test_no_owner_email_is_not_a_failure():
    with _conn() as conn:
        t = _tenant(conn)
        _seed_activity(conn, t)
        _as_app_role(conn, t)
        render, store, send, calls = _fakes()
        run_monthly_report(str(t), "2026-04", conn=conn, owner_email=None,
                           render=render, store=store, send=send)
        assert calls["sent"] == 0       # never attempted
        row = _row(conn, t)
        assert row["email_sent_at"] is None           # not sent
        assert row["email_failure_count"] == 0              # but NOT counted as a failure


def test_retry_upsert_idempotent():
    with _conn() as conn:
        t = _tenant(conn)
        _seed_activity(conn, t)
        _as_app_role(conn, t)
        # First run: email fails → count 1.
        render, store, fail_send, _ = _fakes(send_result=False)
        run_monthly_report(str(t), "2026-04", conn=conn, owner_email="o@x.com",
                           render=render, store=store, send=fail_send)
        # Retry: email succeeds → email_sent_at set, count stays 1 (not reset).
        render2, store2, ok_send, _ = _fakes(send_result=True)
        run_monthly_report(str(t), "2026-04", conn=conn, owner_email="o@x.com",
                           render=render2, store=store2, send=ok_send)
        rows = conn.execute(
            "SELECT count(*) AS n FROM monthly_reports WHERE tenant_id = %s", (t,)
        ).fetchone()["n"]
        assert rows == 1                 # upsert, not duplicate
        row = _row(conn, t)
        assert row["email_sent_at"] is not None        # now sent
        assert row["email_failure_count"] == 1               # prior failure retained, not reset
