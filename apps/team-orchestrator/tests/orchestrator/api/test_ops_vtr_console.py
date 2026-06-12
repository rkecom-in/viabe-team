"""VT-370 Gap-6 — the canary-3 battery as in-repo tests (plan §7 ship-blocking acceptance).

Covers, against a real migrated PG:

  1. ``ops_common.require_vtr_action`` — the structurally unskippable gate: no JWT → 403 EVEN
     WITH a valid internal secret (the run-control-inheritance-broken proof); bad secret → 403;
     body operator_id != claim → 403; unassigned tenant → 403 AND the deny is itself audited;
     FAZAL_OWNER_UUID break-glass passes. Real HS256 JWTs minted in-test.
  2. ``require_exception_tier`` — env unset/empty ⇒ 403 (never an open gate); mismatch ⇒ 403;
     match passes.
  3. ``vtr-plan-edit`` flow vs a seeded plan: clean edit + correct expected_prev_version → new
     version + a ``plan_edit`` ops_audit row carrying field NAMES only (CL-390); replay/stale →
     409; foreign item_id → 404 (the _locate leg); unassigned → 403 + deny audit (the authz leg —
     the FLAGGED double-leg defense, each leg pinned independently); an uncited number → 400 with
     the violation echo scrubbed.
  4. ``vtr-batch-cancel`` — NO tenant_id in the body (model pin); tenant derived from the batch
     row; foreign-tenant operator → 403 + deny audit; success cancels the batch, halts its
     drafted rows, audits.
  5. ``vtr-batch-drafts`` — non-exception operator → 403; Fazal sees params AND the
     ``draft_params_reveal`` audit row exists (audit-before-read).
  6. The mig-130 view invariants (the acceptance pins): vtr_business_plan = exactly 1 row/tenant,
     diff_from_prev VALUES stripped (keys only); vtr_draft_batches / vtr_agent_autonomy column
     exclusions; ``SET ROLE app_vtr_role`` raw-table reads → permission denied (DB-enforced,
     not app-trust).
  7. Seam units: cancel_batch idempotent on a terminal batch; unfreeze cancels nothing;
     StaleVersion on a version mismatch.

DB substrate mirrors ``tests/orchestrator/agents/test_autonomy.py``: importorskip
psycopg+dbos(+langgraph), skipif no DATABASE_URL, module fixture apply_migrations + launch_dbos;
seeds via direct autocommit psycopg (service role). Endpoint handlers are called DIRECTLY as plain
functions with EVERY header param passed explicitly (a defaulted Query/Header is a truthy
FieldInfo when bypassing FastAPI's DI — the repo-memory trap).
"""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")
pytest.importorskip("langgraph")  # orchestrator.db -> orchestrator.graph imports langgraph
pytest.importorskip("fastapi")
pytest.importorskip("jwt")

import jwt as pyjwt  # noqa: E402 — after dependency skip guards
import psycopg  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from psycopg import errors as pg_errors  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

from orchestrator.agents.autonomy import (  # noqa: E402
    cancel_batch,
    vtr_autonomy_override,
)
from orchestrator.api import ops_common, ops_vtr_console as console  # noqa: E402
from orchestrator.business_plan import seams, store  # noqa: E402
from orchestrator.business_plan.seams import StaleVersion  # noqa: E402
from orchestrator.db import tenant_connection  # noqa: E402
from orchestrator.privacy.vtr import vtr_connection  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-370 VTR console tests skipped",
)

pytestmark = requires_db

AGENT = "sales_recovery"  # in OWNING_AGENTS too (the override endpoint validates against it)
_TEMPLATE = "team_winback_simple"
_TEST_INTERNAL_SECRET = "unit-test-internal-not-a-secret"
_TEST_JWT_KEY = "unit-test-jwt-signing-key-not-a-secret-0000"  # >=32 bytes (HS256 hygiene)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the service pool / tenant_connection exist."""
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


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERNAL_API_SECRET", _TEST_INTERNAL_SECRET)
    monkeypatch.setenv("OPERATOR_JWT_SECRET", _TEST_JWT_KEY)
    # No ambient break-glass: tests that want Fazal set FAZAL_OWNER_UUID explicitly.
    monkeypatch.delenv("FAZAL_OWNER_UUID", raising=False)


