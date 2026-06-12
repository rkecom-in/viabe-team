"""VT-374 — run-control ops API acceptance (plan §7; build-contract §Ops API; fix-contract Test-B).

The Gap-6 TestClient pattern (mirrors ``test_ops_vtr_console.py``): handlers are called DIRECTLY
as plain functions with EVERY header param passed explicitly (a defaulted ``Header`` is a truthy
``FieldInfo`` when FastAPI's DI is bypassed — the repo-memory trap). Run against a real migrated PG.

Coverage (fix-contract Test-B):
  * Auth stack — bad secret / missing JWT / bad-signed JWT / expired JWT / operator≠JWT-claim /
    unassigned tenant all 403; FAZAL_OWNER_UUID break-glass passes. (The shared ``ops_common`` gate
    returns **403** for transport-auth failures — NOT 401; the contract's "401" is loose wording.
    The Gap-6 template asserts 403 too. Asserting 401 here would falsely fail the correct code.)
  * /pause read-back + 409 already-paused + /release + 404 release-when-none.
  * EVERY /override 422 arm: unknown step, pause-only (dispatch_brain, N3), observed tier
    (question_brain_compose), non-allowed pinned keys, pinned_output on a non-pure_return step,
    gate-module step name (F14), past expires_at, nothing-pinned, non-object pin (C4 — enforced by
    pydantic on OverrideBody; the /rerun leg below exercises the handler's explicit C4 422 guard).
  * Row-targeted tenant derivation (cancel-override / rerun derive tenant FROM THE ROW): cross-tenant
    attempts are 403 (the derived-tenant gate fires) / 404 (missing row) — the VT-293/294 IDOR probes.
  * Audit row written BEFORE the mutation (CL-390 — metadata only, never reason/pin values).
  * THE BINDING registry-populates acceptance (VT-361 lesson): seed a tenant + a 2-word customer
    display name via the real customers path; POST /override with a reason carrying that name + a
    phone; read the raw row as service role; assert BOTH redacted AND that make_name_registry
    actually returned a POPULATED registry. An inert/empty registry MUST FAIL this test — the phone
    is pattern-redacted regardless, so the name token is the only signal the registry did its job,
    and the explicit ``reg(name) is True`` assertion is the inert-registry tripwire.
  * /rerun 422-vs-409 mapping (C3): RerunRefused.code 422 (forbidden kind / unknown step /
    non-object pin) maps to 422; .code 409 (open pending approval, F10) maps to 409 — the
    distinction the pre-C3 ``.status_code`` lookup collapsed to 409.
  * VT-376 /timeline annotations (build contract §B1.3/§B1.6): per-step ``allowed_keys``
    (registry key NAMES on controllable steps, [] otherwise), run-level ``rerunnable`` +
    the pinned ``forbidden_reason`` why-copy for rerun-forbidden kinds; and the mig-132
    explicit envelope projections read END-TO-END through the endpoint (injected
    foreign keys/values never reach the response).

DB substrate mirrors ``test_ops_vtr_console.py``: importorskip psycopg+dbos(+langgraph)+fastapi+jwt,
skipif no DATABASE_URL, module fixture apply_migrations + launch_dbos; seeds via direct autocommit
psycopg (service role). Unique phone/tenant per run (uuid suffix).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")
pytest.importorskip("langgraph")  # orchestrator.graph imports langgraph transitively
pytest.importorskip("fastapi")
pytest.importorskip("jwt")

import jwt as pyjwt  # noqa: E402 — after dependency skip guards
import psycopg  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from orchestrator.api import ops_run_control as rc  # noqa: E402
from orchestrator.privacy import customer_registry  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-374 run-control API tests skipped",
)

pytestmark = requires_db

_TEST_INTERNAL_SECRET = "unit-test-internal-not-a-secret"
_TEST_JWT_KEY = "unit-test-jwt-signing-key-not-a-secret-0000"  # >=32 bytes (HS256 hygiene)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the service pool (get_pool) the handlers use exists."""
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
        shutdown_dbos()


@pytest.fixture(autouse=True)
def _purge_synthetic_breach_rows(substrate):  # type: ignore[no-untyped-def]
    """The mig-132 projection tests seed synthetic ``tenant_isolation_breach`` step rows,
    but the VT-79 Detector-1 suite (privacy/test_k_anonymity.py) asserts a GLOBAL zero
    count of that kind — purge on teardown so the detector invariant holds suite-wide."""
    yield
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM pipeline_steps WHERE step_kind = 'tenant_isolation_breach'"
        )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth env per test; no ambient break-glass (tests that want Fazal set it explicitly)."""
    monkeypatch.setenv("INTERNAL_API_SECRET", _TEST_INTERNAL_SECRET)
    monkeypatch.setenv("OPERATOR_JWT_SECRET", _TEST_JWT_KEY)
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt-374-test-salt")
    monkeypatch.delenv("FAZAL_OWNER_UUID", raising=False)
    # The registry caches per-tenant in-process; a stale cache from a prior test must never
    # mask a real population (the BINDING acceptance depends on a true read).
    customer_registry.invalidate_all()


def _op_jwt(
    operator_id: str, *, secret: str = _TEST_JWT_KEY, exp_delta: int = 300
) -> str:
    now = int(time.time())
    return pyjwt.encode(
        {"operator_claim": True, "operator_id": operator_id, "aud": "authenticated",
         "iat": now, "exp": now + exp_delta},
        secret, algorithm="HS256",
    )


# --- seeding helpers (direct service-role connection — RLS bypassed at seed) ---


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, whatsapp_number) "
            "VALUES ('VT-374 run-control test', 'founding', 'trial', now(), 'restaurant', %s) "
            "RETURNING id",
            (f"+91{uuid4().int % 10**10:010d}",),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _assign(dsn: str, operator_id: str, tenant: UUID) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO operator_assignments (operator_id, tenant_id) VALUES (%s, %s)",
            (operator_id, str(tenant)),
        )


def _seed_customer(dsn: str, tenant: UUID, display_name: str) -> UUID:
    """The REAL customers path the name registry reads (CustomersWrapper.list_display_names)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO customers (tenant_id, display_name) VALUES (%s, %s) RETURNING id",
            (str(tenant), display_name),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_run(dsn: str, tenant: UUID, *, run_type: str, status: str = "completed") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        run_id = uuid4()
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) VALUES (%s, %s, %s, %s)",
            (str(run_id), str(tenant), run_type, status),
        )
    return run_id


def _seed_open_approval(dsn: str, tenant: UUID, run_id: UUID) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, summary, timeout_at) "
            "VALUES (%s, %s, 'campaign_send', 'pending', now() + interval '1 day')",
            (str(tenant), str(run_id)),
        )


