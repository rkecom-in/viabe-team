"""VT-360 — VTR-facing reads route through app_vtr_role + the de-identified views ONLY. Real-PG.

Proves: the endpoints return EXACTLY the view columns (no message_text/payload/PII); the guarantee
probe — the endpoint's role (app_vtr_role) CANNOT read the raw tenant_alerts table; auth-gated;
bounded (hard cap). Synthetic only (CL-422).
"""

from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
import psycopg  # noqa: E402
from psycopg import errors as pg_errors  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

_SECRET = "vt360-internal"
_OP_SECRET = "vt360-operator-jwt"


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return dsn


def _op_jwt(operator_id: str) -> str:
    import jwt as pyjwt

    now = int(time.time())
    return pyjwt.encode(
        {"operator_claim": True, "operator_id": operator_id, "aud": "authenticated",
         "iat": now, "exp": now + 300},
        _OP_SECRET, algorithm="HS256",
    )


def _seed(dsn) -> str:
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        tid = str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-360', 'founding', 'paid_active') RETURNING id"
        ).fetchone()["id"])
        conn.execute(
            "INSERT INTO escalations (tenant_id, kind, severity, status, route, notes) "
            "VALUES (%s, 'how_to_gap', 'medium', 'open', 'vtr', 'SECRET operator note')",
            (tid,),
        )
        conn.execute(
            "INSERT INTO tenant_alerts (tenant_id, trigger_kind, severity, dedup_key, message_text) "
            "VALUES (%s, 'hard_limit', 'warning', %s, 'SECRET customer +91999... in body')",
            (tid, f"k{uuid4().hex[:8]}"),
        )
    return tid


