"""VT-377 — two-operator VTR assignment-scoping isolation suite (real Postgres).

The CL-426 second-VTR precondition, proven by TEST not by a second human (the
capability-complete boundary): with the mig-134 ``app.vtr_operator_id`` GUC set to
operator A, EVERY one of the nine ``app_vtr_role`` views returns ONLY A's assigned
tenant; with it set to B, only B's; unset OR empty-string ⇒ ZERO rows (fail-closed,
NEVER a cast error — the ruling's predicate fix); ``SET ROLE app_vtr_admin_role`` ⇒
BOTH (the admin tier is the role, not a bypass flag); a REVOKED assignment (A→T_B with
``unassigned_at`` set) grants nothing.

The substrate (mig-134 ``app_vtr_operator()`` helper + the nine assignment-scoped views +
the per-caller GUC plumbing in ``privacy/vtr.py``) lands CONCURRENTLY as B1. This suite is
written against the CONTRACT: the migrate fixture applies whatever is present (mig-134
included once B1 lands), so the view-scoping legs go GREEN the moment B1 merges and FAIL
loudly until then (never a false pass). The ``_mig134_present`` probe records which legs are
substrate-gated so the integrator re-run is unambiguous.

The API legs (operator A denied B's run/tenant across timeline / programs / pause /
override / rerun, tenant-scoped AND row-targeted) ride the existing
``test_ops_run_control.py`` TestClient idiom — the assignment gate is
``operator_assignments`` (mig-072), which exists today; those legs pass NOW.

Gated on DATABASE_URL + dbos (CL-422 — synthetic data only). Unique operators/tenants per
test (uuid suffixes) so a recycled DB never collides.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("fastapi")
pytest.importorskip("jwt")

import psycopg  # noqa: E402
from psycopg import errors as pg_errors  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-377 assignment-scoping tests skipped",
)

# The NINE app_vtr_role views the CL-426 second-VTR precondition scopes (the build
# contract's 9-view census superseding the Gap-6 five). Pinned here so a view added or
# dropped from the assignment-scoping migration trips this suite, not just review. Every
# one of these exposes ``tenant_id``.
_SCOPED_VIEWS = (
    "vtr_customers",
    "vtr_escalations",
    "vtr_tenant_alerts",
    "vtr_business_plan",
    "vtr_plan_history",
    "vtr_agent_autonomy",
    "vtr_draft_batches",
    "vtr_step_timeline",
    "vtr_workflow_controls",
)

_VTR_OPERATOR_GUC = "app.vtr_operator_id"


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations (mig-134 included once B1 lands) + launch DBOS + bootstrap the
    vtr_ref_secret the vtr_customers view HMACs against. Mirrors
    test_run_control_realdb.py."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    # vtr_customers' projection HMACs each id against this singleton secret — without it the
    # view raises on a NULL secret rather than returning rows.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO vtr_ref_secret (id, secret) VALUES (true, %s) "
            "ON CONFLICT (id) DO NOTHING",
            ("vt377-test-ref-secret",),
        )
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


def _mig134_present(dsn: str) -> bool:
    """True once mig-134 has defined the ``app_vtr_operator()`` helper. The scoping legs
    HARD-ASSERT on this (gate C2) — a database without the migration fails the isolation
    battery loudly instead of xfail-neutering it."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        return (
            conn.execute(
                "SELECT to_regprocedure('app_vtr_operator()') IS NOT NULL"
            ).fetchone()[0]
            is True
        )


# --- seed helpers (superuser — RLS bypassed at seed time, like every realdb suite) -----


def _operator() -> str:
    return str(uuid4())


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase) "
                "VALUES (%s, 'founding', 'paid_active') RETURNING id",
                (f"VT377 {uuid4().hex[:8]}",),
            ).fetchone()[0]
        )


def _assign(dsn: str, operator_id: str, tenant_id: str, *, revoked: bool = False) -> None:
    """An operator_assignments row (mig-072). ``revoked`` stamps ``unassigned_at`` so the
    predicate's ``unassigned_at IS NULL`` leg excludes it."""
    sql = (
        "INSERT INTO operator_assignments (operator_id, tenant_id, unassigned_at) "
        "VALUES (%s, %s, now())"
        if revoked
        else "INSERT INTO operator_assignments (operator_id, tenant_id) VALUES (%s, %s)"
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql, (operator_id, tenant_id))


