"""mig 173 — LLM $-budget gate + VTR-controlled caps: acceptance battery.

Two units under one real-migrated-PG substrate (mirrors ``test_ops_vtr_console.py``):

  * ``orchestrator.llm.budget_gate.check_llm_budget`` — NULL caps → ok; soft/hard thresholds;
    FAIL-OPEN on a read error; once-per-period tm_audit notification; the app_role SELECT-only
    posture (a tenant connection cannot UPDATE ``tenant_llm_limits``).
  * ``ops_vtr_console`` LLM endpoints — set-tenant-limits (assigned-VTR ``_gate``, IDOR deny audit),
    set-global-limits (exception-tier only), usage read (tenant vs platform).

Direct handler calls with EVERY header/query param passed explicitly (the FieldInfo-default trap).
Seeds via direct autocommit psycopg (service role, RLS bypassed at seed).
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")
pytest.importorskip("langgraph")
pytest.importorskip("fastapi")
pytest.importorskip("jwt")

import jwt as pyjwt  # noqa: E402
import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from orchestrator.api import ops_vtr_console as console  # noqa: E402
from orchestrator.db import tenant_connection  # noqa: E402

# budget_gate lives under orchestrator.llm, whose package __init__ re-exports provider.py — a
# SIBLING module owned by another builder in this parallel batch that may not be landed yet.
# budget_gate.py itself has NO provider dependency, so if the package import trips on the missing
# sibling, load the module directly by file path (identical code, bypasses the package __init__).
# Once provider lands, the normal package import wins.
try:
    from orchestrator.llm import budget_gate as _bg
except Exception:  # noqa: BLE001 — parallel-build gap only
    import importlib.util
    import pathlib

    _p = pathlib.Path(__file__).resolve().parents[3] / "src/orchestrator/llm/budget_gate.py"
    _spec = importlib.util.spec_from_file_location("orchestrator_llm_budget_gate_isolated", _p)
    _bg = importlib.util.module_from_spec(_spec)  # type: ignore[assignment]
    _spec.loader.exec_module(_bg)  # type: ignore[union-attr]

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — mig173 LLM budget tests skipped",
)
pytestmark = requires_db

_TEST_INTERNAL_SECRET = "unit-test-internal-not-a-secret"
_TEST_JWT_KEY = "unit-test-jwt-signing-key-not-a-secret-0000"


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        _reset_global(dsn)  # leave the singleton cap-free for any later module
        shutdown_dbos()


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERNAL_API_SECRET", _TEST_INTERNAL_SECRET)
    monkeypatch.setenv("OPERATOR_JWT_SECRET", _TEST_JWT_KEY)
    monkeypatch.delenv("FAZAL_OWNER_UUID", raising=False)


def _op_jwt(operator_id: str, *, secret: str = _TEST_JWT_KEY) -> str:
    now = int(time.time())
    return pyjwt.encode(
        {"operator_claim": True, "operator_id": operator_id, "aud": "authenticated",
         "iat": now, "exp": now + 300},
        secret, algorithm="HS256",
    )


# --- seeding helpers (direct service-role connection) -----------------------


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, whatsapp_number) "
            "VALUES ('mig173 budget test', 'founding', 'trial', now(), 'restaurant', %s) "
            "RETURNING id",
            (f"+9198{uuid4().int % 10**8:08d}",),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _assign(dsn: str, operator_id: str, tenant: UUID) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO operator_assignments (operator_id, tenant_id) VALUES (%s, %s)",
            (operator_id, str(tenant)),
        )


def _seed_event(
    dsn: str, tenant: UUID | None, *, cost: float, tokens_in: int = 0, tokens_out: int = 0,
    agent: str = "sales_recovery",
) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO llm_call_events "
            "  (tenant_id, agent, call_site, provider, model, cost_usd, tokens_in, tokens_out) "
            "VALUES (%s, %s, 'test', 'anthropic', 'claude-sonnet-5', %s, %s, %s)",
            (str(tenant) if tenant else None, agent, cost, tokens_in, tokens_out),
        )


def _seed_tenant_limit(
    dsn: str, tenant: UUID, *, cost: float | None = None, tin: int | None = None,
    tout: int | None = None, soft_pct: int = 80, enabled: bool = True, set_by: str = "seed",
) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_llm_limits (tenant_id, max_cost_usd_month, max_tokens_in_month, "
            "  max_tokens_out_month, soft_pct, enabled, set_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant_id) DO UPDATE SET "
            "  max_cost_usd_month = EXCLUDED.max_cost_usd_month, "
            "  max_tokens_in_month = EXCLUDED.max_tokens_in_month, "
            "  max_tokens_out_month = EXCLUDED.max_tokens_out_month, "
            "  soft_pct = EXCLUDED.soft_pct, enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by",
            (str(tenant), cost, tin, tout, soft_pct, enabled, set_by),
        )


def _set_global(
    dsn: str, *, day: float | None = None, month: float | None = None, soft_pct: int = 80,
    enabled: bool = True,
) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO global_llm_limits (id, max_cost_usd_day, max_cost_usd_month, soft_pct, "
            "  enabled, set_by) VALUES (true, %s, %s, %s, %s, 'test') "
            "ON CONFLICT (id) DO UPDATE SET "
            "  max_cost_usd_day = EXCLUDED.max_cost_usd_day, "
            "  max_cost_usd_month = EXCLUDED.max_cost_usd_month, "
            "  soft_pct = EXCLUDED.soft_pct, enabled = EXCLUDED.enabled, set_by = 'test'",
            (day, month, soft_pct, enabled),
        )


def _reset_global(dsn: str) -> None:
    """Return the singleton to the migration default (no caps) so it never leaks into another
    test's tenant-leg assertion (the global leg sums ALL tenants for the month)."""
    _set_global(dsn, day=None, month=None, soft_pct=80, enabled=True)