def _op_jwt(operator_id: str, *, secret: str = _TEST_JWT_KEY) -> str:
    now = int(time.time())
    return pyjwt.encode(
        {"operator_claim": True, "operator_id": operator_id, "aud": "authenticated",
         "iat": now, "exp": now + 300},
        secret, algorithm="HS256",
    )


# --- seeding helpers (direct service-role connection — RLS bypassed at seed) ---


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, whatsapp_number) "
            "VALUES ('VT-370 vtr console test', 'founding', 'trial', now(), 'restaurant', %s) "
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


def _seed_customer(dsn: str, tenant: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO customers (tenant_id, display_name) "
            "VALUES (%s, 'Vtr Console Cust') RETURNING id",
            (str(tenant),),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_batch(
    dsn: str, tenant: UUID, *, status: str = "awaiting_approval", agent: str = AGENT
) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        wi = conn.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
            "VALUES (%s, %s, %s, 'drafting') RETURNING id",
            (str(tenant), f"item-{uuid4().hex[:12]}", agent),
        ).fetchone()
        assert wi is not None
        row = conn.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (str(tenant), str(wi[0]), agent, status),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_draft(
    dsn: str, tenant: UUID, batch: UUID, customer: UUID,
    *, params: dict[str, Any] | None = None, status: str = "drafted",
) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_drafts (tenant_id, batch_id, customer_id, template_name, "
            "params, status) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (str(tenant), str(batch), str(customer), _TEMPLATE, Jsonb(params or {}), status),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_autonomy_row(
    dsn: str, tenant: UUID, *, agent: str = AGENT, frozen: bool = False
) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_agent_autonomy (tenant_id, agent, frozen) VALUES (%s, %s, %s)",
            (str(tenant), agent, frozen),
        )


def _batch_row(dsn: str, tenant: UUID, batch: UUID) -> dict[str, Any]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status FROM agent_draft_batches WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        ).fetchone()
    assert row is not None
    return {"status": row[0]}


def _draft_row(dsn: str, tenant: UUID, draft: UUID) -> dict[str, Any]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, skip_reason FROM agent_drafts WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(draft)),
        ).fetchone()
    assert row is not None
    return {"status": row[0], "skip_reason": row[1]}


def _audit_rows(dsn: str, action: str, operator: str) -> list[dict[str, Any]]:
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT operator_id, tenant_id, action, target_kind, target_id, detail "
            "FROM ops_audit WHERE action = %s AND operator_id = %s ORDER BY created_at",
            (action, operator),
        ).fetchall()


# --- plan seeding (store.write_new_version — the real spine, not fixtures-by-hand) ---

_FACTS = {
    "F1": {"key": "google_rating", "value": 4.2, "source": "gbp"},
    "F2": {"key": "weekly_orders", "value": 120, "source": "pos"},
}
_SUMMARY = {
    "text": "Here is the verified plan.",
    "text_hi": "",
    "cited_facts": ["F1", "F2"],
    "headline_metrics": {},
}


def _item(item_id: str, seq: int, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "item_id": item_id, "seq": seq, "month": 1,
        "objective": "Reply to every review",
        "why": "Ratings can improve with steady replies.",
        "cited_facts": ["F1"], "owning_agent": "reputation",
        "owner_action_needed": False, "owner_action": None, "owner_action_hi": None,
        "status": "accepted", "provenance": {},
    }
    base.update(over)
    return base


def _seed_plan(tenant: UUID, items: list[dict[str, Any]] | None = None) -> int:
    return store.write_new_version(
        tenant,
        summary=_SUMMARY,
        roadmap=items if items is not None else [_item("it-1", 1), _item("it-2", 2)],
        fact_bundle=_FACTS,
        generated_by="gap4_generator",
    )


