"""VT-87 PR-1 — owner-portal dashboard-summary endpoint (the data-spine).

Pure: phone masking (last-4, raw never emitted). DB (gated on DATABASE_URL): the summary
payload — count + top-customers (MASKED at source) + recent campaigns, tenant-scoped +
cross-tenant + the X-Internal-Secret gate. Heavy imports guarded (VT-337 dep-less lesson).
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

# owner_dashboard -> fastapi + db.wrappers -> psycopg. Skip cleanly dep-less.
pytest.importorskip("fastapi")
pytest.importorskip("psycopg")

from orchestrator.api.owner_dashboard import _mask_phone, dashboard_summary  # noqa: E402


# ----------------------------- pure: masking ------------------------------------------
@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("+919876543210", "••••3210"),
        ("9876543210", "••••3210"),
        (None, None),
        ("", None),
        ("12", "••••"),
    ],
)
def test_mask_phone(raw, masked) -> None:
    assert _mask_phone(raw) == masked


def test_endpoint_mapping_campaign_id_and_mask(monkeypatch) -> None:
    """No-DB: the response mapping uses the wrapper's real keys (campaign_id, not id) and
    masks the phone. Catches the field-mismatch class."""
    import orchestrator.api.owner_dashboard as od

    monkeypatch.setenv("INTERNAL_API_SECRET", "s")
    monkeypatch.setattr(od.CustomersWrapper, "count_all", lambda self, t: 3)
    monkeypatch.setattr(
        od.CustomersWrapper, "top_customers_by_spend",
        lambda self, t, *, limit: [
            {"display_name": "Asha", "phone_e164": "+919876543210", "spend_paise": 5000}
        ],
    )
    monkeypatch.setattr(
        od.CampaignsWrapper, "list_recent_with_responses",
        lambda self, t, *, days_back, limit: [
            {"campaign_id": "camp-1", "status": "sent", "template_id": "tmpl",
             "response_count": 4, "sent_at": "2026-06-01"}
        ],
    )
    out = od.dashboard_summary(tenant_id=str(uuid4()), x_internal_secret="s")
    assert out["recent_campaigns"][0]["campaign_id"] == "camp-1"  # the field-mapping fix
    assert out["recent_campaigns"][0]["responses"] == 4
    assert out["top_customers"][0]["phone_last4"] == "••••3210"

    import json

    assert "9876543210" not in json.dumps(out)  # raw phone never in the payload


@pytest.mark.integration
def test_settings_plan_and_trial(monkeypatch, _dbpool) -> None:
    """Settings plan/trial: trial_ends_at = trial_started_at + 14d; secret gates."""
    from fastapi import HTTPException

    from orchestrator.api.owner_dashboard import dashboard_settings

    monkeypatch.setenv("INTERNAL_API_SECRET", "s")
    tid = uuid4()
    with _dbpool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, trial_started_at) "
            "VALUES (%s, 'Shop', 'founding', 'onboarding', '2026-06-01T00:00:00+00:00')",
            (str(tid),),
        )
    out = dashboard_settings(tenant_id=str(tid), x_internal_secret="s")
    assert out["plan"]["plan_tier"] == "founding"
    assert out["plan"]["trial_ends_at"].startswith("2026-06-15")  # +14d
    with pytest.raises(HTTPException):
        dashboard_settings(tenant_id=str(tid), x_internal_secret="wrong")


@pytest.mark.integration
def test_reports_lists_desc_with_pdf_flag(monkeypatch, _dbpool) -> None:
    from fastapi import HTTPException

    from orchestrator.api.owner_dashboard import dashboard_reports

    monkeypatch.setenv("INTERNAL_API_SECRET", "s")
    tid = uuid4()
    with _dbpool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, 't', 'standard', 'onboarding')",
            (str(tid),),
        )
        conn.execute(
            "INSERT INTO monthly_reports (tenant_id, year_month, pdf_storage_path) "
            "VALUES (%s, '2026-05', 'p/2026-05.pdf')",
            (str(tid),),
        )
        conn.execute(
            "INSERT INTO monthly_reports (tenant_id, year_month) VALUES (%s, '2026-04')",
            (str(tid),),
        )
    out = dashboard_reports(tenant_id=str(tid), x_internal_secret="s")
    assert [r["year_month"] for r in out["reports"]] == ["2026-05", "2026-04"]  # DESC
    assert out["reports"][0]["has_pdf"] is True
    assert out["reports"][1]["has_pdf"] is False
    with pytest.raises(HTTPException):
        dashboard_reports(tenant_id=str(tid), x_internal_secret="wrong")


def test_campaigns_endpoint_maps_and_gates(monkeypatch) -> None:
    """No-DB: dashboard_campaigns maps the wrapper keys + the secret gates (campaigns
    carry no PII, so no masking to assert)."""
    import orchestrator.api.owner_dashboard as od
    from fastapi import HTTPException

    monkeypatch.setenv("INTERNAL_API_SECRET", "s")
    monkeypatch.setattr(
        od.CampaignsWrapper, "list_recent_with_responses",
        lambda self, t, *, days_back, limit: [
            {"campaign_id": "c1", "status": "sent", "template_id": "tmpl",
             "response_count": 3, "sent_at": "2026-06-01"}
        ],
    )
    out = od.dashboard_campaigns(
        tenant_id=str(uuid4()), days_back=365, limit=50, x_internal_secret="s"
    )
    assert out["campaigns"][0]["campaign_id"] == "c1"
    assert out["campaigns"][0]["responses"] == 3
    with pytest.raises(HTTPException) as exc:
        od.dashboard_campaigns(tenant_id=str(uuid4()), days_back=365, limit=50, x_internal_secret="x")
    assert exc.value.status_code == 403


# ----------------------------- VT-341: report-download signed URL ----------------------
def test_report_signed_url_normalizes_and_fails_safe() -> None:
    from orchestrator.owner_surface.report_storage import report_download_signed_url

    class _Ok:
        def create_signed_url(self, path, ttl):  # noqa: ANN001
            return {"signedURL": f"https://x/{path}?ttl={ttl}"}

    url = report_download_signed_url("tid", "2026-05", client=_Ok())
    assert url is not None and "tid/2026-05.pdf" in url

    class _Err:
        def create_signed_url(self, path, ttl):  # noqa: ANN001
            raise RuntimeError("boom")

    assert report_download_signed_url("tid", "2026-05", client=_Err()) is None


def test_report_signed_url_short_ttl_and_self_validates() -> None:
    """VT-341 amends: default TTL is short (300s, not 1h); the fn self-validates ym."""
    from orchestrator.owner_surface.report_storage import (
        _DEFAULT_SIGNED_URL_TTL_SECONDS,
        report_download_signed_url,
    )

    assert _DEFAULT_SIGNED_URL_TTL_SECONDS == 300

    seen: dict[str, int] = {}

    class _Cap:
        def create_signed_url(self, path, ttl):  # noqa: ANN001
            seen["ttl"] = ttl
            return {"signedURL": "https://x"}

    report_download_signed_url("tid", "2026-05", client=_Cap())
    assert seen["ttl"] == 300  # the default is short

    # self-defending: a bad ym never builds a path / mints a URL (returns None directly)
    for bad in ("2026-13", "../etc", "2026-05/../x", ""):
        assert report_download_signed_url("tid", bad, client=_Cap()) is None


def test_report_download_url_passes_short_ttl(monkeypatch) -> None:
    """The endpoint passes ttl_seconds=300 explicitly (not the default)."""
    import orchestrator.api.owner_dashboard as od

    monkeypatch.setenv("INTERNAL_API_SECRET", "s")
    seen: dict[str, int] = {}
    monkeypatch.setattr(
        "orchestrator.owner_surface.report_storage.report_download_signed_url",
        lambda t, ym, *, ttl_seconds: (seen.__setitem__("ttl", ttl_seconds) or "https://u"),
    )
    body = od.ReportDownloadBody(tenant_id="t", year_month="2026-05")
    od.report_download_url(body=body, x_internal_secret="s")
    assert seen["ttl"] == 300


def test_report_download_url_mints(monkeypatch) -> None:
    import orchestrator.api.owner_dashboard as od

    monkeypatch.setenv("INTERNAL_API_SECRET", "s")
    monkeypatch.setattr(
        "orchestrator.owner_surface.report_storage.report_download_signed_url",
        lambda t, ym, **k: "https://signed/url",
    )
    body = od.ReportDownloadBody(tenant_id=str(uuid4()), year_month="2026-05")
    assert od.report_download_url(body=body, x_internal_secret="s")["signed_url"] == "https://signed/url"


@pytest.mark.parametrize("bad", ["2026-13", "2026-5", "../etc", "2026-05/../x", "", "abcd-12"])
def test_report_download_url_rejects_bad_ym(monkeypatch, bad) -> None:
    from fastapi import HTTPException

    import orchestrator.api.owner_dashboard as od

    monkeypatch.setenv("INTERNAL_API_SECRET", "s")
    body = od.ReportDownloadBody(tenant_id="t", year_month=bad)
    with pytest.raises(HTTPException) as exc:
        od.report_download_url(body=body, x_internal_secret="s")
    assert exc.value.status_code == 400  # no traversal / malformed ym


def test_report_download_url_secret_and_404(monkeypatch) -> None:
    from fastapi import HTTPException

    import orchestrator.api.owner_dashboard as od

    monkeypatch.setenv("INTERNAL_API_SECRET", "s")
    body = od.ReportDownloadBody(tenant_id="t", year_month="2026-05")
    with pytest.raises(HTTPException) as exc:
        od.report_download_url(body=body, x_internal_secret="wrong")
    assert exc.value.status_code == 403  # secret gate
    monkeypatch.setattr(
        "orchestrator.owner_surface.report_storage.report_download_signed_url",
        lambda t, ym, **k: None,
    )
    with pytest.raises(HTTPException) as exc2:
        od.report_download_url(body=body, x_internal_secret="s")
    assert exc2.value.status_code == 404  # absent PDF


def test_secret_gate_rejects_bad_secret(monkeypatch) -> None:
    from fastapi import HTTPException

    monkeypatch.setenv("INTERNAL_API_SECRET", "right")
    with pytest.raises(HTTPException) as exc:
        dashboard_summary(tenant_id=str(uuid4()), x_internal_secret="wrong")
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        dashboard_summary(tenant_id=str(uuid4()), x_internal_secret=None)


# ----------------------------- DB integration -----------------------------------------
@pytest.fixture
def _dbpool():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL not set; integration test requires real DB")
    import apply_migrations  # idempotent — ensure the schema exists regardless of test order

    if apply_migrations.apply(dsn=db_url)["failed"]:
        pytest.fail("migrations failed")
    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            db_url, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return get_pool()


def _seed(pool, tid, customers):
    """customers: (display_name, phone_e164, opt_out_status, spend_paise)."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tid), f"vt87-{tid}"),
        )
        for name, phone, status, spend in customers:
            row = conn.execute(
                "INSERT INTO customers (tenant_id, display_name, phone_e164, opt_out_status) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (str(tid), name, phone, status),
            ).fetchone()
            if spend:
                conn.execute(
                    "INSERT INTO customer_ledger_entries (tenant_id, customer_id, entry_key, "
                    "amount_paise, entry_type, entry_date, acquired_via, source_confidence) "
                    "VALUES (%s, %s, %s, %s, 'sale', now(), 'owner_typed', 1.0)",
                    (str(tid), row["id"], f"{tid}-{row['id']}-{spend}", spend),
                )


