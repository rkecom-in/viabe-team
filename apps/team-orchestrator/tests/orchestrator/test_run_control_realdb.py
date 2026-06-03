"""VT-300 — VTR live run-control endpoint (real Postgres). The enforcement-leg authz.

Proves the endpoint re-derives the run's tenant server-side + re-checks operator_assignments
fail-CLOSED (team-web auth is fail-open here), writes run_controls + ops_audit on success, and
AUDITS the deny. The IDOR negatives the adversarial review demanded: an UNASSIGNED operator (and,
by construction, any client-claimed tenant — there is NO tenant param) is refused. Gated on
DATABASE_URL + dbos; CL-422 synthetic.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("fastapi")

import psycopg  # noqa: E402
from fastapi import HTTPException  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-300 run-control canary skipped",
)

_SECRET = "vt300-test-secret"


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ["INTERNAL_API_SECRET"] = _SECRET
    os.environ.pop("FAZAL_OWNER_UUID", None)  # no break-glass in these tests
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT300', 'founding', 'paid_active') RETURNING id"
        ).fetchone()[0])


def _run(dsn: str, tenant: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, status, run_type) "
            "VALUES (%s, 'running', 'orchestrator') RETURNING id",
            (tenant,),
        ).fetchone()[0])


def _operator(dsn: str) -> str:
    op = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO operator_allowlist (user_id) VALUES (%s)", (op,))
    return op


def _assign(dsn: str, op: str, tenant: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO operator_assignments (operator_id, tenant_id) VALUES (%s, %s)",
            (op, tenant),
        )


def _body(**kw):  # type: ignore[no-untyped-def]
    from orchestrator.api.ops_runcontrol import RunControlBody

    return RunControlBody(**kw)


def _count(dsn: str, table: str, run_id: str) -> int:
    col = "target_id" if table == "ops_audit" else "run_id"
    with psycopg.connect(dsn, autocommit=True) as conn:
        return int(conn.execute(
            f"SELECT count(*) FROM {table} WHERE {col} = %s", (run_id,)
        ).fetchone()[0])


def test_assigned_operator_control_recorded_and_audited(substrate):
    from orchestrator.api.ops_runcontrol import run_control

    tenant = _tenant(substrate)
    run = _run(substrate, tenant)
    op = _operator(substrate)
    _assign(substrate, op, tenant)

    out = run_control(
        _body(run_id=run, operator_id=op, control_type="pause"), x_internal_secret=_SECRET
    )
    assert out["ok"] is True
    assert out["tenant_id"] == tenant          # server-derived
    assert _count(substrate, "run_controls", run) == 1
    # ops_audit gained a control_executed row (server-resolved tenant)
    with psycopg.connect(substrate, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        audit = conn.execute(
            "SELECT action, tenant_id FROM ops_audit WHERE target_id = %s AND action='control_executed'",
            (run,),
        ).fetchone()
    assert audit is not None
    assert str(audit["tenant_id"]) == tenant


def test_unassigned_operator_refused_and_deny_audited_no_write(substrate):
    from orchestrator.api.ops_runcontrol import run_control

    tenant = _tenant(substrate)
    run = _run(substrate, tenant)
    op = _operator(substrate)  # NOT assigned to tenant

    with pytest.raises(HTTPException) as exc:
        run_control(_body(run_id=run, operator_id=op, control_type="override"), x_internal_secret=_SECRET)
    assert exc.value.status_code == 403
    # No control written; the deny IS audited.
    assert _count(substrate, "run_controls", run) == 0
    with psycopg.connect(substrate, autocommit=True) as conn:
        denied = conn.execute(
            "SELECT count(*) FROM ops_audit WHERE target_id = %s AND action='control_denied'", (run,)
        ).fetchone()[0]
    assert denied == 1


def test_cross_tenant_impossible_no_tenant_param(substrate):
    """An operator assigned to tenant A cannot control a run on tenant B — there is no tenant
    param; the tenant is derived from the run, and the assignment check is against THAT."""
    from orchestrator.api.ops_runcontrol import run_control

    tenant_a, tenant_b = _tenant(substrate), _tenant(substrate)
    run_b = _run(substrate, tenant_b)  # run belongs to tenant B
    op = _operator(substrate)
    _assign(substrate, op, tenant_a)  # operator assigned only to A

    with pytest.raises(HTTPException) as exc:
        run_control(_body(run_id=run_b, operator_id=op, control_type="steer"), x_internal_secret=_SECRET)
    assert exc.value.status_code == 403
    assert _count(substrate, "run_controls", run_b) == 0


def test_run_not_found_404(substrate):
    from orchestrator.api.ops_runcontrol import run_control

    op = _operator(substrate)
    with pytest.raises(HTTPException) as exc:
        run_control(_body(run_id=str(uuid4()), operator_id=op, control_type="pause"), x_internal_secret=_SECRET)
    assert exc.value.status_code == 404


def test_bad_secret_and_bad_control_type(substrate):
    from orchestrator.api.ops_runcontrol import run_control

    tenant = _tenant(substrate)
    run = _run(substrate, tenant)
    op = _operator(substrate)
    _assign(substrate, op, tenant)
    with pytest.raises(HTTPException) as exc1:
        run_control(_body(run_id=run, operator_id=op, control_type="pause"), x_internal_secret="wrong")
    assert exc1.value.status_code == 401
    with pytest.raises(HTTPException) as exc2:
        run_control(_body(run_id=run, operator_id=op, control_type="delete_everything"), x_internal_secret=_SECRET)
    assert exc2.value.status_code == 400


def test_directive_pii_scrubbed(substrate):
    from orchestrator.api.ops_runcontrol import run_control

    tenant = _tenant(substrate)
    run = _run(substrate, tenant)
    op = _operator(substrate)
    _assign(substrate, op, tenant)
    run_control(
        _body(run_id=run, operator_id=op, control_type="steer", directive="call +919812345678 now"),
        x_internal_secret=_SECRET,
    )
    with psycopg.connect(substrate, autocommit=True) as conn:
        directive = conn.execute(
            "SELECT directive FROM run_controls WHERE run_id = %s", (run,)
        ).fetchone()[0]
    assert "9812345678" not in (directive or "")  # phone digits scrubbed


def test_consume_pending_control_claims_oldest_once(substrate):
    """Effecting leg: consume_pending_control atomically claims the oldest 'requested' control,
    marks it consumed, and never double-applies."""
    from orchestrator.api.ops_runcontrol import run_control
    from orchestrator.graph import get_pool
    from orchestrator.run_control_handler import consume_pending_control, should_hold_send

    tenant = _tenant(substrate)
    run = _run(substrate, tenant)
    op = _operator(substrate)
    _assign(substrate, op, tenant)
    # Two controls queued on the run (pause then override).
    run_control(_body(run_id=run, operator_id=op, control_type="pause"), x_internal_secret=_SECRET)
    run_control(_body(run_id=run, operator_id=op, control_type="override"), x_internal_secret=_SECRET)

    pool = get_pool()
    first = consume_pending_control(run, pool=pool)
    assert first is not None and first["control_type"] == "pause"   # oldest first
    assert should_hold_send(first) is True
    second = consume_pending_control(run, pool=pool)
    assert second is not None and second["control_type"] == "override"
    third = consume_pending_control(run, pool=pool)
    assert third is None                                            # nothing left → no hold
    assert should_hold_send(third) is False
    # both rows now consumed (none left 'requested')
    with psycopg.connect(substrate, autocommit=True) as conn:
        pending = conn.execute(
            "SELECT count(*) FROM run_controls WHERE run_id = %s AND status='requested'", (run,)
        ).fetchone()[0]
    assert pending == 0