def _seed_override_row(dsn: str, tenant: UUID) -> UUID:
    """A foreign unconsumed next-run override, seeded service-role (for the IDOR cancel probe)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        override_id = uuid4()
        conn.execute(
            "INSERT INTO step_overrides (id, tenant_id, workflow_kind, step_name, created_by, "
            "expires_at) VALUES (%s, %s, 'agent_dispatch', 'candidate_build', %s, "
            "now() + interval '1 day')",
            (str(override_id), str(tenant), str(uuid4())),
        )
    return override_id


def _audit_rows(dsn: str, action: str, tenant: UUID) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT operator_id, tenant_id, action, target_kind, target_id, detail "
            "FROM ops_audit WHERE action = %s AND tenant_id = %s ORDER BY created_at",
            (action, str(tenant)),
        ).fetchall()


def _override_row(dsn: str, override_id: str) -> dict[str, Any]:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT reason, pinned_input, cancelled_at FROM step_overrides WHERE id = %s",
            (override_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _override_count(dsn: str, tenant: UUID) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT count(*) FROM step_overrides WHERE tenant_id = %s", (str(tenant),)
        ).fetchone()[0]


def _control_row(dsn: str, tenant: UUID, workflow_kind: str) -> dict[str, Any] | None:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT reason, released_at FROM workflow_controls "
            "WHERE tenant_id = %s AND workflow_kind = %s",
            (str(tenant), workflow_kind),
        ).fetchone()
    return dict(row) if row is not None else None


# --- header-call wrappers (every header param explicit) ---


def _hdr(secret: str = _TEST_INTERNAL_SECRET, jwt: str | None = "MINT", op: str | None = None):
    """Returns the kwargs for a handler call. jwt='MINT' → sign for ``op``; else pass-through."""
    token = _op_jwt(op or "") if jwt == "MINT" else jwt
    return {"x_internal_secret": secret, "x_operator_jwt": token}


# ---------------------------------------------------------------------------
# 1. Auth stack — the Gap-6 gate is inherited, not re-hand-rolled (all 403)
# ---------------------------------------------------------------------------


def _assigned(dsn: str) -> tuple[str, UUID]:
    op = str(uuid4())
    tenant = _new_tenant(dsn)
    _assign(dsn, op, tenant)
    return op, tenant


def _pause(op: str, tenant: UUID, **hdr: Any) -> dict[str, Any]:
    return rc.pause(
        rc.PauseBody(operator_id=op, tenant_id=str(tenant), workflow_kind="agent_dispatch"),
        **hdr,
    )


def test_auth_bad_internal_secret_403(substrate) -> None:
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _pause(op, tenant, **_hdr(secret="wrong", jwt=_op_jwt(op)))
    assert exc.value.status_code == 403


def test_auth_missing_jwt_403_even_with_valid_secret(substrate) -> None:
    """The run-control-inheritance-broken proof: a valid internal secret ALONE never suffices."""
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _pause(op, tenant, **_hdr(jwt=None))
    assert exc.value.status_code == 403


def test_auth_bad_signed_jwt_403(substrate) -> None:
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _pause(op, tenant, **_hdr(jwt=_op_jwt(op, secret="x" * 40)))
    assert exc.value.status_code == 403


def test_auth_expired_jwt_403(substrate) -> None:
    """exp is REQUIRED + verified — an expired operator JWT is a hard 403 (no honoring-forever)."""
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _pause(op, tenant, **_hdr(jwt=_op_jwt(op, exp_delta=-10)))
    assert exc.value.status_code == 403


def test_auth_operator_id_mismatch_403(substrate) -> None:
    """A JWT signed for B cannot act as body-claimed A (no body-trusted attribution)."""
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _pause(op, tenant, **_hdr(jwt=_op_jwt(str(uuid4()))))
    assert exc.value.status_code == 403


def test_auth_unassigned_tenant_403(substrate) -> None:
    """Fail-CLOSED on assignment: a valid operator with no operator_assignments row is 403."""
    op = str(uuid4())
    tenant = _new_tenant(substrate.dsn)  # no assignment
    with pytest.raises(HTTPException) as exc:
        _pause(op, tenant, **_hdr(op=op))
    assert exc.value.status_code == 403


def test_auth_fazal_break_glass_passes(substrate, monkeypatch) -> None:
    """FAZAL_OWNER_UUID = VTAdmin break-glass: passes WITHOUT an assignment row."""
    fazal = str(uuid4())
    monkeypatch.setenv("FAZAL_OWNER_UUID", fazal)
    tenant = _new_tenant(substrate.dsn)
    out = _pause(fazal, tenant, **_hdr(op=fazal))
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# 2. /pause read-back + 409 + /release
# ---------------------------------------------------------------------------


def test_pause_readback_then_409_already_paused_then_release(substrate) -> None:
    op, tenant = _assigned(substrate.dsn)
    out = _pause(op, tenant, **_hdr(op=op))
    assert out["ok"] is True and out["workflow_kind"] == "agent_dispatch"

    # 409 on a second pause of the same (tenant, kind) — the partial-unique active index.
    with pytest.raises(HTTPException) as exc:
        _pause(op, tenant, **_hdr(op=op))
    assert exc.value.status_code == 409

    rel = rc.release(
        rc.ReleaseBody(operator_id=op, tenant_id=str(tenant), workflow_kind="agent_dispatch"),
        **_hdr(op=op),
    )
    assert rel["ok"] is True
    assert _control_row(substrate.dsn, tenant, "agent_dispatch")["released_at"] is not None


def test_release_when_none_active_404(substrate) -> None:
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        rc.release(
            rc.ReleaseBody(operator_id=op, tenant_id=str(tenant), workflow_kind="agent_dispatch"),
            **_hdr(op=op),
        )
    assert exc.value.status_code == 404


def test_pause_audit_before_mutation_metadata_only(substrate) -> None:
    """One workflow_pause audit row, attributed to the VERIFIED operator, carrying metadata only —
    NEVER the reason text (CL-390): only reason_len + the kind."""
    op, tenant = _assigned(substrate.dsn)
    rc.pause(
        rc.PauseBody(
            operator_id=op, tenant_id=str(tenant), workflow_kind="agent_dispatch",
            reason="Customer Rajesh Kumar wants a pause",
        ),
        **_hdr(op=op),
    )
    rows = _audit_rows(substrate.dsn, "workflow_pause", tenant)
    assert len(rows) == 1
    assert str(rows[0]["operator_id"]) == op
    assert rows[0]["target_kind"] == "workflow_control"
    detail = rows[0]["detail"] or ""
    assert "reason_len=" in detail and "agent_dispatch" in detail
    assert "Rajesh" not in detail and "Kumar" not in detail  # no value leak


# ---------------------------------------------------------------------------
# 3. /override — EVERY 422 arm
# ---------------------------------------------------------------------------


def _override(op: str, tenant: UUID, **fields: Any):
    body = rc.OverrideBody(operator_id=op, tenant_id=str(tenant), **fields)
    return rc.override(body, **_hdr(op=op))


def test_override_unknown_step_422(substrate) -> None:
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _override(op, tenant, workflow_kind="agent_dispatch", step_name="nope",
                  pinned_input={"limit": 1})
    assert exc.value.status_code == 422


def test_override_pause_only_dispatch_brain_422(substrate) -> None:
    """N3: dispatch_brain is a pause-only boundary — an override write 422s by construction."""
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _override(op, tenant, workflow_kind="webhook_inbound", step_name="dispatch_brain",
                  pinned_input={"x": 1})
    assert exc.value.status_code == 422


def test_override_observed_tier_422(substrate) -> None:
    """question_brain_compose is observed-only (STEP-0 demotion) — timeline display, not control."""
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _override(op, tenant, workflow_kind="webhook_inbound",
                  step_name="question_brain_compose", pinned_input={"x": 1})
    assert exc.value.status_code == 422


def test_override_non_allowed_keys_422(substrate) -> None:
    """I7: candidate_build allows only {'limit'} — any other key is 422 (never allow-listed)."""
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _override(op, tenant, workflow_kind="agent_dispatch", step_name="candidate_build",
                  pinned_input={"evil": 1})
    assert exc.value.status_code == 422


def test_override_pinned_output_non_pure_return_422(substrate) -> None:
    """F6 scenario A: pinned_output is legal only for pure_return steps (v1 registry: none)."""
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _override(op, tenant, workflow_kind="agent_dispatch", step_name="candidate_build",
                  pinned_output={"result": 1})
    assert exc.value.status_code == 422


def test_override_gate_module_step_name_422(substrate) -> None:
    """F14: a step NAMED like a gate-manifest surface gets the explicit manifest rejection."""
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _override(op, tenant, workflow_kind="campaign_send", step_name="customer_send",
                  pinned_input={"x": 1})
    assert exc.value.status_code == 422


def test_override_nothing_pinned_422(substrate) -> None:
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _override(op, tenant, workflow_kind="agent_dispatch", step_name="candidate_build")
    assert exc.value.status_code == 422


def test_override_past_expires_at_422(substrate) -> None:
    """Next-run pins REQUIRE a future expires_at (F8) — a past one is 422."""
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _override(op, tenant, workflow_kind="agent_dispatch", step_name="candidate_build",
                  pinned_input={"limit": 1},
                  expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    assert exc.value.status_code == 422


def test_override_non_object_pin_rejected_by_body(substrate) -> None:
    """C4 (/override leg): a present-but-non-object pinned_input never reaches the handler — the
    OverrideBody pydantic model (dict | None) rejects it at construction (a 422-class refusal at
    the API boundary, so redaction is never silently skipped). The handler's explicit C4 422 guard
    is exercised on the /rerun leg, where overrides is list[dict] and the inner value can be a str.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        rc.OverrideBody(
            operator_id=str(uuid4()), tenant_id=str(uuid4()), workflow_kind="agent_dispatch",
            step_name="candidate_build", pinned_input="not-an-object",  # type: ignore[arg-type]
        )