def _seed_all_views(dsn: str, tenant_id: str) -> None:
    """Seed ONE row into every base table behind the nine scoped views for ``tenant_id`` —
    so EACH view returns exactly one row for this tenant (and the scoping assertion is per
    view, not just one of them). Minimal valid shapes (the scoping is what's under test, not
    the projection)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        # vtr_customers ← customers
        conn.execute(
            "INSERT INTO customers (tenant_id, display_name, source) "
            "VALUES (%s, %s, 'test')",
            (tenant_id, f"Cust {uuid4().hex[:6]}"),
        )
        # vtr_escalations ← escalations
        conn.execute(
            "INSERT INTO escalations (tenant_id, kind, severity, status) "
            "VALUES (%s, 'agent_escalated', 'medium', 'open')",
            (tenant_id,),
        )
        # vtr_tenant_alerts ← tenant_alerts (JOIN tenants — present)
        conn.execute(
            "INSERT INTO tenant_alerts (tenant_id, trigger_kind, severity, dedup_key, "
            "message_text) VALUES (%s, 'hard_limit', 'warning', %s, 'x')",
            (tenant_id, f"dk-{uuid4().hex[:8]}"),
        )
        # vtr_business_plan + vtr_plan_history ← business_plan
        conn.execute(
            "INSERT INTO business_plan (tenant_id, version, generated_by) "
            "VALUES (%s, 1, 'test')",
            (tenant_id,),
        )
        # vtr_agent_autonomy ← tenant_agent_autonomy (JOIN tenants — present)
        conn.execute(
            "INSERT INTO tenant_agent_autonomy (tenant_id, agent) VALUES (%s, 'sales_recovery')",
            (tenant_id,),
        )
        # vtr_draft_batches ← agent_draft_batches (FK to agent_work_items)
        work_item = str(
            conn.execute(
                "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
                "VALUES (%s, %s, 'sales_recovery', 'awaiting_approval') RETURNING id",
                (tenant_id, f"item-{uuid4().hex[:6]}"),
            ).fetchone()[0]
        )
        conn.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status) "
            "VALUES (%s, %s, 'sales_recovery', 'awaiting_approval')",
            (tenant_id, work_item),
        )
        # vtr_step_timeline ← pipeline_runs (LEFT JOIN pipeline_steps — a run with no steps
        # still yields one timeline row via the LEFT JOIN).
        conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
            "VALUES (%s, 'agent_dispatch', 'completed')",
            (tenant_id,),
        )
        # vtr_workflow_controls ← workflow_controls
        conn.execute(
            "INSERT INTO workflow_controls (tenant_id, workflow_kind, set_by, reason) "
            "VALUES (%s, 'agent_dispatch', %s, 'test hold')",
            (tenant_id, str(uuid4())),
        )


@contextmanager
def _vtr_session(dsn: str, *, operator_id: str | None, admin: bool = False):  # type: ignore[no-untyped-def]
    """A connection entered as ``app_vtr_role`` (or ``app_vtr_admin_role`` when ``admin``)
    with the ``app.vtr_operator_id`` GUC set to ``operator_id`` (txn-local, the mig-134
    plumbing). ``operator_id=None`` leaves the GUC UNSET; pass ``''`` to set it to the empty
    string (the ruling's predicate-fix case). Mirrors how the suite exercises the views the
    way ``vtr_connection(operator_id=...)`` will at runtime."""
    with psycopg.connect(dsn, autocommit=False) as conn, conn.cursor() as cur:
        role = "app_vtr_admin_role" if admin else "app_vtr_role"
        cur.execute(f"SET ROLE {role}")  # noqa: S608 — fixed role allowlist
        if operator_id is not None:
            cur.execute(
                "SELECT set_config(%s, %s, true)", (_VTR_OPERATOR_GUC, operator_id)
            )
        try:
            yield cur
        finally:
            conn.rollback()  # txn-local GUC + role unwind; no committed state


def _tenants_seen(cur: Any, view: str) -> set[str]:
    cur.execute(f"SELECT DISTINCT tenant_id::text FROM {view}")  # noqa: S608 — fixed view allowlist
    return {r[0] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# View-scoping legs. The build-window xfail gate is RETIRED (Cowork gate C2):
# post-merge, a missing mig-134 must HARD-FAIL the isolation battery — an xfail
# here is a silent-neutering vector on the multi-VTR security proof.
# ---------------------------------------------------------------------------


def _gate(dsn: str) -> None:
    assert _mig134_present(dsn), (
        "mig-134 (app_vtr_operator + assignment-scoped views) is MISSING from the "
        "test database — the multi-VTR isolation battery cannot be skipped (gate C2)"
    )


def test_guc_operator_a_sees_only_its_tenant_across_all_nine_views(substrate):
    """GUC = A ⇒ EVERY one of the nine views returns ONLY T_A; never T_B."""
    _gate(substrate)
    op_a, op_b = _operator(), _operator()
    t_a, t_b = _tenant(substrate), _tenant(substrate)
    _assign(substrate, op_a, t_a)
    _assign(substrate, op_b, t_b)
    _seed_all_views(substrate, t_a)
    _seed_all_views(substrate, t_b)

    with _vtr_session(substrate, operator_id=op_a) as cur:
        for view in _SCOPED_VIEWS:
            seen = _tenants_seen(cur, view)
            assert t_a in seen, f"{view}: operator A cannot see its OWN assigned tenant"
            assert t_b not in seen, f"{view}: operator A LEAKED operator B's tenant (scoping broken)"


def test_guc_operator_b_sees_only_its_tenant_across_all_nine_views(substrate):
    """GUC = B ⇒ the symmetric assertion: only T_B, never T_A."""
    _gate(substrate)
    op_a, op_b = _operator(), _operator()
    t_a, t_b = _tenant(substrate), _tenant(substrate)
    _assign(substrate, op_a, t_a)
    _assign(substrate, op_b, t_b)
    _seed_all_views(substrate, t_a)
    _seed_all_views(substrate, t_b)

    with _vtr_session(substrate, operator_id=op_b) as cur:
        for view in _SCOPED_VIEWS:
            seen = _tenants_seen(cur, view)
            assert t_b in seen, f"{view}: operator B cannot see its OWN assigned tenant"
            assert t_a not in seen, f"{view}: operator B LEAKED operator A's tenant"


def test_guc_unset_sees_zero_rows_across_all_nine_views(substrate):
    """GUC UNSET ⇒ ``app_vtr_operator()`` is NULL ⇒ the scoped subquery matches nothing ⇒
    ZERO rows. Fail-closed by construction (a missing operator never sees anything)."""
    _gate(substrate)
    t_a = _tenant(substrate)
    _seed_all_views(substrate, t_a)
    with _vtr_session(substrate, operator_id=None) as cur:
        for view in _SCOPED_VIEWS:
            assert _tenants_seen(cur, view) == set(), (
                f"{view}: an UNSET operator GUC must see ZERO rows (fail-closed)"
            )


def test_guc_empty_string_sees_zero_rows_and_no_error(substrate):
    """THE ruling's binding predicate-fix case: GUC = '' (the empty string a pooled session
    can carry) ⇒ ``NULLIF(current_setting(...), '')::uuid`` is NULL ⇒ ZERO rows AND NO error.
    The naive ``current_setting(...)::uuid`` would raise ``invalid input syntax for type
    uuid: ""`` here (a 500 on every view query). This leg fails LOUD if the predicate fix
    regressed to the cast-throwing form."""
    _gate(substrate)
    t_a = _tenant(substrate)
    _seed_all_views(substrate, t_a)
    with _vtr_session(substrate, operator_id="") as cur:
        for view in _SCOPED_VIEWS:
            try:
                seen = _tenants_seen(cur, view)
            except pg_errors.InvalidTextRepresentation as exc:  # ''::uuid cast error
                pytest.fail(
                    f"{view}: empty-string GUC raised a cast error ({exc}) — the "
                    "predicate-fix (NULLIF before ::uuid) regressed; this 500s every view"
                )
            assert seen == set(), (
                f"{view}: an empty-string operator GUC must see ZERO rows (fail-closed)"
            )


def test_admin_role_sees_both_tenants_across_all_nine_views(substrate):
    """SET ROLE ``app_vtr_admin_role`` ⇒ BOTH tenants in every scoped view — the admin tier
    is the ROLE (the ``current_user = 'app_vtr_admin_role'`` predicate leg), not a bypass
    flag. No operator GUC is needed for admin."""
    _gate(substrate)
    t_a, t_b = _tenant(substrate), _tenant(substrate)
    _seed_all_views(substrate, t_a)
    _seed_all_views(substrate, t_b)
    with _vtr_session(substrate, operator_id=None, admin=True) as cur:
        for view in _SCOPED_VIEWS:
            seen = _tenants_seen(cur, view)
            assert t_a in seen and t_b in seen, (
                f"{view}: app_vtr_admin_role must see BOTH tenants (the admin-via-role leg)"
            )


def test_revoked_assignment_grants_nothing(substrate):
    """A REVOKED A→T_B row (``unassigned_at`` non-NULL) is excluded by the predicate's
    ``unassigned_at IS NULL`` leg: operator A — assigned to T_A, REVOKED from T_B — sees T_A
    but NOT T_B, proving the revoke takes immediate effect at the DB scoping layer."""
    _gate(substrate)
    op_a = _operator()
    t_a, t_b = _tenant(substrate), _tenant(substrate)
    _assign(substrate, op_a, t_a)  # live
    _assign(substrate, op_a, t_b, revoked=True)  # REVOKED
    _seed_all_views(substrate, t_a)
    _seed_all_views(substrate, t_b)
    with _vtr_session(substrate, operator_id=op_a) as cur:
        for view in _SCOPED_VIEWS:
            seen = _tenants_seen(cur, view)
            assert t_a in seen, f"{view}: the LIVE assignment must still grant T_A"
            assert t_b not in seen, (
                f"{view}: a REVOKED assignment (unassigned_at set) must grant NOTHING"
            )


# ===========================================================================
# API legs — operator A denied operator B's run/tenant across the run-control
# surface (timeline / programs / pause / override / rerun), tenant-scoped AND
# row-targeted. The assignment gate is operator_assignments (mig-072), which
# exists TODAY — these legs pass NOW (they are NOT substrate-gated on mig-134).
# Ride the test_ops_run_control.py TestClient idiom (every header explicit).
# ===========================================================================

_TEST_INTERNAL_SECRET = "unit-test-internal-not-a-secret"
_TEST_JWT_KEY = "unit-test-jwt-signing-key-not-a-secret-0000"  # >=32 bytes (HS256 hygiene)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERNAL_API_SECRET", _TEST_INTERNAL_SECRET)
    monkeypatch.setenv("OPERATOR_JWT_SECRET", _TEST_JWT_KEY)
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt-377-test-salt")
    monkeypatch.delenv("FAZAL_OWNER_UUID", raising=False)


def _op_jwt(operator_id: str) -> str:
    import jwt as pyjwt

    now = int(time.time())
    return pyjwt.encode(
        {"operator_claim": True, "operator_id": operator_id, "aud": "authenticated",
         "iat": now, "exp": now + 300},
        _TEST_JWT_KEY, algorithm="HS256",
    )


def _hdr(op: str) -> dict[str, Any]:
    return {"x_internal_secret": _TEST_INTERNAL_SECRET, "x_operator_jwt": _op_jwt(op)}


def _seed_run(dsn: str, tenant: str, *, run_type: str = "agent_dispatch") -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
                "VALUES (%s, %s, 'completed') RETURNING id",
                (tenant, run_type),
            ).fetchone()[0]
        )


def _seed_override_row(dsn: str, tenant: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO step_overrides (tenant_id, workflow_kind, step_name, "
                "created_by, expires_at) VALUES (%s, 'agent_dispatch', 'candidate_build', "
                "%s, now() + interval '1 day') RETURNING id",
                (tenant, str(uuid4())),
            ).fetchone()[0]
        )


def _two_operators(dsn: str) -> tuple[str, str, str]:
    """op_a assigned ONLY to t_a; t_b assigned to NOBODY op_a can reach. Returns
    (op_a, t_a, t_b)."""
    op_a = _operator()
    t_a, t_b = _tenant(dsn), _tenant(dsn)
    _assign(dsn, op_a, t_a)
    return op_a, t_a, t_b


def _deny_audit_rows(dsn: str, action: str, tenant: str) -> list[tuple[str, str, str]]:
    """The ops_audit deny rows for ``action`` on the DERIVED tenant — mirrors the
    ``_audit_rows`` idiom in api/test_ops_run_control.py. The Gap-6 gate writes the deny row on
    the SERVICE pool (app_vtr_role has no ops_audit grant) BEFORE raising 403, attributed to the
    VERIFIED operator. Returns (operator_id, target_kind, target_id) per row."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        return [
            (str(r[0]), r[1], r[2])
            for r in conn.execute(
                "SELECT operator_id, target_kind, target_id FROM ops_audit "
                "WHERE action = %s AND tenant_id = %s ORDER BY created_at",
                (action, tenant),
            ).fetchall()
        ]