def _max_plan_version(dsn: str, tenant: UUID) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM business_plan WHERE tenant_id = %s",
            (str(tenant),),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _plan_row(dsn: str, tenant: UUID, version: int) -> dict[str, Any]:
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT generated_by, roadmap_json FROM business_plan "
            "WHERE tenant_id = %s AND version = %s",
            (str(tenant), version),
        ).fetchone()
    assert row is not None
    return dict(row)


# ---------------------------------------------------------------------------
# 1. require_vtr_action — the gate, step by step
# ---------------------------------------------------------------------------


def _gate_direct(dsn: str, **kw: Any) -> str:
    """Call require_vtr_action on a real autocommit cursor (so the deny audit persists)."""
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        return ops_common.require_vtr_action(cur, **kw)


def test_gate_no_jwt_403_even_with_valid_secret(substrate, monkeypatch) -> None:
    """The run-control-inheritance-broken proof: a valid INTERNAL_API_SECRET alone NEVER
    suffices for a VTR action — no X-Operator-Jwt is a hard 403."""
    _env(monkeypatch)
    op = str(uuid4())
    with pytest.raises(HTTPException) as exc:
        _gate_direct(
            substrate.dsn, x_internal_secret=_TEST_INTERNAL_SECRET, x_operator_jwt=None,
            body_operator_id=op, tenant_id=str(_new_tenant(substrate.dsn)),
            deny_action="test_denied",
        )
    assert exc.value.status_code == 403