def _limit_row(dsn: str, tenant: UUID) -> dict[str, Any] | None:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT * FROM tenant_llm_limits WHERE tenant_id = %s", (str(tenant),)
        ).fetchone()


def _global_row(dsn: str) -> dict[str, Any] | None:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        return conn.execute("SELECT * FROM global_llm_limits WHERE id = true").fetchone()


def _audit_rows(dsn: str, action: str, operator: str) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT operator_id, tenant_id, action, target_kind, target_id, detail "
            "FROM ops_audit WHERE action = %s AND operator_id = %s ORDER BY created_at",
            (action, operator),
        ).fetchall()


def _tm_audit_rows(dsn: str, tenant: UUID, event_kind: str) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT actor, event_kind, severity, status FROM tm_audit_log "
            "WHERE tenant_id = %s AND event_kind = %s", (str(tenant), event_kind)
        ).fetchall()


def _check(tenant: UUID, agent: str = "sales_recovery") -> str:
    """check_llm_budget with the TTL cache dropped first (each assertion reads live DB state)."""
    _bg.reset_budget_cache(tenant)
    return _bg.check_llm_budget(str(tenant), agent)


# ===========================================================================
# 1. budget_gate.severity — the pure threshold function
# ===========================================================================


def test_severity_thresholds() -> None:
    assert _bg.severity(999.0, None, 80) == "ok"     # NULL cap = no cap
    assert _bg.severity(10.0, 100.0, 80) == "ok"     # 10%
    assert _bg.severity(79.9, 100.0, 80) == "ok"     # just under soft
    assert _bg.severity(80.0, 100.0, 80) == "soft"   # exactly soft_pct
    assert _bg.severity(99.9, 100.0, 80) == "soft"
    assert _bg.severity(100.0, 100.0, 80) == "hard"  # at cap
    assert _bg.severity(150.0, 100.0, 80) == "hard"
    assert _bg.severity(0.0, 0, 80) == "hard"        # a 0 cap is a deliberate freeze


# ===========================================================================
# 2. check_llm_budget — the tenant leg
# ===========================================================================