def _assert_deny_audited(
    dsn: str, *, action: str, tenant: str, operator: str, target_id: str
) -> None:
    """The contract '403 + deny audit': after a cross-operator 403, EXACTLY the deny row for
    this (action, derived tenant) exists, attributed to the verified operator and carrying the
    right target id (the row the gate denied)."""
    rows = _deny_audit_rows(dsn, action, tenant)
    assert any(op == operator and tid == target_id for (op, _kind, tid) in rows), (
        f"{action}: missing deny audit row for op={operator} target={target_id}; got {rows}"
    )


def test_api_timeline_operator_a_denied_b_run_403(substrate):
    """/timeline derives tenant FROM THE RUN ROW (VT-293/294), then gates: op_a (assigned to
    t_a only) reading t_b's run is 403 — the row-targeted assignment gate fires."""
    from fastapi import HTTPException

    from orchestrator.api import ops_run_control as rc

    op_a, _t_a, t_b = _two_operators(substrate)
    run_b = _seed_run(substrate, t_b)
    with pytest.raises(HTTPException) as exc:
        rc.timeline(str(run_b), **_hdr(op_a))
    assert exc.value.status_code == 403
    _assert_deny_audited(
        substrate, action="timeline_read_denied", tenant=t_b, operator=op_a,
        target_id=str(run_b),
    )