def _env(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", _SECRET)
    monkeypatch.setenv("OPERATOR_JWT_SECRET", _OP_SECRET)


def _assign(dsn: str, operator_id: str, tenant_id: str) -> None:
    """VT-377 (mig-134): the views are assignment-scoped — an operator with no ACTIVE
    operator_assignments row reads zero rows (fail-closed), so row-asserting tests assign."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO operator_assignments (operator_id, tenant_id) VALUES (%s, %s)",
            (operator_id, tenant_id),
        )


def test_vtr_escalations_returns_view_columns_only(substrate, monkeypatch):
    _env(monkeypatch)
    from orchestrator.api.ops_resolve import VtrReadBody, vtr_escalations_read

    tid = _seed(substrate)
    # noise that MUST be filtered server-side (early-review F7): owner-routed + resolved.
    from psycopg.rows import dict_row
    with psycopg.connect(substrate, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("INSERT INTO escalations (tenant_id, kind, severity, status, route) "
                     "VALUES (%s, 'pricing', 'medium', 'open', 'owner')", (tid,))
        conn.execute("INSERT INTO escalations (tenant_id, kind, severity, status, route) "
                     "VALUES (%s, 'how_to_gap', 'medium', 'resolved', 'vtr')", (tid,))
    op = str(uuid4())
    _assign(substrate, op, tid)  # mig-134 scoping: row-asserting read needs the assignment
    out = vtr_escalations_read(
        VtrReadBody(operator_id=op), x_internal_secret=_SECRET, x_operator_jwt=_op_jwt(op)
    )
    assert out["count"] >= 1
    row = out["rows"][0]
    assert set(row) == {"escalation_id", "tenant_id", "tenant_name", "kind", "severity", "status",
                        "opened_at", "resolved_at", "route"}
    assert "notes" not in row  # operator free-text never surfaces
    # server-side filter holds: only route='vtr' + unresolved surface.
    assert all(r["route"] == "vtr" and r["status"] != "resolved" for r in out["rows"])


def test_vtr_monitoring_excludes_message_text(substrate, monkeypatch):
    _env(monkeypatch)
    from orchestrator.api.ops_resolve import VtrReadBody, vtr_monitoring_read

    tid = _seed(substrate)
    op = str(uuid4())
    _assign(substrate, op, tid)  # mig-134 scoping: row-asserting read needs the assignment
    out = vtr_monitoring_read(
        VtrReadBody(operator_id=op), x_internal_secret=_SECRET, x_operator_jwt=_op_jwt(op)
    )
    assert out["count"] >= 1
    row = out["rows"][0]
    assert set(row) == {"alert_id", "tenant_id", "tenant_name", "trigger_kind", "severity", "fired_at"}
    # free-text / identifier risk + the run drill-in pivot (F3) all excluded.
    assert not ({"message_text", "payload", "run_id", "dedup_key"} & set(row))


def test_vtr_role_cannot_read_raw_tables(substrate):
    """The GUARANTEE: the endpoint's role (app_vtr_role) is DENIED on raw escalations + tenant_alerts."""
    from orchestrator.privacy.vtr import vtr_connection

    with vtr_connection() as conn, conn.cursor() as cur:
        for tbl in ("escalations", "tenant_alerts"):
            with pytest.raises(pg_errors.InsufficientPrivilege):
                cur.execute(f"SELECT 1 FROM {tbl} LIMIT 1")  # noqa: S608 — fixed allowlist
            cur.execute("ROLLBACK")
        # views ARE readable (the only door)
        cur.execute("SELECT count(*) FROM vtr_escalations")
        cur.execute("SELECT count(*) FROM vtr_tenant_alerts")


def test_vtr_read_bad_secret_403(substrate, monkeypatch):
    _env(monkeypatch)
    from fastapi import HTTPException

    from orchestrator.api.ops_resolve import VtrReadBody, vtr_escalations_read

    with pytest.raises(HTTPException) as exc:
        vtr_escalations_read(
            VtrReadBody(operator_id="x"), x_internal_secret="wrong", x_operator_jwt="x"
        )
    assert exc.value.status_code == 403


def test_vtr_read_caps_limit(substrate, monkeypatch):
    """Early-review F4: seed > cap so count==200 actually proves the clamp (not a vacuous <=200)."""
    _env(monkeypatch)
    from psycopg.rows import dict_row

    from orchestrator.api.ops_resolve import VtrReadBody, vtr_escalations_read

    with psycopg.connect(substrate, autocommit=True, row_factory=dict_row) as conn:
        tid = str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-360 cap', 'founding', 'paid_active') RETURNING id"
        ).fetchone()["id"])
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO escalations (tenant_id, kind, severity, status, route) "
                "VALUES (%s, 'how_to_gap', 'medium', 'open', 'vtr')",
                [(tid,)] * 201,
            )
    op = str(uuid4())
    _assign(substrate, op, tid)  # mig-134 scoping: row-asserting read needs the assignment
    out = vtr_escalations_read(
        VtrReadBody(operator_id=op, limit=9999), x_internal_secret=_SECRET, x_operator_jwt=_op_jwt(op)
    )
    assert out["count"] == 200  # clamped to _VTR_PAGE_CAP despite 201 eligible + limit=9999


def test_vtr_read_operator_id_mismatch_403(substrate, monkeypatch):
    """Early-review F5: a valid JWT whose operator_id != body.operator_id → 403."""
    _env(monkeypatch)
    from fastapi import HTTPException

    from orchestrator.api.ops_resolve import VtrReadBody, vtr_escalations_read

    with pytest.raises(HTTPException) as exc:
        vtr_escalations_read(
            VtrReadBody(operator_id="claimed-A"),
            x_internal_secret=_SECRET,
            x_operator_jwt=_op_jwt("actually-B"),  # signed for a different operator
        )
    assert exc.value.status_code == 403