def test_gate_bad_secret_403(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    op = str(uuid4())
    with pytest.raises(HTTPException) as exc:
        _gate_direct(
            substrate.dsn, x_internal_secret="wrong", x_operator_jwt=_op_jwt(op),
            body_operator_id=op, tenant_id=str(_new_tenant(substrate.dsn)),
            deny_action="test_denied",
        )
    assert exc.value.status_code == 403


def test_gate_body_operator_mismatch_403(substrate, monkeypatch) -> None:
    """A valid JWT signed for B cannot act as body-claimed A (no body-trusted attribution)."""
    _env(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _gate_direct(
            substrate.dsn, x_internal_secret=_TEST_INTERNAL_SECRET, x_operator_jwt=_op_jwt(str(uuid4())),
            body_operator_id=str(uuid4()), tenant_id=str(_new_tenant(substrate.dsn)),
            deny_action="test_denied",
        )
    assert exc.value.status_code == 403


def test_gate_unassigned_tenant_403_and_deny_audited(substrate, monkeypatch) -> None:
    """Fail-CLOSED on assignment AND the denial itself lands in ops_audit (fail-closed + visible)."""
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)  # NO operator_assignments row
    with pytest.raises(HTTPException) as exc:
        _gate_direct(
            dsn, x_internal_secret=_TEST_INTERNAL_SECRET, x_operator_jwt=_op_jwt(op),
            body_operator_id=op, tenant_id=str(tenant), deny_action="test_denied",
        )
    assert exc.value.status_code == 403
    rows = _audit_rows(dsn, "test_denied", op)
    assert len(rows) == 1
    assert str(rows[0]["tenant_id"]) == str(tenant)
    assert rows[0]["target_kind"] == "tenant"


def test_gate_assigned_operator_passes(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)
    _assign(dsn, op, tenant)
    verified = _gate_direct(
        dsn, x_internal_secret=_TEST_INTERNAL_SECRET, x_operator_jwt=_op_jwt(op),
        body_operator_id=op, tenant_id=str(tenant), deny_action="test_denied",
    )
    assert verified == op


def test_gate_fazal_break_glass_passes(substrate, monkeypatch) -> None:
    """FAZAL_OWNER_UUID = VTAdmin break-glass: passes WITHOUT an assignment row."""
    _env(monkeypatch)
    dsn = substrate.dsn
    fazal = str(uuid4())
    monkeypatch.setenv("FAZAL_OWNER_UUID", fazal)
    verified = _gate_direct(
        dsn, x_internal_secret=_TEST_INTERNAL_SECRET, x_operator_jwt=_op_jwt(fazal),
        body_operator_id=fazal, tenant_id=str(_new_tenant(dsn)), deny_action="test_denied",
    )
    assert verified == fazal


# ---------------------------------------------------------------------------
# 2. require_exception_tier — never an open gate
# ---------------------------------------------------------------------------


def test_exception_tier_env_unset_403(monkeypatch) -> None:
    """The `if fazal and` idiom pin: an UNSET (or empty) FAZAL_OWNER_UUID is a closed gate —
    a bare == would match empty-to-empty."""
    monkeypatch.delenv("FAZAL_OWNER_UUID", raising=False)
    with pytest.raises(HTTPException) as exc:
        ops_common.require_exception_tier(str(uuid4()))
    assert exc.value.status_code == 403

    monkeypatch.setenv("FAZAL_OWNER_UUID", "")  # empty string — same closed gate
    with pytest.raises(HTTPException) as exc:
        ops_common.require_exception_tier("")
    assert exc.value.status_code == 403


def test_exception_tier_mismatch_403(monkeypatch) -> None:
    monkeypatch.setenv("FAZAL_OWNER_UUID", str(uuid4()))
    with pytest.raises(HTTPException) as exc:
        ops_common.require_exception_tier(str(uuid4()))
    assert exc.value.status_code == 403


def test_exception_tier_match_passes(monkeypatch) -> None:
    fazal = str(uuid4())
    monkeypatch.setenv("FAZAL_OWNER_UUID", fazal)
    ops_common.require_exception_tier(fazal)  # no raise


# ---------------------------------------------------------------------------
# 3. vtr-plan-edit — the flow vs a seeded plan
# ---------------------------------------------------------------------------


def _edit(
    op: str, tenant: UUID, *, item_id: str = "it-1", patch: dict[str, Any],
    expected_prev_version: int, jwt_token: str | None = "MINT",
    secret: str = _TEST_INTERNAL_SECRET,
) -> dict[str, Any]:
    """Direct handler call — every header param passed explicitly (the FieldInfo trap)."""
    return console.vtr_plan_edit(
        console.VtrPlanEditBody(
            operator_id=op, tenant_id=str(tenant), item_id=item_id, patch=patch,
            expected_prev_version=expected_prev_version,
        ),
        x_internal_secret=secret,
        x_operator_jwt=_op_jwt(op) if jwt_token == "MINT" else jwt_token,
    )


def test_plan_edit_clean_edit_mints_version_and_audits_names_only(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)
    _assign(dsn, op, tenant)
    v1 = _seed_plan(tenant)
    assert v1 == 1

    out = _edit(op, tenant, patch={"why": "Rating sits at 4.2 today."}, expected_prev_version=v1)
    assert out == {"ok": True, "new_version": 2}

    # The new version is attributed to the VERIFIED claim id and carries vtr_edit provenance.
    row = _plan_row(dsn, tenant, 2)
    assert row["generated_by"] == f"vtr:{op}"
    item = next(i for i in row["roadmap_json"] if i["item_id"] == "it-1")
    assert item["why"] == "Rating sits at 4.2 today."
    assert item["provenance"]["origin"] == "vtr_edit"
    assert item["provenance"]["prev_version"] == 1

    # ops_audit: field NAMES + versions ONLY — never patch values (CL-390).
    audits = _audit_rows(dsn, "plan_edit", op)
    assert len(audits) == 1
    assert audits[0]["target_kind"] == "roadmap_item"
    assert audits[0]["target_id"] == "it-1"
    detail = audits[0]["detail"] or ""
    assert "why" in detail
    assert "v1" in detail and "v2" in detail
    assert "4.2" not in detail and "sits" not in detail  # no values


def test_plan_edit_replay_and_stale_version_409(substrate, monkeypatch) -> None:
    """Plan-edit is NOT idempotent (every POST appends an immutable version) — a replayed body
    and a stale expected_prev_version must both lose with 409, minting nothing."""
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)
    _assign(dsn, op, tenant)
    v1 = _seed_plan(tenant)

    assert _edit(op, tenant, patch={"month": 2}, expected_prev_version=v1)["new_version"] == 2

    # Replay the SAME body → 409 (its expected_prev_version is now stale).
    with pytest.raises(HTTPException) as exc:
        _edit(op, tenant, patch={"month": 2}, expected_prev_version=v1)
    assert exc.value.status_code == 409

    # An arbitrary stale/future version token → 409.
    with pytest.raises(HTTPException) as exc:
        _edit(op, tenant, patch={"month": 3}, expected_prev_version=99)
    assert exc.value.status_code == 409

    assert _max_plan_version(dsn, tenant) == 2  # neither 409 minted a version