def test_api_programs_operator_a_denied_b_tenant_403(substrate):
    """/programs gates on the PATH tenant: op_a reading t_b's programs is 403 (tenant-scoped
    assignment gate, not row-targeted)."""
    from fastapi import HTTPException

    from orchestrator.api import ops_run_control as rc

    op_a, _t_a, t_b = _two_operators(substrate)
    with pytest.raises(HTTPException) as exc:
        rc.programs(str(t_b), **_hdr(op_a))
    assert exc.value.status_code == 403
    # programs uses the default deny target (kind='tenant', id defaults to the tenant itself).
    _assert_deny_audited(
        substrate, action="programs_read_denied", tenant=t_b, operator=op_a,
        target_id=str(t_b),
    )


def test_api_pause_operator_a_denied_b_tenant_403(substrate):
    """/pause gates on the BODY tenant: op_a pausing t_b is 403 (tenant-scoped)."""
    from fastapi import HTTPException

    from orchestrator.api import ops_run_control as rc

    op_a, _t_a, t_b = _two_operators(substrate)
    with pytest.raises(HTTPException) as exc:
        rc.pause(
            rc.PauseBody(operator_id=op_a, tenant_id=str(t_b), workflow_kind="agent_dispatch"),
            **_hdr(op_a),
        )
    assert exc.value.status_code == 403
    _assert_deny_audited(
        substrate, action="workflow_pause_denied", tenant=t_b, operator=op_a,
        target_id=f"{t_b}:agent_dispatch",
    )