def test_vtr_read_invalid_jwt_403(substrate, monkeypatch):
    """Early-review F5: a malformed / wrong-secret JWT → 403."""
    _env(monkeypatch)
    from fastapi import HTTPException

    from orchestrator.api.ops_resolve import VtrReadBody, vtr_monitoring_read

    with pytest.raises(HTTPException) as exc:
        vtr_monitoring_read(
            VtrReadBody(operator_id="x"),
            x_internal_secret=_SECRET,
            x_operator_jwt="not-a-jwt",
        )
    assert exc.value.status_code == 403


# --- VT-405 Part A: vtr_tenant_profile -----------------------------------------------------------


def _seed_profile_tenant(dsn: str) -> str:
    from psycopg.rows import dict_row

    # Unique number (tenants.whatsapp_number is UNIQUE) but a fixed last-4 (3598) for the mask assert.
    phone = f"+919{uuid4().int % 10**5:05d}3598"
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        tid = str(
            conn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase, business_type, "
                "whatsapp_number, locality, city_tier, owner_contact, signed_up_at) "
                "VALUES ('VT-405 Biz','founding','trial','retail',%s,'Mumbai',"
                "'tier_1','Asha Owner', now()) RETURNING id",
                (phone,),
            ).fetchone()["id"]
        )
        conn.execute(
            "INSERT INTO business_profile_draft (tenant_id, attributes, provenance) VALUES "
            "(%s, '{\"about\":\"a shop\",\"rating\":4.7}'::jsonb, "
            "'{\"about\":{\"source\":\"website\"},\"rating\":{\"source\":\"gbp\"}}'::jsonb)",
            (tid,),
        )
    return tid


def test_vtr_tenant_profile_scoped_masked_non_pii(substrate, monkeypatch):
    """The profile read returns EXACTLY the view columns: WhatsApp masked to last-4 (raw number
    absent), owner name + discovered draft present, confirmation keys-only. Assignment-scoped."""
    _env(monkeypatch)
    from orchestrator.api.ops_vtr_console import VtrTenantProfileBody, vtr_tenant_profile

    tid = _seed_profile_tenant(substrate)
    op = str(uuid4())
    _assign(substrate, op, tid)
    out = vtr_tenant_profile(
        VtrTenantProfileBody(operator_id=op, tenant_id=tid),
        x_internal_secret=_SECRET,
        x_operator_jwt=_op_jwt(op),
    )
    p = out["profile"]
    assert p is not None
    assert p["business_name"] == "VT-405 Biz"
    assert p["whatsapp_last4"] == "3598"  # masked at the view
    assert "whatsapp_number" not in p  # the raw PII column never surfaces
    assert p["owner_name"] == "Asha Owner"
    assert p["draft_attributes"]["rating"] == 4.7
    assert p["draft_provenance"]["about"]["source"] == "website"
    assert set(p) == {
        "tenant_id", "business_name", "phase", "plan_tier", "business_type", "locality",
        "city_tier", "language_preference", "preferred_language", "signed_up_at", "trial_started_at",
        "phase_entered_at", "owner_name", "whatsapp_last4", "draft_attributes", "draft_provenance",
        "draft_created_at", "draft_updated_at", "onboarding_status", "onboarding_queue_len",
        "confirmed_fields",
    }


def test_vtr_tenant_profile_unassigned_403(substrate, monkeypatch):
    """An operator with no active assignment to the tenant is denied at the gate (require_vtr_action
    operator_assigned, fail-closed + audited) — defense-in-depth ABOVE the view's own scope predicate."""
    _env(monkeypatch)
    from fastapi import HTTPException

    from orchestrator.api.ops_vtr_console import VtrTenantProfileBody, vtr_tenant_profile

    tid = _seed_profile_tenant(substrate)
    op = str(uuid4())  # deliberately NOT assigned
    with pytest.raises(HTTPException) as exc:
        vtr_tenant_profile(
            VtrTenantProfileBody(operator_id=op, tenant_id=tid),
            x_internal_secret=_SECRET,
            x_operator_jwt=_op_jwt(op),
        )
    assert exc.value.status_code == 403