def test_override_next_run_defaults_expiry_and_succeeds(substrate) -> None:
    """A clean next-run override: no workflow_id → expires_at auto-defaults (F8 7d), row written."""
    op, tenant = _assigned(substrate.dsn)
    out = _override(op, tenant, workflow_kind="agent_dispatch", step_name="candidate_build",
                    pinned_input={"limit": 3})
    assert out["ok"] is True and out["expires_at"] is not None
    assert _override_row(substrate.dsn, out["override_id"])["pinned_input"] == {"limit": 3}


def test_override_registry_build_failure_503_writes_nothing(substrate, monkeypatch) -> None:
    """A1 fail-closed: a make_name_registry FAILURE on a /override carrying free text refuses the
    write with 503 (never an unredacted row) — and writes ZERO step_overrides rows (the registry
    fail-closed fires BEFORE the INSERT). The auth + step validation pass; only the registry build
    blows up, so this proves the redaction gate, not an earlier refusal."""
    op, tenant = _assigned(substrate.dsn)

    def _boom(_tenant_id: str) -> Any:
        raise RuntimeError("synthetic registry outage")

    monkeypatch.setattr(rc, "make_name_registry", _boom)

    before = _override_count(substrate.dsn, tenant)
    with pytest.raises(HTTPException) as exc:
        _override(op, tenant, workflow_kind="agent_dispatch", step_name="candidate_build",
                  pinned_input={"limit": 1}, reason="Customer Rajesh Kumar wants a pause")
    assert exc.value.status_code == 503
    assert _override_count(substrate.dsn, tenant) == before, (
        "the registry-503 must write zero step_overrides rows (fail-closed before INSERT)"
    )


# ---------------------------------------------------------------------------
# 4. Row-targeted tenant derivation — the VT-293/294 IDOR probes
# ---------------------------------------------------------------------------


def _cancel(op: str, override_id: str, **hdr: Any) -> dict[str, Any]:
    return rc.cancel_override(
        rc.CancelOverrideBody(operator_id=op, override_id=override_id), **hdr
    )


def test_cancel_override_no_tenant_id_in_body() -> None:
    """The VT-293/294 pin: the cancel + rerun bodies CANNOT carry a tenant_id — it is derived from
    the target row server-side, so a client can never pair a foreign row with an assigned tenant."""
    assert "tenant_id" not in rc.CancelOverrideBody.model_fields
    assert "tenant_id" not in rc.RerunBody.model_fields


def test_cancel_override_cross_tenant_403_derived_gate(substrate) -> None:
    """opA (assigned to A only) cannot cancel B's override: the tenant DERIVED from the row is B,
    so the assignment gate fails — 403, the override untouched."""
    dsn = substrate.dsn
    op_a, _ = _assigned(dsn)
    tenant_b = _new_tenant(dsn)
    override_b = _seed_override_row(dsn, tenant_b)
    with pytest.raises(HTTPException) as exc:
        _cancel(op_a, str(override_b), **_hdr(op=op_a))
    assert exc.value.status_code == 403
    assert _override_row(dsn, str(override_b))["cancelled_at"] is None  # untouched