@pytest.mark.integration
def test_summary_masks_and_orders(monkeypatch, _dbpool) -> None:
    monkeypatch.setenv("INTERNAL_API_SECRET", "s")
    tid = uuid4()
    _seed(_dbpool, tid, [
        ("Big Spender", "+919811111111", "subscribed", 500000),
        ("Small", "+919822222222", "subscribed", 100),
        ("OptedOut", "+919833333333", "opted_out", 999999),  # excluded
    ])
    out = dashboard_summary(tenant_id=str(tid), x_internal_secret="s")
    assert out["customer_count"] == 3
    names = [c["display_name"] for c in out["top_customers"]]
    assert names[0] == "Big Spender"  # spend-ordered
    assert "OptedOut" not in names  # opted-out excluded
    # MASKED at source: last4 present, NO raw phone anywhere in the payload
    assert out["top_customers"][0]["phone_last4"] == "••••1111"
    import json

    assert "9811111111" not in json.dumps(out)  # raw phone NEVER crosses the boundary


@pytest.mark.integration
def test_summary_cross_tenant(monkeypatch, _dbpool) -> None:
    monkeypatch.setenv("INTERNAL_API_SECRET", "s")
    a, b = uuid4(), uuid4()
    _seed(_dbpool, a, [("A-cust", "+919700000001", "subscribed", 100)])
    _seed(_dbpool, b, [("B-cust", "+919700000002", "subscribed", 100)])
    out_a = dashboard_summary(tenant_id=str(a), x_internal_secret="s")
    names = [c["display_name"] for c in out_a["top_customers"]]
    assert names == ["A-cust"] and "B-cust" not in names  # tenant A can't see B


