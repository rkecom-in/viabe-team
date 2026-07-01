"""VT-77 — DSR self-serve export substrate tests.

Live Postgres via DATABASE_URL (CI orchestrator job). Exercises export_tenant_data
+ build_export_zip + the PII denylist + tenant-scope + the VT-80 audit chain.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — DSR export tests skipped",
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


def _seed_tenant_with_data(pool, *, phone: str) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s)",
            (tid, f"dsr-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
        # a customer row carrying raw contact PII (phone_e164) that MUST be scrubbed
        conn.execute(
            "INSERT INTO customers (tenant_id, display_name, phone_e164) "
            "VALUES (%s, %s, %s)",
            (tid, "Test Customer", phone),
        )
    return tid


def test_export_gathers_tables_and_logs_chained_audit(pool):
    from orchestrator.dsr_export import export_tenant_data
    from orchestrator.observability.audit_verify import verify_chain

    tid = _seed_tenant_with_data(pool, phone="+919812345678")
    export = export_tenant_data(tid)

    assert export["tenant_id"] == tid
    assert "tenants" in export["tables"]
    assert "customers" in export["tables"]
    assert len(export["tables"]["tenants"]) == 1
    assert len(export["tables"]["customers"]) == 1

    # audit: requested + completed appended to the chain; the chain verifies from
    # this export's first row (suffix verify — robust against other tests' rows).
    with pool.connection() as conn:
        evs = conn.execute(
            "SELECT seq, event_type FROM privacy_audit_log WHERE tenant_id = %s "
            "AND event_type IN ('dsr_export_requested','dsr_export_completed') "
            "ORDER BY seq ASC",
            (tid,),
        ).fetchall()
        kinds = {(e["event_type"] if isinstance(e, dict) else e[1]) for e in evs}
        assert kinds == {"dsr_export_requested", "dsr_export_completed"}
        first_seq = evs[0]["seq"] if isinstance(evs[0], dict) else evs[0][0]
        assert verify_chain(conn, since_seq=first_seq).ok


def test_export_scrubs_pii_denylist(pool):
    from orchestrator.dsr_export import export_tenant_data

    secret_phone = "+919800011122"
    tid = _seed_tenant_with_data(pool, phone=secret_phone)
    export = export_tenant_data(tid)

    blob = json.dumps(export, default=str)
    assert secret_phone not in blob, "raw phone_e164 leaked into export"
    for row in export["tables"]["customers"]:
        assert "phone_e164" not in row
    for row in export["tables"].get("phone_token_resolutions", []):
        assert "phone_number_encrypted" not in row
    for row in export["tables"].get("tenant_oauth_tokens", []):
        assert "refresh_token_encrypted" not in row


def test_export_is_tenant_scoped(pool):
    from orchestrator.dsr_export import export_tenant_data

    tid_a = _seed_tenant_with_data(pool, phone="+919811111111")
    tid_b = _seed_tenant_with_data(pool, phone="+919822222222")
    export_a = export_tenant_data(tid_a)
    # tenant A's export contains only A's tenant row.
    tenant_ids = {str(r["id"]) for r in export_a["tables"]["tenants"]}
    assert tenant_ids == {tid_a}
    assert tid_b not in tenant_ids


def test_build_export_zip_has_manifest_and_tables(pool):
    from orchestrator.dsr_export import build_export_zip, export_tenant_data

    tid = _seed_tenant_with_data(pool, phone="+919833333333")
    zip_bytes = build_export_zip(export_tenant_data(tid))
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "customers.json" in names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["tenant_id"] == tid
        assert "data_residency" in manifest


def test_verify_internal_secret(monkeypatch):
    from orchestrator.api import dsr as dsr_api
    from fastapi import HTTPException

    monkeypatch.setenv("INTERNAL_API_SECRET", "s3cr3t")
    dsr_api._verify_internal_secret("s3cr3t")  # no raise
    with pytest.raises(HTTPException):
        dsr_api._verify_internal_secret("wrong")
    with pytest.raises(HTTPException):
        dsr_api._verify_internal_secret(None)


def test_export_endpoint_tracks_access_ticket(pool, monkeypatch):
    """VT-523: the /export endpoint records the access request as a dsr_tickets 'access'
    ticket and stamps it completed once the ZIP is built (regulator-facing parity with
    the deletion path — no more off-the-books access fulfilment)."""
    from orchestrator.api.dsr import DsrTenantBody, dsr_export

    monkeypatch.setenv("INTERNAL_API_SECRET", "test-secret-123")
    tid = _seed_tenant_with_data(pool, phone="+919812349999")

    resp = dsr_export(DsrTenantBody(tenant_id=tid), x_internal_secret="test-secret-123")
    assert resp.status_code == 200
    assert resp.media_type == "application/zip"
    ticket_id = resp.headers["X-DSR-Access-Ticket"]
    assert ticket_id

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT request_type, status, completed_at FROM dsr_tickets WHERE id = %s",
            (ticket_id,),
        ).fetchone()
    assert row is not None
    assert (row["request_type"] if isinstance(row, dict) else row[0]) == "access"
    assert (row["status"] if isinstance(row, dict) else row[1]) == "completed"
    assert (row["completed_at"] if isinstance(row, dict) else row[2]) is not None


def test_export_endpoint_reuses_open_access_ticket(pool, monkeypatch):
    """A second access export reuses an existing non-completed access ticket rather than
    spawning duplicates (one fulfilment path, Pillar 8)."""
    from orchestrator.api.dsr import DsrTenantBody, dsr_export

    monkeypatch.setenv("INTERNAL_API_SECRET", "test-secret-123")
    tid = _seed_tenant_with_data(pool, phone="+919812340000")
    with pool.connection() as conn:
        pre = conn.execute(
            "INSERT INTO dsr_tickets (tenant_id, request_type, status, acknowledged_at) "
            "VALUES (%s, 'access', 'acknowledged', now()) RETURNING id::text AS id",
            (tid,),
        ).fetchone()
    pre_id = pre["id"] if isinstance(pre, dict) else pre[0]

    resp = dsr_export(DsrTenantBody(tenant_id=tid), x_internal_secret="test-secret-123")
    assert resp.headers["X-DSR-Access-Ticket"] == pre_id  # reused the open ticket

    with pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM dsr_tickets WHERE tenant_id = %s AND request_type = 'access'",
            (tid,),
        ).fetchone()
        st = conn.execute(
            "SELECT status FROM dsr_tickets WHERE id = %s", (pre_id,)
        ).fetchone()
    assert (n["n"] if isinstance(n, dict) else n[0]) == 1  # exactly one access ticket
    assert (st["status"] if isinstance(st, dict) else st[0]) == "completed"