def test_check_no_limits_is_ok(substrate) -> None:
    """No tenant_llm_limits row + no global cap → ok (the record-only default)."""
    tenant = _new_tenant(substrate.dsn)
    _seed_event(substrate.dsn, tenant, cost=5.0)
    assert _check(tenant) == "ok"


def test_check_soft_then_hard(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_tenant_limit(dsn, tenant, cost=10.00, soft_pct=80)

    _seed_event(dsn, tenant, cost=5.0)      # 50% → ok
    assert _check(tenant) == "ok"
    _seed_event(dsn, tenant, cost=3.5)      # 85% → soft
    assert _check(tenant) == "soft"
    _seed_event(dsn, tenant, cost=1.5)      # 100% → hard
    assert _check(tenant) == "hard"


def test_check_token_cap_hard(substrate) -> None:
    """A token cap (not just cost) also trips the gate."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_tenant_limit(dsn, tenant, tin=1000, soft_pct=80)
    _seed_event(dsn, tenant, cost=0.0, tokens_in=1200)
    assert _check(tenant) == "hard"


def test_check_disabled_limit_is_ok(substrate) -> None:
    """enabled=false → the caps do not enforce (ok) even when usage exceeds them."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_tenant_limit(dsn, tenant, cost=1.00, enabled=False)
    _seed_event(dsn, tenant, cost=50.0)
    assert _check(tenant) == "ok"


def test_check_fails_open_on_read_error(substrate, monkeypatch) -> None:
    """A read error in the tenant leg fails OPEN (ok) even though usage is over the hard cap —
    availability over enforcement; the OTHER leg (global, no cap here) is unaffected."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_tenant_limit(dsn, tenant, cost=1.00)
    _seed_event(dsn, tenant, cost=99.0)  # would be hard on a successful read

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("simulated tenant_connection failure")

    monkeypatch.setattr("orchestrator.db.tenant_connection", _boom)
    assert _check(tenant) == "ok"


def test_notify_once_per_period(substrate) -> None:
    """A hard verdict emits ONE llm_budget_hard tm_audit row (actor=platform); a re-check the same
    period does NOT duplicate it (the tm_audit dedup, not the VT-619 stamp columns)."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_tenant_limit(dsn, tenant, cost=10.00)
    _seed_event(dsn, tenant, cost=12.0)

    assert _check(tenant) == "hard"
    rows = _tm_audit_rows(dsn, tenant, "llm_budget_hard")
    assert len(rows) == 1
    assert rows[0]["actor"] == "platform"
    assert rows[0]["status"] == "blocked"

    assert _check(tenant) == "hard"  # cache dropped by _check → a real re-read
    assert len(_tm_audit_rows(dsn, tenant, "llm_budget_hard")) == 1  # deduped, still one


# ===========================================================================
# 3. check_llm_budget — the global (platform) leg
# ===========================================================================


def test_global_leg_hard(substrate) -> None:
    """A platform-wide cost cap trips the gate for a tenant with NO per-tenant cap. Restored in
    finally so the tiny global cap never leaks into another test's tenant-leg assertion."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)  # deliberately no tenant_llm_limits row
    try:
        _seed_event(dsn, tenant, cost=1.0)
        _set_global(dsn, month=0.01, enabled=True)  # platform month cost ≫ 0.01 → hard
        assert _check(tenant) == "hard"
    finally:
        _reset_global(dsn)
    # With the global cap gone, the same tenant (no per-tenant cap) is ok again.
    assert _check(tenant) == "ok"


# ===========================================================================
# 4. RLS posture — the runtime enforces but can never self-edit its caps
# ===========================================================================


def test_app_role_cannot_update_tenant_llm_limits(substrate) -> None:
    """The runtime may READ its caps (to enforce) but can never SELF-EDIT them. mig 173 defines
    only a FOR SELECT policy under FORCE ROW LEVEL SECURITY — so even though app_role carries a
    blanket table-level UPDATE grant, an UPDATE from a tenant connection matches ZERO rows (no
    UPDATE policy → nothing is writable) and the cap value is untouched. This is the same
    row-level protection agent_cost_limits relies on (mig 171)."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_tenant_limit(dsn, tenant, cost=10.00)

    with tenant_connection(tenant) as conn, conn.cursor() as cur:
        cur.execute("SELECT max_cost_usd_month FROM tenant_llm_limits WHERE tenant_id = %s",
                    (str(tenant),))
        assert cur.fetchone() is not None  # SELECT works (the runtime enforces on this read)
        cur.execute("UPDATE tenant_llm_limits SET max_cost_usd_month = 0 WHERE tenant_id = %s",
                    (str(tenant),))
        assert cur.rowcount == 0  # FORCE RLS + no UPDATE policy → no row is writable

    row = _limit_row(dsn, tenant)
    assert row is not None and float(row["max_cost_usd_month"]) == 10.0  # cap untouched


# ===========================================================================
# 5. Endpoint — set a tenant's limits (assigned-VTR gate, IDOR deny audit)
# ===========================================================================


def _set_limits(op: str, tenant: UUID, *, jwt_for: str | None = "MINT", **caps: Any) -> dict[str, Any]:
    return console.vtr_llm_limits(
        console.VtrLlmLimitsBody(operator_id=op, tenant_id=str(tenant), **caps),
        x_internal_secret=_TEST_INTERNAL_SECRET,
        x_operator_jwt=(_op_jwt(op) if jwt_for == "MINT" else jwt_for),
    )


def test_set_limits_success_and_audit(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)
    _assign(dsn, op, tenant)

    out = _set_limits(op, tenant, max_cost_usd_month=25.0, soft_pct=75, enabled=True)
    assert out == {"ok": True, "tenant_id": str(tenant), "enabled": True}

    row = _limit_row(dsn, tenant)
    assert row is not None
    assert float(row["max_cost_usd_month"]) == 25.0
    assert row["soft_pct"] == 75
    assert row["set_by"] == op  # the VERIFIED operator id, server-side (never a client field)

    audits = _audit_rows(dsn, "vtr_llm_limits_set", op)
    assert len(audits) == 1
    assert str(audits[0]["tenant_id"]) == str(tenant)
    assert "soft_pct=75" in (audits[0]["detail"] or "")


def test_set_limits_unassigned_403_and_deny_audited(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)  # NO assignment

    with pytest.raises(HTTPException) as exc:
        _set_limits(op, tenant, max_cost_usd_month=5.0)
    assert exc.value.status_code == 403
    assert len(_audit_rows(dsn, "llm_limits_set_denied", op)) == 1
    assert _limit_row(dsn, tenant) is None  # nothing written


def test_set_limits_no_jwt_403(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)
    _assign(dsn, op, tenant)
    with pytest.raises(HTTPException) as exc:
        _set_limits(op, tenant, max_cost_usd_month=5.0, jwt_for=None)
    assert exc.value.status_code == 403
    assert _limit_row(dsn, tenant) is None


def test_set_limits_bad_soft_pct_400(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)
    _assign(dsn, op, tenant)
    for bad in (0, 101, -5):
        with pytest.raises(HTTPException) as exc:
            _set_limits(op, tenant, max_cost_usd_month=5.0, soft_pct=bad)
        assert exc.value.status_code == 400


# ===========================================================================
# 6. Endpoint — set the GLOBAL singleton (exception-tier only)
# ===========================================================================


def _set_global_ep(op: str, *, jwt_for: str | None = "MINT", **caps: Any) -> dict[str, Any]:
    return console.vtr_llm_limits_global(
        console.VtrLlmLimitsGlobalBody(operator_id=op, **caps),
        x_internal_secret=_TEST_INTERNAL_SECRET,
        x_operator_jwt=(_op_jwt(op) if jwt_for == "MINT" else jwt_for),
    )


def test_set_global_non_exception_operator_403(substrate, monkeypatch) -> None:
    """A valid, JWT-holding operator who is NOT Fazal (exception tier) cannot set the platform cap."""
    _env(monkeypatch)  # FAZAL_OWNER_UUID unset → exception tier closed
    op = str(uuid4())
    with pytest.raises(HTTPException) as exc:
        _set_global_ep(op, max_cost_usd_month=1000.0)
    assert exc.value.status_code == 403


def test_set_global_fazal_success_and_audit(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    dsn = substrate.dsn
    fazal = str(uuid4())
    monkeypatch.setenv("FAZAL_OWNER_UUID", fazal)
    try:
        out = _set_global_ep(fazal, max_cost_usd_day=100.0, max_cost_usd_month=2000.0, soft_pct=90)
        assert out == {"ok": True, "enabled": True}

        row = _global_row(dsn)
        assert row is not None
        assert float(row["max_cost_usd_day"]) == 100.0
        assert float(row["max_cost_usd_month"]) == 2000.0
        assert row["soft_pct"] == 90
        assert row["set_by"] == fazal

        audits = _audit_rows(dsn, "vtr_llm_limits_global_set", fazal)
        assert len(audits) == 1
        assert audits[0]["tenant_id"] is None  # platform-scoped, not a tenant action
        assert audits[0]["target_kind"] == "platform"
    finally:
        _reset_global(dsn)


# ===========================================================================
# 7. Endpoint — usage read (tenant vs platform)
# ===========================================================================


def test_usage_tenant_scope(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)
    _assign(dsn, op, tenant)
    _seed_tenant_limit(dsn, tenant, cost=10.00, soft_pct=80)
    _seed_event(dsn, tenant, cost=8.5, tokens_in=100, tokens_out=50)

    out = console.vtr_llm_usage(
        operator_id=op, tenant_id=str(tenant),
        x_internal_secret=_TEST_INTERNAL_SECRET, x_operator_jwt=_op_jwt(op),
    )
    assert out["scope"] == "tenant"
    assert out["tenant_id"] == str(tenant)
    assert out["month"]["cost_usd"] == pytest.approx(8.5)
    assert out["month"]["tokens_in"] == 100
    assert out["month"]["calls"] == 1
    assert out["limits"]["max_cost_usd_month"] == pytest.approx(10.0)
    assert out["state"] == "soft"  # 85% of cost cap


def test_usage_tenant_unassigned_403(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)  # NOT assigned
    with pytest.raises(HTTPException) as exc:
        console.vtr_llm_usage(
            operator_id=op, tenant_id=str(tenant),
            x_internal_secret=_TEST_INTERNAL_SECRET, x_operator_jwt=_op_jwt(op),
        )
    assert exc.value.status_code == 403


def test_usage_platform_non_exception_403(substrate, monkeypatch) -> None:
    """The no-tenant platform view is cross-tenant → exception-tier (Fazal) only."""
    _env(monkeypatch)
    op = str(uuid4())
    with pytest.raises(HTTPException) as exc:
        console.vtr_llm_usage(
            operator_id=op, tenant_id=None,
            x_internal_secret=_TEST_INTERNAL_SECRET, x_operator_jwt=_op_jwt(op),
        )
    assert exc.value.status_code == 403


def test_usage_platform_fazal_totals_and_top(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    dsn = substrate.dsn
    fazal = str(uuid4())
    monkeypatch.setenv("FAZAL_OWNER_UUID", fazal)
    tenant = _new_tenant(dsn)
    _seed_event(dsn, tenant, cost=7.25, tokens_in=10, tokens_out=5)

    out = console.vtr_llm_usage(
        operator_id=fazal, tenant_id=None,
        x_internal_secret=_TEST_INTERNAL_SECRET, x_operator_jwt=_op_jwt(fazal),
    )
    assert out["scope"] == "platform"
    # Cross-tenant month total includes this + every other test's events → at least our contribution.
    assert out["month_cost_usd"] >= 7.25
    assert out["day_cost_usd"] >= 7.25
    assert isinstance(out["top_tenants"], list)
    assert len(out["top_tenants"]) <= 10
    assert "state" in out and "limits" in out
