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