def test_plan_edit_foreign_item_404_locate_leg(substrate, monkeypatch) -> None:
    """IDOR leg 2 (_locate): an item_id from ANOTHER tenant's plan — or none at all — does not
    resolve under the assigned tenant. 404, nothing minted."""
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant_a = _new_tenant(dsn)
    tenant_b = _new_tenant(dsn)
    _assign(dsn, op, tenant_a)
    _seed_plan(tenant_a)
    _seed_plan(tenant_b, [_item("bt-1", 1), _item("bt-2", 2)])

    for foreign_item in ("bt-1", f"it-{uuid4().hex[:8]}"):
        with pytest.raises(HTTPException) as exc:
            _edit(op, tenant_a, item_id=foreign_item, patch={"month": 2}, expected_prev_version=1)
        assert exc.value.status_code == 404
    assert _max_plan_version(dsn, tenant_a) == 1
    assert _max_plan_version(dsn, tenant_b) == 1


def test_plan_edit_unassigned_403_authz_leg_and_deny_audited(substrate, monkeypatch) -> None:
    """IDOR leg 1 (the gate): an operator NOT assigned to the tenant is 403'd BEFORE the seam,
    the deny is audited, and nothing is minted."""
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)  # no assignment
    _seed_plan(tenant)

    with pytest.raises(HTTPException) as exc:
        _edit(op, tenant, patch={"month": 2}, expected_prev_version=1)
    assert exc.value.status_code == 403
    assert len(_audit_rows(dsn, "plan_edit_denied", op)) == 1
    assert _max_plan_version(dsn, tenant) == 1


def test_plan_edit_no_jwt_403_even_with_valid_secret(substrate, monkeypatch) -> None:
    """The endpoint-level inheritance proof: the handler routes through the gate — a valid
    internal secret with NO X-Operator-Jwt is 403, never a mint."""
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)
    _assign(dsn, op, tenant)
    _seed_plan(tenant)

    with pytest.raises(HTTPException) as exc:
        _edit(op, tenant, patch={"month": 2}, expected_prev_version=1, jwt_token=None)
    assert exc.value.status_code == 403
    assert _max_plan_version(dsn, tenant) == 1


def test_plan_edit_uncited_number_400_detail_scrubbed(substrate, monkeypatch) -> None:
    """An edit smuggling an uncited number is rejected by the re-ground (400) AND the violation
    echo is scrub_pii'd BEFORE the HTTPException — the raw token never reaches the response."""
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)
    _assign(dsn, op, tenant)
    _seed_plan(tenant)

    with pytest.raises(HTTPException) as exc:
        _edit(
            op, tenant, patch={"why": "Call customers about 98765432 now."},
            expected_prev_version=1,
        )
    assert exc.value.status_code == 400
    detail = str(exc.value.detail)
    assert "98765432" not in detail  # the uncited digit-run is scrubbed
    assert "[REDACTED:digits]" in detail
    assert "ungrounded" in detail  # still actionable for the VTR
    assert _max_plan_version(dsn, tenant) == 1


# ---------------------------------------------------------------------------
# 4. vtr-batch-cancel — tenant derived server-side
# ---------------------------------------------------------------------------


def _cancel(op: str, batch: UUID, *, jwt_for: str | None = None) -> dict[str, Any]:
    return console.vtr_batch_cancel(
        console.VtrBatchCancelBody(operator_id=op, batch_id=str(batch), reason="bad batch"),
        x_internal_secret=_TEST_INTERNAL_SECRET,
        x_operator_jwt=_op_jwt(jwt_for or op),
    )


def test_batch_cancel_body_has_no_tenant_id() -> None:
    """The VT-293/294 pin: the body CANNOT carry a tenant_id — the tenant is derived from the
    batch row server-side, so a client can never pair a foreign batch with an assigned tenant."""
    assert "tenant_id" not in console.VtrBatchCancelBody.model_fields
    assert "tenant_id" not in console.VtrBatchDraftsBody.model_fields