def test_cancel_override_missing_404(substrate) -> None:
    op, _ = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _cancel(op, str(uuid4()), **_hdr(op=op))
    assert exc.value.status_code == 404


def test_cancel_override_success_then_409_already_cancelled(substrate) -> None:
    op, tenant = _assigned(substrate.dsn)
    out = _override(op, tenant, workflow_kind="agent_dispatch", step_name="candidate_build",
                    pinned_input={"limit": 2})
    cancelled = _cancel(op, out["override_id"], **_hdr(op=op))
    assert cancelled["ok"] is True and cancelled["tenant_id"] == str(tenant)
    assert _override_row(substrate.dsn, out["override_id"])["cancelled_at"] is not None
    with pytest.raises(HTTPException) as exc:
        _cancel(op, out["override_id"], **_hdr(op=op))
    assert exc.value.status_code == 409


def _rerun(op: str, source_run_id: UUID, from_step: str, overrides=None, **hdr: Any):
    return rc.rerun(
        rc.RerunBody(operator_id=op, source_run_id=str(source_run_id), from_step=from_step,
                     overrides=overrides or []),
        **hdr,
    )


def test_rerun_cross_tenant_403_derived_gate(substrate) -> None:
    """opA cannot rerun B's run: tenant derived from the run row is B → assignment gate 403."""
    dsn = substrate.dsn
    op_a, _ = _assigned(dsn)
    tenant_b = _new_tenant(dsn)
    run_b = _seed_run(dsn, tenant_b, run_type="agent_dispatch")
    with pytest.raises(HTTPException) as exc:
        _rerun(op_a, run_b, "execute_item", **_hdr(op=op_a))
    assert exc.value.status_code == 403


def test_rerun_missing_run_404(substrate) -> None:
    op, _ = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _rerun(op, uuid4(), "execute_item", **_hdr(op=op))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 5. /rerun 422-vs-409 mapping (C3 — RerunRefused.code, not the dropped .status_code)
# ---------------------------------------------------------------------------


def test_rerun_forbidden_kind_422(substrate) -> None:
    """webhook_inbound is forbidden-on-rerun (MessageSid ledger semantics): RerunRefused code=422
    → 422. Pre-C3 this collapsed to 409 (the .status_code attr never existed on RerunRefused)."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    run = _seed_run(dsn, tenant, run_type="twilio_inbound")  # → webhook_inbound, forbidden
    with pytest.raises(HTTPException) as exc:
        _rerun(op, run, "dispatch_brain", **_hdr(op=op))
    assert exc.value.status_code == 422


def test_rerun_unknown_step_422(substrate) -> None:
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    run = _seed_run(dsn, tenant, run_type="plan_generate")
    with pytest.raises(HTTPException) as exc:
        _rerun(op, run, "no-such-step", **_hdr(op=op))
    assert exc.value.status_code == 422


def test_rerun_non_object_pin_422_c4_handler_guard(substrate) -> None:
    """C4 (/rerun leg): a present-but-non-object pin inside a RerunBody override spec (overrides is
    list[dict], so pydantic does NOT block the inner str) hits the handler's explicit 422 guard —
    it must NOT silently skip redaction."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    run = _seed_run(dsn, tenant, run_type="plan_generate")
    with pytest.raises(HTTPException) as exc:
        _rerun(op, run, "generate_validate",
               overrides=[{"step_name": "generate_validate", "pinned_input": "str-not-object"}],
               **_hdr(op=op))
    assert exc.value.status_code == 422


def test_rerun_open_approval_409(substrate) -> None:
    """F10: an open pending approval refuses the rerun — RerunRefused code=409 → 409. This is the
    OTHER half of the C3 mapping: 409 stays 409 while the 422 arms above map to 422."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    run = _seed_run(dsn, tenant, run_type="plan_generate")
    _seed_open_approval(dsn, tenant, run)
    with pytest.raises(HTTPException) as exc:
        _rerun(op, run, "generate_validate", **_hdr(op=op))
    assert exc.value.status_code == 409


def test_rerun_overlap_escalates_response_carries_outcome_200(substrate, monkeypatch) -> None:
    """VT-375 C1 (Cowork ruling 20260611T234500Z, Option A) — the FORCED overlap: an owner
    approval arms AFTER the 409 gate passed but DURING the rerun's execution (injected by
    stubbing ``delivery.deliver_plan`` to commit a real pending_approvals row mid-flight).
    The /rerun response must stay HTTP **200** (the rerun DID run — no rollback) and carry
    ``outcome='escalated_overlap'``; the lineage row closes 'escalated' with
    ``final_outcome='rerun_overlapped_open_approval'`` — escalated and disclosed, never a
    silent keep."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    run = _seed_run(dsn, tenant, run_type="plan_deliver")
    # An active plan with every part already delivered (bitmap 3) — the deliver arm is the
    # cheapest synchronous arm to drive through the API.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO business_plan (tenant_id, version, summary_json, roadmap_json, "
            "fact_bundle_json, generated_by, delivered_parts) "
            "VALUES (%s, 1, %s, '[]', '{}', 'test', 3)",
            (str(tenant), '{"text": "plan"}'),
        )

    from orchestrator.business_plan import delivery

    def _arm_mid_flight(*_a: Any, **_k: Any) -> None:
        _seed_open_approval(dsn, tenant, run)  # the overlap: commits between gate and re-check

    monkeypatch.setattr(delivery, "deliver_plan", _arm_mid_flight)

    out = _rerun(op, run, "deliver_parts", **_hdr(op=op))

    assert out["ok"] is True
    assert out["outcome"] == "escalated_overlap"
    assert out["new_run_id"] and out["new_run_id"] != str(run)
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT status, terminal_state_metadata FROM pipeline_runs WHERE id = %s",
            (out["new_run_id"],),
        ).fetchone()
    assert row is not None and row["status"] == "escalated"
    assert row["terminal_state_metadata"]["final_outcome"] == "rerun_overlapped_open_approval"
    assert row["terminal_state_metadata"]["version"] == 1, "the arm's effects stand (no rollback)"