def test_api_override_operator_a_denied_b_tenant_403(substrate):
    """/override gates on the BODY tenant: op_a pinning an override on t_b is 403."""
    from fastapi import HTTPException

    from orchestrator.api import ops_run_control as rc

    op_a, _t_a, t_b = _two_operators(substrate)
    with pytest.raises(HTTPException) as exc:
        rc.override(
            rc.OverrideBody(
                operator_id=op_a, tenant_id=str(t_b), workflow_kind="agent_dispatch",
                step_name="candidate_build", pinned_input={"limit": 1},
            ),
            **_hdr(op_a),
        )
    assert exc.value.status_code == 403
    _assert_deny_audited(
        substrate, action="step_override_denied", tenant=t_b, operator=op_a,
        target_id="agent_dispatch:candidate_build",
    )


def test_api_cancel_override_operator_a_denied_b_row_403(substrate):
    """/cancel-override is ROW-targeted: the tenant is DERIVED from the override row (t_b), so
    op_a — who never sends a tenant — is 403 by the derived-tenant gate (the VT-293/294 IDOR
    probe at the assignment layer)."""
    from fastapi import HTTPException

    from orchestrator.api import ops_run_control as rc

    op_a, _t_a, t_b = _two_operators(substrate)
    override_b = _seed_override_row(substrate, t_b)
    with pytest.raises(HTTPException) as exc:
        rc.cancel_override(
            rc.CancelOverrideBody(operator_id=op_a, override_id=str(override_b)), **_hdr(op_a)
        )
    assert exc.value.status_code == 403
    # tenant is DERIVED from the override row (t_b); the deny row keys on it.
    _assert_deny_audited(
        substrate, action="step_override_cancel_denied", tenant=t_b, operator=op_a,
        target_id=str(override_b),
    )