def test_batch_cancel_foreign_tenant_403_and_deny_audited(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant_a = _new_tenant(dsn)
    tenant_b = _new_tenant(dsn)
    _assign(dsn, op, tenant_a)  # assigned to A only
    batch_b = _seed_batch(dsn, tenant_b)

    with pytest.raises(HTTPException) as exc:
        _cancel(op, batch_b)
    assert exc.value.status_code == 403
    rows = _audit_rows(dsn, "batch_cancel_denied", op)
    assert len(rows) == 1
    assert rows[0]["target_kind"] == "draft_batch"
    assert rows[0]["target_id"] == str(batch_b)
    assert str(rows[0]["tenant_id"]) == str(tenant_b)  # the DERIVED tenant, not a client claim
    assert _batch_row(dsn, tenant_b, batch_b)["status"] == "awaiting_approval"  # untouched


def test_batch_cancel_success_halts_drafts_and_audits(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)
    _assign(dsn, op, tenant)
    customer = _seed_customer(dsn, tenant)
    batch = _seed_batch(dsn, tenant)
    draft = _seed_draft(dsn, tenant, batch, customer)

    out = _cancel(op, batch)
    assert out["ok"] is True
    assert out["tenant_id"] == str(tenant)
    assert out["drafts_halted"] == 1

    assert _batch_row(dsn, tenant, batch)["status"] == "cancelled"
    d = _draft_row(dsn, tenant, draft)
    assert d["status"] == "halted"
    assert d["skip_reason"] == "halted_vtr_cancel"
    rows = _audit_rows(dsn, "draft_batch_cancel", op)
    assert len(rows) == 1
    assert rows[0]["target_id"] == str(batch)
    assert f"agent={AGENT}" in (rows[0]["detail"] or "")
    assert "drafts_halted=1" in (rows[0]["detail"] or "")


def test_batch_cancel_missing_batch_404(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    op = str(uuid4())
    with pytest.raises(HTTPException) as exc:
        _cancel(op, uuid4())
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 5. vtr-batch-drafts — the exception tier
# ---------------------------------------------------------------------------


def test_batch_drafts_non_exception_operator_403(substrate, monkeypatch) -> None:
    """Even an ASSIGNED operator is refused params — the drill-in is exception-tier only."""
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)
    _assign(dsn, op, tenant)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant), params={"1": "AshaSecret"})

    with pytest.raises(HTTPException) as exc:
        console.vtr_batch_drafts(
            console.VtrBatchDraftsBody(operator_id=op, batch_id=str(batch)),
            x_internal_secret=_TEST_INTERNAL_SECRET,
            x_operator_jwt=_op_jwt(op),
        )
    assert exc.value.status_code == 403
    assert _audit_rows(dsn, "draft_params_reveal", op) == []  # no reveal happened, none audited


def test_batch_drafts_fazal_sees_params_and_reveal_is_audited(substrate, monkeypatch) -> None:
    _env(monkeypatch)
    dsn = substrate.dsn
    fazal = str(uuid4())
    monkeypatch.setenv("FAZAL_OWNER_UUID", fazal)
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(
        dsn, tenant, batch, _seed_customer(dsn, tenant),
        params={"1": "AshaSecret", "2": "20% off"},
    )

    out = console.vtr_batch_drafts(
        console.VtrBatchDraftsBody(operator_id=fazal, batch_id=str(batch)),
        x_internal_secret=_TEST_INTERNAL_SECRET,
        x_operator_jwt=_op_jwt(fazal),
    )
    assert len(out["drafts"]) == 1
    assert out["drafts"][0]["template_name"] == _TEMPLATE
    assert out["drafts"][0]["params"] == {"1": "AshaSecret", "2": "20% off"}  # params VISIBLE

    rows = _audit_rows(dsn, "draft_params_reveal", fazal)  # ...and the reveal is on the record
    assert len(rows) == 1
    assert rows[0]["target_kind"] == "draft_batch"
    assert rows[0]["target_id"] == str(batch)


