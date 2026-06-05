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