def test_api_rerun_operator_a_denied_b_run_403(substrate):
    """/rerun is ROW-targeted: tenant DERIVED from the source run (t_b) ⇒ op_a is 403 by the
    derived-tenant assignment gate (no tenant crosses the wire)."""
    from fastapi import HTTPException

    from orchestrator.api import ops_run_control as rc

    op_a, _t_a, t_b = _two_operators(substrate)
    run_b = _seed_run(substrate, t_b)
    with pytest.raises(HTTPException) as exc:
        rc.rerun(
            rc.RerunBody(
                operator_id=op_a, source_run_id=str(run_b), from_step="execute_item",
                overrides=[],
            ),
            **_hdr(op_a),
        )
    assert exc.value.status_code == 403
    # tenant is DERIVED from the source run row (t_b); the deny row keys on it.
    _assert_deny_audited(
        substrate, action="run_rerun_denied", tenant=t_b, operator=op_a,
        target_id=str(run_b),
    )


# ---------------------------------------------------------------------------
# Gate C3 — the vtr_connection() FAZAL break-glass delegation legs (the helper's
# admin dispatch + its fail-closed branch were previously untested).
# ---------------------------------------------------------------------------


class _DsnPool:
    """Minimal ``.connection()`` shim over a plain superuser connect (the
    test_run_control_realdb idiom) — exercises vtr_connection's pool seam without
    depending on the launched DBOS pool's state."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @contextmanager
    def connection(self):  # type: ignore[no-untyped-def]
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            yield conn


def test_vtr_connection_fazal_delegates_to_admin_tier(substrate, monkeypatch):
    """operator_id == FAZAL_OWNER_UUID ⇒ vtr_connection delegates to vtr_admin_connection:
    the session runs as app_vtr_admin_role and the mig-134 role leg opens ALL tenants."""
    _gate(substrate)
    from orchestrator.privacy import vtr as vtr_mod

    fazal = _operator()
    monkeypatch.setenv("FAZAL_OWNER_UUID", fazal)
    t_a, t_b = _tenant(substrate), _tenant(substrate)
    _seed_all_views(substrate, t_a)
    _seed_all_views(substrate, t_b)

    pool = _DsnPool(substrate)
    with vtr_mod.vtr_connection(operator_id=fazal, pool=pool) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_user")
        assert cur.fetchone()[0] == "app_vtr_admin_role", (
            "FAZAL delegation must run the session as the admin role (role IS the mechanism)"
        )
        for view in _SCOPED_VIEWS:
            seen = _tenants_seen(cur, view)
            assert {t_a, t_b} <= seen, f"{view}: admin delegation must see ALL tenants"


def test_vtr_connection_fazal_env_unset_fails_closed(substrate, monkeypatch):
    """The fail-closed branch: FAZAL_OWNER_UUID UNSET ⇒ the would-be admin uuid gets NO
    delegation — it is a plain VTR with zero assignments, and every scoped view returns
    nothing. An unset env must never open the all-tenants gate."""
    _gate(substrate)
    from orchestrator.privacy import vtr as vtr_mod

    would_be_fazal = _operator()
    monkeypatch.delenv("FAZAL_OWNER_UUID", raising=False)
    t_a = _tenant(substrate)
    _seed_all_views(substrate, t_a)

    pool = _DsnPool(substrate)
    with vtr_mod.vtr_connection(operator_id=would_be_fazal, pool=pool) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_user")
        assert cur.fetchone()[0] == "app_vtr_role", (
            "without the env the identity must NOT reach the admin role"
        )
        for view in _SCOPED_VIEWS:
            assert _tenants_seen(cur, view) == set(), (
                f"{view}: unassigned identity leaked rows with FAZAL_OWNER_UUID unset"
            )