def test_rerun_no_overlap_response_outcome_completed_200(substrate, monkeypatch) -> None:
    """The C1 counter-leg: no approval arms mid-flight → 200 with ``outcome='completed'`` and
    the lineage row closed 'completed' (the response shape the canvas-era operators read)."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    run = _seed_run(dsn, tenant, run_type="plan_deliver")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO business_plan (tenant_id, version, summary_json, roadmap_json, "
            "fact_bundle_json, generated_by, delivered_parts) "
            "VALUES (%s, 1, %s, '[]', '{}', 'test', 3)",
            (str(tenant), '{"text": "plan"}'),
        )

    from orchestrator.business_plan import delivery

    monkeypatch.setattr(delivery, "deliver_plan", lambda *a, **k: None)

    out = _rerun(op, run, "deliver_parts", **_hdr(op=op))

    assert out["ok"] is True
    assert out["outcome"] == "completed"
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT status, terminal_state_metadata FROM pipeline_runs WHERE id = %s",
            (out["new_run_id"],),
        ).fetchone()
    assert row is not None and row["status"] == "completed"
    assert row["terminal_state_metadata"]["final_outcome"] == "completed"


# ---------------------------------------------------------------------------
# 6. /timeline — read through the mig-131 views, tenant derived from the run
# ---------------------------------------------------------------------------


def test_timeline_reads_views_and_derives_tenant(substrate) -> None:
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    run = _seed_run(dsn, tenant, run_type="agent_dispatch")
    out = rc.timeline(str(run), **_hdr(op=op))
    assert out["run_id"] == str(run)
    assert out["tenant_id"] == str(tenant)
    assert isinstance(out["steps"], list)
    assert isinstance(out["active_controls"], list)


def test_timeline_open_approval_preflight_flag(substrate) -> None:
    """VT-376 pre-flight: /timeline carries the tenant's open-approval STATE as a boolean
    only (the rerun dialog's warn+disable input; server 409 stays the authority). The
    response must never carry approval row contents."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    run = _seed_run(dsn, tenant, run_type="agent_dispatch")
    out = rc.timeline(str(run), **_hdr(op=op))
    assert out["open_approval"] is False
    _seed_open_approval(dsn, tenant, run)
    out2 = rc.timeline(str(run), **_hdr(op=op))
    assert out2["open_approval"] is True
    # Boolean only — no approval payload anywhere in the response.
    assert "approval" not in {k for k in out2 if k != "open_approval"}
    assert not any(
        isinstance(v, dict) and "approval_type" in v
        for v in out2.values()
        if isinstance(v, dict)
    )


def test_timeline_cross_tenant_403(substrate) -> None:
    """Timeline derives tenant from the run row, then gates — opA cannot read B's run."""
    dsn = substrate.dsn
    op_a, _ = _assigned(dsn)
    tenant_b = _new_tenant(dsn)
    run_b = _seed_run(dsn, tenant_b, run_type="agent_dispatch")
    with pytest.raises(HTTPException) as exc:
        rc.timeline(str(run_b), **_hdr(op=op_a))
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# VT-377 — two-operator cross-scope + the mig-134 empty-GUC predicate-fix legs
# (build contract §B3.1: "empty-GUC + cross-operator API legs"). The assignment
# gate is operator_assignments (mig-072), live today; the GUC plumbing
# (vtr_connection(operator_id=...) + app_vtr_operator()) lands as B1/mig-134, so
# the empty-GUC read leg is substrate-gated and flips to PASS on that merge.
# ---------------------------------------------------------------------------


def _mig134_present(dsn: str) -> bool:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return (
            conn.execute(
                "SELECT to_regprocedure('app_vtr_operator()') IS NOT NULL"
            ).fetchone()[0]
            is True
        )


def test_timeline_both_operators_assigned_a_still_denied_b_run_403(substrate) -> None:
    """The gate is PER-OPERATOR, not "any assignment": opA and opB are BOTH validly assigned
    (each to their own tenant), yet opA reading opB's run is still 403 — the derived-tenant
    gate keys on opA's OWN assignment set, never "is the caller assigned to anything"."""
    dsn = substrate.dsn
    op_a, _tenant_a = _assigned(dsn)
    op_b, tenant_b = _assigned(dsn)  # opB is independently, validly assigned
    run_b = _seed_run(dsn, tenant_b, run_type="agent_dispatch")
    with pytest.raises(HTTPException) as exc:
        rc.timeline(str(run_b), **_hdr(op=op_a))  # opA, assigned elsewhere, still denied
    assert exc.value.status_code == 403
    # control: opB CAN read its own run (the gate admits the rightful operator).
    out = rc.timeline(str(run_b), **_hdr(op=op_b))
    assert out["tenant_id"] == str(tenant_b)


def test_programs_read_survives_with_operator_guc_no_500(substrate) -> None:
    """mig-134 empty-GUC predicate fix, end-to-end at the read layer: once the operator GUC
    is plumbed through vtr_connection, a VALID assigned operator's /programs read must return
    cleanly (NOT 500) — the NULLIF(...)-guarded app_vtr_operator() never throws on an
    unset/empty GUC. Substrate-gated: until B1 plumbs the operator, this xfails (the read
    runs with no operator GUC, the pre-fix behaviour). It flips to PASS on B1's merge.

    Falsifiability: if the predicate fix regressed to current_setting(...)::uuid on an empty
    GUC, the vtr_workflow_controls read inside /programs would raise — but /programs catches
    that into degraded=true (fail-open), so the falsifiable signal here is that the read
    RETURNS and is NOT degraded for a real operator with a real GUC."""
    if not _mig134_present(substrate.dsn):
        pytest.xfail(
            "mig-134 operator-GUC plumbing not applied yet — B1 lands concurrently; "
            "this leg flips to PASS on that merge"
        )
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    _seed_run(dsn, tenant, run_type="agent_dispatch", status="completed")
    out = rc.programs(str(tenant), **_hdr(op=op))
    assert out["tenant_id"] == str(tenant)
    assert out["degraded"] is False, (
        "a real operator with a real GUC must read holds cleanly — not fail-open degraded"
    )
    assert isinstance(out["holds"], list)


def _seed_step(
    dsn: str,
    run: UUID,
    tenant: UUID,
    seq: int,
    step_kind: str,
    step_name: str,
    *,
    input_env: dict[str, Any] | None = None,
    output_env: dict[str, Any] | None = None,
) -> None:
    from psycopg.types.json import Jsonb

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_steps (run_id, tenant_id, step_seq, step_kind, step_name, "
            "status, input_envelope, output_envelope) VALUES (%s, %s, %s, %s, %s, "
            "'completed', %s, %s)",
            (
                str(run), str(tenant), seq, step_kind, step_name,
                Jsonb(input_env) if input_env is not None else None,
                Jsonb(output_env) if output_env is not None else None,
            ),
        )


def test_timeline_step_tier_annotation(substrate) -> None:
    """VT-375 Task 3: every timeline row carries a 'tier' field — pure response annotation,
    no view/RLS change. A REGISTERED controllable seam (agent_dispatch:candidate_build) reads
    'controllable'; an unregistered kind (a brain micro-step) and a run_control_intervention
    companion row read 'observed' (the honest 'not controllable' label, plan §1)."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    # agent_dispatch run_type → workflow_kind agent_dispatch; candidate_build IS a controllable
    # registry seam.
    run = _seed_run(dsn, tenant, run_type="agent_dispatch", status="completed")
    _seed_step(dsn, run, tenant, 0, "tool_call", "candidate_build")  # controllable seam
    _seed_step(dsn, run, tenant, 1, "state_transition", "some_brain_micro_step")  # unregistered
    # the colon-namespaced companion row — its post-colon part matches a controllable step but
    # the row is observability ABOUT the seam, never the seam → must stay observed.
    _seed_step(
        dsn, run, tenant, 2, "run_control_intervention", "agent_dispatch:candidate_build"
    )

    out = rc.timeline(str(run), **_hdr(op=op))
    by_name = {s["step_name"]: s["tier"] for s in out["steps"]}
    assert all("tier" in s for s in out["steps"]), "every timeline row must carry a tier field"
    assert by_name["candidate_build"] == "controllable"
    assert by_name["some_brain_micro_step"] == "observed"
    assert by_name["agent_dispatch:candidate_build"] == "observed", (
        "a run_control_intervention companion row is observed, never controllable"
    )


def test_timeline_step_tier_unregistered_kind_observed(substrate) -> None:
    """A run whose run_type has NO registry mapping (only legacy/known run_types map) yields
    'observed' for every step — the registry lookup misses, so nothing is controllable."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    # 'duplicate_rejected' is a valid pipeline_runs status but a run_type with no RUN_TYPE_TO_KIND
    # entry; use an unmapped run_type so the kind lookup returns None.
    run = _seed_run(dsn, tenant, run_type="some_unmapped_run_type", status="completed")
    _seed_step(dsn, run, tenant, 0, "tool_call", "candidate_build")

    out = rc.timeline(str(run), **_hdr(op=op))
    tiers = {s["tier"] for s in out["steps"]}
    assert tiers == {"observed"}, (
        f"an unregistered run_type must annotate every step observed, got {tiers}"
    )
    # VT-376 run-level legs for the unmapped kind: not rerunnable, and no why-copy (the
    # pinned copy exists only for the three forbidden registry kinds).
    assert out["rerunnable"] is False
    assert out["forbidden_reason"] is None


# ---------------------------------------------------------------------------
# 6b. /timeline — VT-376 annotations (allowed_keys / rerunnable / forbidden_reason)
#     + the mig-132 projections through the API read path
# ---------------------------------------------------------------------------


def test_timeline_allowed_keys_annotation(substrate) -> None:
    """VT-376 (build contract §B1.3): every timeline step carries ``allowed_keys`` — the
    registry key NAMES for CONTROLLABLE steps (the override form's field list), [] for
    everything else (a controllable step with an empty registry allowlist, an observed
    micro-step, an intervention companion row) — so the dialog can never render a field
    for a step the registry does not allow-list."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    run = _seed_run(dsn, tenant, run_type="agent_dispatch", status="completed")
    _seed_step(dsn, run, tenant, 0, "tool_call", "candidate_build")  # allowed {'limit'}
    _seed_step(dsn, run, tenant, 1, "tool_call", "compose_drafts")  # allowed {'model'}
    _seed_step(dsn, run, tenant, 2, "tool_call", "persist_batch")  # controllable, ∅ keys
    _seed_step(dsn, run, tenant, 3, "state_transition", "some_brain_micro_step")  # observed
    _seed_step(dsn, run, tenant, 4, "run_control_intervention", "agent_dispatch:candidate_build")

    out = rc.timeline(str(run), **_hdr(op=op))
    by_name = {s["step_name"]: s for s in out["steps"]}
    assert all("allowed_keys" in s for s in out["steps"]), "every step row carries allowed_keys"
    assert by_name["candidate_build"]["allowed_keys"] == ["limit"]
    assert by_name["compose_drafts"]["allowed_keys"] == ["model"]
    assert by_name["persist_batch"]["allowed_keys"] == []  # controllable but pins nothing
    assert by_name["persist_batch"]["tier"] == "controllable"
    assert by_name["some_brain_micro_step"]["allowed_keys"] == []
    assert by_name["agent_dispatch:candidate_build"]["allowed_keys"] == [], (
        "an intervention companion row is observed — it must never advertise keys"
    )


@pytest.mark.parametrize(
    ("run_type", "rerunnable", "why"),
    [
        ("agent_dispatch", True, None),
        ("plan_generate", True, None),
        ("twilio_inbound", False, "message-dedup semantics"),  # → webhook_inbound
        ("trial_sweep", False, "duplicate-nudge risk"),
        ("campaign_send", False, "kg-duplication"),
    ],
)
def test_timeline_run_level_rerunnable_and_forbidden_reason(
    substrate, run_type: str, rerunnable: bool, why: str | None
) -> None:
    """VT-376 (build contract §B1.3): the run-level payload carries ``rerunnable`` (kind
    in RERUNNABLE) and the pinned per-kind why-copy for the rerun-forbidden kinds — the
    substrate for B2's 'no button + why' rendering. Rerunnable kinds carry None."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    run = _seed_run(dsn, tenant, run_type=run_type, status="completed")
    out = rc.timeline(str(run), **_hdr(op=op))
    assert out["rerunnable"] is rerunnable
    assert out["forbidden_reason"] == why


def test_timeline_mig132_projection_through_api(substrate) -> None:
    """mig-132 through the API read path (build contract §B1.6): the three formerly
    whole-envelope kinds project EXACTLY their writer-pinned keys, and an injected
    foreign key/value never reaches the /timeline response — the endpoint reads the
    hardened view, so the projection holds end-to-end, not just at the SQL layer."""
    import json as _json

    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    run = _seed_run(dsn, tenant, run_type="agent_dispatch", status="completed")
    _seed_step(
        dsn, run, tenant, 0, "agent_invocation", "brain_dispatch_entry",
        input_env={
            "inbound_body_len": 42,
            "trigger": "owner_substantive_message",
            "dispatched_at": "2026-06-12T00:00:00+00:00",
            "smuggled": "Ramesh-leak",
        },
        output_env={"reason": "ok", "smuggled": "Ramesh-leak"},
    )
    _seed_step(
        dsn, run, tenant, 1, "aborted_hard_limit", "brain_dispatch_aborted",
        input_env={"reason": "hard_limit_exceeded:tokens", "inbound_body_len": 9,
                   "smuggled": "Ramesh-leak"},
        output_env={"axis": "tokens", "observed": 11.0, "limit": 10.0, "smuggled": "Ramesh-leak"},
    )
    _seed_step(
        dsn, run, tenant, 2, "tenant_isolation_breach", "context_isolation_preflight",
        output_env={
            "layer": "pre_flight",
            "offending_ids": {"campaigns": ["c-1"]},
            "counts": {"campaigns": 1},
            "smuggled": "Ramesh-leak",
        },
    )

    out = rc.timeline(str(run), **_hdr(op=op))
    by_seq = {s["step_seq"]: s for s in out["steps"]}
    assert by_seq[0]["input_envelope"] == {
        "inbound_body_len": 42,
        "trigger": "owner_substantive_message",
        "dispatched_at": "2026-06-12T00:00:00+00:00",
    }
    assert by_seq[0]["output_envelope"] == {"reason": "ok"}
    assert by_seq[1]["input_envelope"] == {
        "reason": "hard_limit_exceeded:tokens",
        "inbound_body_len": 9,
    }
    assert by_seq[1]["output_envelope"] == {"axis": "tokens", "observed": 11.0, "limit": 10.0}
    assert by_seq[2]["output_envelope"] == {
        "layer": "pre_flight",
        "offending_ids": {"campaigns": ["c-1"]},
        "counts": {"campaigns": 1},
    }
    surface = _json.dumps(out["steps"], default=str)
    assert "smuggled" not in surface and "Ramesh" not in surface, (
        "an injected key/value crossed the mig-132 projection into the API response"
    )


# ---------------------------------------------------------------------------
# 7. THE BINDING registry-populates acceptance (VT-361 lesson)
# ---------------------------------------------------------------------------


def test_override_reason_redacts_customer_name_AND_registry_populates(substrate) -> None:
    """BINDING (fix-contract Test-B; POST-ACK ADDENDA): write-time redaction of a /override reason
    must actually consult a POPULATED customer-name registry.

    The tripwire (VT-361 lesson): the phone in the reason is pattern-redacted regardless of the
    registry, so a passing "phone gone" assertion proves NOTHING about the registry. The name —
    a 2-word display name, the only shape the free-text scan matches — is redacted ONLY when the
    registry returns its known customer names. We assert:
      (a) the raw step_overrides.reason has the name replaced by the <customer_name> token AND the
          plaintext name is gone (output side);
      (b) make_name_registry(tenant) is itself a POPULATED predicate (input side) — an inert/empty
          registry (zero live customers, the VT-170 production-inert default) returns False here
          and FAILS the test. This is the assertion the redaction output alone cannot give.
    """
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    display_name = "Rajesh Kumar"  # 2-token: the free-text registry scan matches bigrams
    _seed_customer(dsn, tenant, display_name)
    customer_registry.invalidate_all()  # force a true read, never a stale-empty cache

    # (b) input side — the registry actually populated from the seeded customer. An inert/empty
    # registry returns False; this line is the inert-registry FAIL gate.
    registry = customer_registry.make_name_registry(str(tenant))
    assert registry(display_name.casefold()) is True, (
        "make_name_registry returned an INERT/empty registry — the seeded customer was not read; "
        "write-time name redaction would silently no-op (VT-361 lesson)"
    )

    out = _override(
        op, tenant, workflow_kind="agent_dispatch", step_name="candidate_build",
        pinned_input={"limit": 1},
        reason=f"Customer {display_name} at 9876543210 wants a refund",
    )
    assert out["ok"] is True

    # (a) output side — read the RAW row as service role (RLS-bypassed); both PII forms gone.
    raw_reason = _override_row(dsn, out["override_id"])["reason"]
    assert "<customer_name>" in raw_reason, (
        f"the registry-known name was not tokenised; raw reason = {raw_reason!r}"
    )
    assert display_name not in raw_reason  # the plaintext name is gone
    assert "9876543210" not in raw_reason  # the phone is pattern-redacted (registry-independent)


# ---------------------------------------------------------------------------
# 8. /programs/{tenant_id} — read-only projection (VT-375 Phase B, B1)
# ---------------------------------------------------------------------------


def _programs(op: str, tenant: UUID, **hdr: Any) -> dict[str, Any]:
    return rc.programs(str(tenant), **hdr)


def _set_trial_start(dsn: str, tenant: UUID, started_at: datetime) -> None:
    """Set trial_started_at so the forecast leg projects a warn/expiry inside the window."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE tenants SET trial_started_at = %s, paid_conversion_at = NULL "
            "WHERE id = %s",
            (started_at, str(tenant)),
        )


def _seed_work_item(dsn: str, tenant: UUID, status: str = "dispatched") -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
            "VALUES (%s, %s, 'sales_recovery', %s)",
            (str(tenant), f"cust-{uuid4().hex[:8]}", status),
        )


def _seed_plan(
    dsn: str,
    tenant: UUID,
    roadmap: list[dict[str, Any]],
    *,
    created_at: datetime | None = None,
) -> None:
    from psycopg.types.json import Jsonb

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO business_plan (tenant_id, version, summary_json, roadmap_json, "
            "fact_bundle_json, generated_by, created_at) "
            "VALUES (%s, 1, '{}', %s, '{}', 'test', %s)",
            (str(tenant), Jsonb(roadmap), created_at or datetime.now(timezone.utc)),
        )


def test_programs_auth_unassigned_tenant_403(substrate) -> None:
    """The GET inherits the Gap-6 assignment gate (tenant from the PATH): an unassigned
    operator is 403, never a read."""
    op = str(uuid4())
    tenant = _new_tenant(substrate.dsn)  # no assignment
    with pytest.raises(HTTPException) as exc:
        _programs(op, tenant, **_hdr(op=op))
    assert exc.value.status_code == 403


def test_programs_auth_missing_jwt_403(substrate) -> None:
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _programs(op, tenant, **_hdr(jwt=None))
    assert exc.value.status_code == 403


def test_programs_auth_bad_secret_403(substrate) -> None:
    op, tenant = _assigned(substrate.dsn)
    with pytest.raises(HTTPException) as exc:
        _programs(op, tenant, **_hdr(secret="wrong", jwt=_op_jwt(op)))
    assert exc.value.status_code == 403


def test_programs_response_shape_and_past_running(substrate) -> None:
    """Shape pin + past/running split: a terminal run lands in ``past`` with lineage fields;
    a 'running' run lands in ``running`` with the ``active_hold`` flag. ``degraded`` is False
    on a healthy control read; ``holds`` is a list."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    terminal = _seed_run(dsn, tenant, run_type="agent_dispatch", status="completed")
    running = _seed_run(dsn, tenant, run_type="agent_dispatch", status="running")

    out = _programs(op, tenant, **_hdr(op=op))
    assert set(out) == {"tenant_id", "past", "running", "upcoming_7d", "holds", "degraded"}
    assert out["tenant_id"] == str(tenant)
    assert out["degraded"] is False
    assert isinstance(out["holds"], list)

    past_ids = {r["run_id"] for r in out["past"]}
    running_ids = {r["run_id"] for r in out["running"]}
    assert str(terminal) in past_ids and str(terminal) not in running_ids
    assert str(running) in running_ids and str(running) not in past_ids
    # past row carries the pinned structural fields.
    past_row = next(r for r in out["past"] if r["run_id"] == str(terminal))
    assert set(past_row) == {
        "run_id", "run_type", "status", "started_at", "ended_at",
        "rerun_of_run_id", "rerun_from_step", "step_count",
    }
    # running row additionally carries active_hold; no hold seeded → False.
    run_row = next(r for r in out["running"] if r["run_id"] == str(running))
    assert run_row["active_hold"] is False


def test_programs_running_active_hold_true_when_kind_paused(substrate) -> None:
    """active_hold reflects a live hold: pausing the run's workflow_kind flips active_hold,
    and the hold appears in ``holds`` (workflow_kind + set_at, NEVER reason)."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    running = _seed_run(dsn, tenant, run_type="agent_dispatch", status="running")
    _pause(op, tenant, **_hdr(op=op))  # pauses agent_dispatch for this tenant

    out = _programs(op, tenant, **_hdr(op=op))
    run_row = next(r for r in out["running"] if r["run_id"] == str(running))
    assert run_row["active_hold"] is True
    assert len(out["holds"]) == 1
    hold = out["holds"][0]
    assert set(hold) == {"workflow_kind", "set_at"}  # NO reason, no operator id
    assert hold["workflow_kind"] == "agent_dispatch"


def test_programs_upcoming_trial_sweep_computed(substrate) -> None:
    """COMPUTED trial forecast: a trial started 28 days ago projects BOTH a warn (day 28,
    now-ish) and an expiry (day 30, +2d) inside the 7d window — kind='trial_sweep',
    source='trial.yaml forecast'. No new state written (the dates are derived)."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    _set_trial_start(dsn, tenant, datetime.now(timezone.utc) - timedelta(days=28))

    out = _programs(op, tenant, **_hdr(op=op))
    trial = [u for u in out["upcoming_7d"] if u["kind"] == "trial_sweep"]
    assert trial, f"expected a trial_sweep forecast, got {out['upcoming_7d']}"
    assert all(u["source"] == "trial.yaml forecast" for u in trial)
    # day-30 expiry must be in-window (+2d); the day-28 warn is ~now.
    assert any("expiry" in u["label"] for u in trial)


def test_programs_upcoming_agent_dispatch_and_roadmap_computed(substrate) -> None:
    """COMPUTED legs: a queued 'dispatched' work item projects kind='agent_dispatch'
    (source='agent_work_items', label names the next sweep); a roadmap month-2 item on a
    plan created ~26 days ago projects kind='roadmap' (window = created_at + 30d ≈ +4d, in
    the 7d horizon; source='business_plan v1'). A month-12 item on the same plan is far out
    of window and must NOT appear."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    _seed_work_item(dsn, tenant, status="dispatched")
    # The objective is LLM free text and must NEVER reach the VTR label (PII boundary). The
    # owning_agent is the closed enum the structural label surfaces instead.
    secret_objective = "Launch the loyalty push for Rajesh Kumar"
    _seed_plan(
        dsn, tenant,
        roadmap=[
            {"month": 2, "objective": secret_objective, "owning_agent": "sales_recovery"},
            {"month": 12, "objective": "Out-of-window milestone", "owning_agent": "reputation"},
        ],
        created_at=datetime.now(timezone.utc) - timedelta(days=26),
    )

    out = _programs(op, tenant, **_hdr(op=op))
    kinds = {u["kind"] for u in out["upcoming_7d"]}
    assert "agent_dispatch" in kinds, f"queued work item not forecast: {out['upcoming_7d']}"
    assert "roadmap" in kinds, f"month-2 roadmap window not forecast: {out['upcoming_7d']}"
    agent = next(u for u in out["upcoming_7d"] if u["kind"] == "agent_dispatch")
    assert agent["source"] == "agent_work_items" and "sweep" in agent["label"]
    roadmaps = [u for u in out["upcoming_7d"] if u["kind"] == "roadmap"]
    assert len(roadmaps) == 1, f"only the in-window month-2 item should forecast: {roadmaps}"
    assert roadmaps[0]["source"] == "business_plan v1"
    # STRUCTURAL label: month number + owning_agent enum, and ZERO objective free text.
    label = roadmaps[0]["label"]
    assert "month 2" in label
    assert "sales_recovery" in label, f"owning_agent enum must be on the label: {label!r}"
    assert secret_objective not in label  # the LLM free text never crosses onto the label
    assert "Rajesh" not in label and "Kumar" not in label  # nor any token of it
    assert "loyalty" not in label and "Launch" not in label


def test_programs_terminal_work_item_not_forecast(substrate) -> None:
    """A TERMINAL ('sent') work item is NOT a queued dispatch — it must not appear in the
    forecast (only non-terminal pre-run 'dispatched' rows do)."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    _seed_work_item(dsn, tenant, status="sent")  # terminal

    out = _programs(op, tenant, **_hdr(op=op))
    assert not [u for u in out["upcoming_7d"] if u["kind"] == "agent_dispatch"], (
        "a terminal work item must not be forecast as a queued dispatch"
    )


def test_programs_degraded_true_when_control_read_raises(substrate, monkeypatch) -> None:
    """degraded=true (fail-OPEN): when the workflow_controls (holds) read raises, the
    projection still returns past/running/upcoming, ``holds`` is empty, and ``degraded`` is
    True — the panel then shows the pause-state-unverifiable copy. The pipeline_runs read is
    unaffected (it is on the service pool, not the vtr view)."""
    dsn = substrate.dsn
    op, tenant = _assigned(dsn)
    _seed_run(dsn, tenant, run_type="agent_dispatch", status="completed")

    from contextlib import contextmanager

    @contextmanager
    def _boom_vtr(*_a: Any, **_k: Any):  # type: ignore[no-untyped-def]
        raise RuntimeError("synthetic vtr-view outage")
        yield  # pragma: no cover

    monkeypatch.setattr(rc, "vtr_connection", _boom_vtr)

    out = _programs(op, tenant, **_hdr(op=op))
    assert out["degraded"] is True
    assert out["holds"] == []
    assert len(out["past"]) >= 1, "the run projection must still return when controls degrade"