def test_draft_batches_endpoint_returns_aggregates_only(substrate, monkeypatch) -> None:
    """The non-exception read: counts + template enums; params/owner_feedback/customer_id are
    structurally absent from every row (canary-4's app-layer leg)."""
    _env(monkeypatch)
    dsn = substrate.dsn
    op = str(uuid4())
    tenant = _new_tenant(dsn)
    _assign(dsn, op, tenant)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant), params={"1": "AshaSecret"})

    out = console.vtr_draft_batches(
        console.VtrDraftBatchesBody(operator_id=op, tenant_id=str(tenant)),
        x_internal_secret=_TEST_INTERNAL_SECRET,
        x_operator_jwt=_op_jwt(op),
    )
    assert out["count"] == 1
    row = out["rows"][0]
    assert row["draft_count"] == 1
    assert row["pending_count"] == 1
    assert row["template_names"] == [_TEMPLATE]
    assert not ({"params", "owner_feedback", "customer_id", "message_sid"} & set(row))
    assert "AshaSecret" not in json.dumps(row, default=str)


# ---------------------------------------------------------------------------
# 6. The mig-130 view invariants (the acceptance pins)
# ---------------------------------------------------------------------------


def test_vtr_business_plan_latest_only_and_diff_values_stripped(substrate) -> None:
    """On a multi-version tenant the view returns EXACTLY 1 row (the latest) and every
    diff_from_prev is reduced to its KEYS — no prior-version text, no diff VALUES (the
    redaction paradox: an edited-out name must not persist on the VTR surface)."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    sentinel = "OLDSECRET99887766"
    _seed_plan(tenant, [_item("it-1", 1, why=f"Ratings need work. {sentinel}"), _item("it-2", 2)])
    v2_items = [
        _item("it-1", 1, provenance={
            "origin": "vtr_edit", "editor": "vtr:x", "prev_version": 1,
            "diff_from_prev": {"why": [f"Ratings need work. {sentinel}", "Ratings improved."]},
        }),
        _item("it-2", 2),
    ]
    store.write_new_version(
        tenant, summary=_SUMMARY, roadmap=v2_items, fact_bundle=_FACTS, generated_by="vtr:x",
    )

    # VT-377 (mig-134): the view is assignment-scoped — an operator-less read is zero rows
    # (fail-closed), so this pin reads as an ASSIGNED operator via the GUC.
    op = str(uuid4())
    _assign(dsn, op, tenant)
    with vtr_connection(operator_id=op) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM vtr_business_plan WHERE tenant_id = %s", (str(tenant),))
        rows = cur.fetchall()
    assert len(rows) == 1  # latest only — v1 is unreachable through the view
    row = dict(rows[0])
    assert row["version"] == 2
    assert "fact_bundle_json" not in row  # fact values may carry customer names — excluded
    edited = next(i for i in row["roadmap_json"] if i["item_id"] == "it-1")
    diff = edited["provenance"]["diff_from_prev"]
    assert diff == ["why"]  # keys survive (WHAT changed), values are stripped (never to-what)
    assert sentinel not in json.dumps(row, default=str)  # neither via v1 nor via the diff


def test_vtr_views_exclude_sensitive_columns(substrate) -> None:
    """Column-level exclusions are DB-real: no params/owner_feedback/customer_id on
    vtr_draft_batches; no revoke_reason on vtr_agent_autonomy."""
    with vtr_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM vtr_draft_batches LIMIT 0")
        batch_cols = {d.name for d in cur.description}
        cur.execute("SELECT * FROM vtr_agent_autonomy LIMIT 0")
        autonomy_cols = {d.name for d in cur.description}
    assert not ({"params", "owner_feedback", "customer_id", "message_sid"} & batch_cols)
    assert "revoke_reason" not in autonomy_cols


def test_vtr_role_denied_on_raw_tables_and_admin_view(substrate) -> None:
    """The GUARANTEE: app_vtr_role has ZERO grants on the raw tables (agent_drafts.params is a
    physical permission-denied, not an app-side mask) and NO grant on the exception-tier view."""
    denied = (
        "SELECT params FROM agent_drafts LIMIT 1",
        "SELECT * FROM business_plan LIMIT 1",
        "SELECT * FROM agent_draft_batches LIMIT 1",
        "SELECT * FROM tenant_agent_autonomy LIMIT 1",
        "SELECT * FROM vtr_admin_batch_drafts LIMIT 1",  # exception tier is NOT this role's door
    )
    with vtr_connection() as conn, conn.cursor() as cur:
        for sql in denied:
            with pytest.raises(pg_errors.InsufficientPrivilege):
                cur.execute(sql)
            cur.execute("ROLLBACK")
        # ...while the four granted views ARE readable (the only door).
        for view in ("vtr_business_plan", "vtr_plan_history", "vtr_agent_autonomy",
                     "vtr_draft_batches"):
            cur.execute(f"SELECT count(*) FROM {view}")  # noqa: S608 — fixed allowlist


# ---------------------------------------------------------------------------
# 7. Seam units
# ---------------------------------------------------------------------------


def test_cancel_batch_halts_drafted_and_is_idempotent(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    customer = _seed_customer(dsn, tenant)
    batch = _seed_batch(dsn, tenant)
    draft = _seed_draft(dsn, tenant, batch, customer)

    with tenant_connection(tenant) as conn:
        assert cancel_batch(tenant, batch, reason="bad", vtr_id="v1", conn=conn) == 1
    assert _batch_row(dsn, tenant, batch)["status"] == "cancelled"
    d = _draft_row(dsn, tenant, draft)
    assert d["status"] == "halted"
    assert d["skip_reason"] == "halted_vtr_cancel"

    # Second cancel: the batch is now terminal — 0, no error, nothing re-touched.
    with tenant_connection(tenant) as conn:
        assert cancel_batch(tenant, batch, reason="again", vtr_id="v1", conn=conn) == 0


def test_cancel_batch_terminal_batch_is_zero_noop(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    sent_batch = _seed_batch(dsn, tenant, status="sent")
    with tenant_connection(tenant) as conn:
        assert cancel_batch(tenant, sent_batch, reason="x", vtr_id="v1", conn=conn) == 0
    assert _batch_row(dsn, tenant, sent_batch)["status"] == "sent"  # history untouched


def test_vtr_override_unfreeze_cancels_nothing(substrate) -> None:
    """Unfreeze is recovery, not a kill switch: frozen → false and open batches survive
    (work re-enters via the next coordinator sweep)."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_autonomy_row(dsn, tenant, frozen=True)
    batch = _seed_batch(dsn, tenant)

    with tenant_connection(tenant) as conn:
        st = vtr_autonomy_override(
            tenant, AGENT, "unfreeze", reason="resolved", vtr_id="v1", conn=conn
        )
    assert st.frozen is False
    assert _batch_row(dsn, tenant, batch)["status"] == "awaiting_approval"  # nothing cancelled


def test_edit_roadmap_item_stale_version_raises(substrate) -> None:
    tenant = _new_tenant(substrate.dsn)
    _seed_plan(tenant)
    with pytest.raises(StaleVersion):
        seams.edit_roadmap_item(
            str(tenant), "it-1", {"month": 2}, vtr_id="v1", expected_prev_version=99
        )
    assert _max_plan_version(substrate.dsn, tenant) == 1


def test_jwt_without_exp_is_rejected(monkeypatch):
    """Gate hardening: an exp-LESS operator JWT must be rejected (pyjwt verifies exp only if
    present by default — require it, so a future exp-less mint can't be honored forever)."""
    import jwt as pyjwt

    from orchestrator.api import ops_common

    monkeypatch.setenv("OPERATOR_JWT_SECRET", _TEST_JWT_KEY)
    token = pyjwt.encode(
        {"operator_claim": True, "operator_id": "op-1", "aud": "authenticated"},  # NO exp
        _TEST_JWT_KEY, algorithm="HS256",
    )
    with pytest.raises(Exception) as exc:
        ops_common.verify_operator_jwt(token)
    assert getattr(exc.value, "status_code", None) == 403