# ----------------------------- VT-338: customers list ---------------------------------
@pytest.mark.integration
def test_customers_paginated_masked(monkeypatch, _dbpool) -> None:
    from orchestrator.api.owner_dashboard import dashboard_customers

    monkeypatch.setenv("INTERNAL_API_SECRET", "s")
    tid = uuid4()
    _seed(_dbpool, tid, [(f"Cust{i}", f"+91980000{i:04d}", "subscribed", i * 100) for i in range(5)])
    # NOTE: called directly (not via HTTP), so FastAPI Query() defaults aren't injected —
    # pass excluded_only explicitly (a bare Query(False) default is a truthy FieldInfo).
    out = dashboard_customers(
        tenant_id=str(tid), page=1, page_size=2, excluded_only=False, x_internal_secret="s"
    )
    assert out["total"] == 5
    assert out["page_size"] == 2 and len(out["customers"]) == 2
    import json

    assert "98000000" not in json.dumps(out)  # raw phone NEVER crosses the boundary
    assert all(c["phone_last4"].startswith("••••") for c in out["customers"] if c["phone_last4"])


@pytest.mark.integration
def test_customers_cross_tenant(monkeypatch, _dbpool) -> None:
    from orchestrator.api.owner_dashboard import dashboard_customers

    monkeypatch.setenv("INTERNAL_API_SECRET", "s")
    a, b = uuid4(), uuid4()
    _seed(_dbpool, a, [("A", "+919700000001", "subscribed", 100)])
    _seed(_dbpool, b, [("B", "+919700000002", "subscribed", 100)])
    out = dashboard_customers(
        tenant_id=str(a), page=1, page_size=50, excluded_only=False, x_internal_secret="s"
    )
    names = [c["display_name"] for c in out["customers"]]
    assert names == ["A"] and "B" not in names


@pytest.mark.integration
def test_customers_excluded_filter(monkeypatch, _dbpool) -> None:
    from orchestrator.api.owner_dashboard import dashboard_customers

    monkeypatch.setenv("INTERNAL_API_SECRET", "s")
    tid = uuid4()
    _seed(_dbpool, tid, [
        ("Sub", "+919811111111", "subscribed", 100),
        ("Out", "+919822222222", "opted_out", 100),
    ])
    out = dashboard_customers(
        tenant_id=str(tid), page=1, page_size=50, excluded_only=True, x_internal_secret="s"
    )
    names = [c["display_name"] for c in out["customers"]]
    assert names == ["Out"] and "Sub" not in names
