"""VT-379 — pipeline_steps PII redaction at write (real-Postgres tests).

The VT-374 STEP-0 audit found a double blind spot: ``pipeline_steps.error``
was never redacted at write, and three direct-INSERT writers
(``error_router._log_decision``, ``sales_recovery._emit_self_evaluate_gate``,
``collapse.record_terminal_verdict``) bypassed redaction entirely. Plus
``name_registry=None`` everywhere — write-time redaction never consulted the
customer-name registry.

Covered here:
  1. ``write_step`` redacts the ``error`` param AND both envelopes with a
     POPULATED tenant name registry (the VT-361 inert-registry tripwire
     pattern: assert the registry populates BEFORE asserting redaction —
     phone redaction is pattern-driven and proves nothing about the
     registry; only the 2-token display name does).
  2. ``write_step`` fail-soft: a registry build failure degrades to
     pattern-only redaction with a structured warning — the write still
     lands (a registry outage must not kill a live pipeline; contrast the
     fail-CLOSED VT-374 ops-API posture).
  3-5. Each of the three former direct writers now lands a redacted row via
     ``write_redacted_step_row`` with its exact row semantics preserved
     (step_kind / step_seq linkage / column set / rationale).

All identities are uuid-suffixed (shared local DB; no fixed names).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("pydantic")

import psycopg  # noqa: E402 — after the dependency skip guard

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — pipeline_observability redaction tests skipped",
)


@pytest.fixture(scope="module")
def rls_ctx():
    """Apply migrations + launch DBOS so the tenant_connection pool exists."""
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


# --- seeding helpers (superuser, RLS bypassed; uuid-suffixed identities) ----


def _new_tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES (%s, 'founding', 'paid_at_risk') RETURNING id",
            (f"vt379-obs-{uuid4().hex[:8]}",),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _new_run(dsn: str, tenant_id: str) -> str:
    run_id = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running')",
            (run_id, tenant_id),
        )
    return run_id


def _seed_customer(dsn: str, tenant_id: str, display_name: str) -> None:
    """The REAL customers path the name registry reads
    (CustomersWrapper.list_display_names)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customers (tenant_id, display_name) VALUES (%s, %s)",
            (tenant_id, display_name),
        )


def _populated_registry(dsn: str, tenant_id: str) -> str:
    """Seed a 2-token customer name and PROVE the registry populates from it
    (the VT-361 inert-registry tripwire — binding pre-assertion for every
    name-redaction test below). Returns the display name."""
    from orchestrator.privacy import customer_registry

    display_name = f"Rajesh {uuid4().hex[:8].capitalize()}"
    _seed_customer(dsn, tenant_id, display_name)
    customer_registry.invalidate_all()  # force a true read, never a stale-empty cache
    registry = customer_registry.make_name_registry(tenant_id)
    assert registry(display_name.casefold()) is True, (
        "make_name_registry returned an INERT/empty registry — the seeded "
        "customer was not read; name redaction would silently no-op (VT-361)"
    )
    return display_name


def _raw_steps(dsn: str, run_id: str) -> list[dict]:
    """Read raw rows as superuser (RLS bypassed) — write-time proof, not a
    read-path projection."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        return conn.execute(
            "SELECT step_seq, step_kind, step_name, input_envelope, "
            "       output_envelope, error, decision_rationale, status "
            "FROM pipeline_steps WHERE run_id = %s ORDER BY step_seq",
            (run_id,),
        ).fetchall()


_PHONE = "9876543210"


# --- 1. write_step: error column + envelopes, registry-backed ---------------


def test_write_step_redacts_error_and_envelopes_with_populated_registry(rls_ctx):
    """The error param (the never-redacted column) and BOTH envelopes flow
    through the redactor WITH the tenant name registry (closes the
    name_registry=None gap for write_step)."""
    from orchestrator.observability.pipeline_observability import write_step

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)
    name = _populated_registry(rls_ctx.dsn, tenant)

    write_step(
        step_kind="error",
        run_id=UUID(run_id),
        tenant_id=UUID(tenant),
        input_envelope={
            "failure_type": "llm_api_error",
            "message": f"draft for {name} at {_PHONE} failed",
        },
        output_envelope={"strategy": "retry_with_backoff"},
        error={
            "message": f"Anthropic call failed while drafting for {name}",
            "dropped_values": {"note": f"call {name} on {_PHONE}"},
        },
        status="completed",
    )

    rows = _raw_steps(rls_ctx.dsn, run_id)
    assert len(rows) == 1
    row = rows[0]
    surface = json.dumps(
        {
            "input": row["input_envelope"],
            "output": row["output_envelope"],
            "error": row["error"],
        },
        default=str,
    )
    assert _PHONE not in surface, "raw phone survived the write-time redactor"
    assert name not in surface, "registry-known customer name survived at write"
    assert "<customer_name>" in row["error"]["message"], (
        f"name not tokenised in error column: {row['error']['message']!r}"
    )
    assert "<customer_name>" in row["input_envelope"]["message"]
    assert "phone_tok_" in row["error"]["dropped_values"]["note"]
    # Clean payloads validate against the registered ErrorEnvelope — no
    # soft-fail flags injected into the error column.
    assert "payload_validation_failed" not in row["error"]
    assert row["output_envelope"] == {"strategy": "retry_with_backoff"}


def test_write_step_returns_the_inserted_rows_id(rls_ctx):
    """§7D — write_step's return value is the EXACT ``pipeline_steps.id`` it just inserted, so a
    caller (langchain_callback.py) can thread a precise ``reasoning_ref.step_id`` instead of the
    coarser (run_id, step_name) join."""
    from orchestrator.observability.pipeline_observability import write_step

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)

    returned_id = write_step(
        step_kind="error",
        run_id=UUID(run_id),
        tenant_id=UUID(tenant),
        input_envelope={"failure_type": "llm_api_error", "message": "x"},
        output_envelope={"strategy": "retry_with_backoff"},
        status="completed",
    )

    assert returned_id is not None
    with psycopg.connect(rls_ctx.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT id FROM pipeline_steps WHERE run_id = %s", (run_id,)
        ).fetchone()
    assert row is not None
    assert returned_id == row[0]


def test_write_step_registry_failure_fails_soft_pattern_only(rls_ctx, monkeypatch, caplog):
    """Fail-soft posture (documented in _registry_for_tenant): registry build
    failure → the write still lands, pattern redaction still runs, and a
    structured warning is logged. Contrast: the VT-374 ops API fail-closes."""
    from orchestrator.privacy import customer_registry
    from orchestrator.observability.pipeline_observability import write_step

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)

    def _boom(_tenant_id: str):
        raise RuntimeError("vt379 synthetic registry outage")

    monkeypatch.setattr(customer_registry, "make_name_registry", _boom)

    with caplog.at_level(
        logging.WARNING, logger="orchestrator.observability.pipeline_observability"
    ):
        write_step(
            step_kind="error",
            run_id=UUID(run_id),
            tenant_id=UUID(tenant),
            input_envelope={"failure_type": "llm_api_error", "message": "x"},
            output_envelope={"strategy": "retry_with_backoff"},
            error={"message": f"vendor said call {_PHONE} back"},
            status="completed",
        )

    rows = _raw_steps(rls_ctx.dsn, run_id)
    assert len(rows) == 1, "registry outage must NOT kill the pipeline write"
    assert _PHONE not in json.dumps(rows[0]["error"]), (
        "pattern redaction must still run when the registry is unavailable"
    )
    assert any(
        "name-registry build failed" in rec.message for rec in caplog.records
    ), "fail-soft must log a structured warning, not degrade silently"


# --- 2. error_router._log_decision via the shared redacting writer ----------


def test_error_router_decision_row_is_redacted(rls_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.error_router import route_failure
    from orchestrator.failures import FailureRecord, FailureType
    from orchestrator.strategies import Strategy

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)
    name = _populated_registry(rls_ctx.dsn, tenant)

    record = FailureRecord(
        failure_type=FailureType.TOOL_CALL_TIMEOUT,
        message=f"timeout while messaging {name} at {_PHONE}",
        occurred_at=datetime.now(UTC),
        tenant_id=UUID(tenant),
        run_id=UUID(run_id),
        vendor="anthropic",
        metadata={"dropped_values": {"reply_draft": f"Hi {name}, ring {_PHONE}"}},
    )
    assert route_failure(record) == Strategy.RETRY_WITH_BACKOFF

    rows = _raw_steps(rls_ctx.dsn, run_id)
    assert len(rows) == 1
    row = rows[0]
    # Exact row semantics preserved (VT-379 contract).
    assert row["step_kind"] == "error"
    assert row["step_seq"] == 1
    assert row["status"] == "completed"
    assert row["output_envelope"] == {"strategy": "retry_with_backoff"}
    assert row["decision_rationale"] == "tool_call_timeout -> retry_with_backoff"
    assert row["error"]["failure_type"] == "tool_call_timeout"
    assert row["error"]["vendor"] == "anthropic"
    # Free text redacted: failure.message + metadata.dropped_values (the
    # verbatim-model-output leak the VT-374 audit flagged).
    surface = json.dumps(row["error"], default=str)
    assert _PHONE not in surface and name not in surface
    assert "<customer_name>" in row["error"]["message"]
    assert "<customer_name>" in row["error"]["metadata"]["dropped_values"]["reply_draft"]
    # RLS read path still sees the row (helper wrote via tenant_connection).
    with tenant_connection(tenant) as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM pipeline_steps WHERE run_id = %s",
            (run_id,),
        ).fetchone()["n"]
    assert n == 1


# --- 3. sales_recovery._emit_self_evaluate_gate ------------------------------


def test_self_evaluate_gate_row_is_redacted(rls_ctx):
    from orchestrator.agent.sales_recovery import _emit_self_evaluate_gate
    from orchestrator.context_builder import SalesRecoveryContext

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)
    name = _populated_registry(rls_ctx.dsn, tenant)

    context = SalesRecoveryContext(
        tenant_id=UUID(tenant),
        run_id=UUID(run_id),
        user_request="recover dormant customers",
    )
    _emit_self_evaluate_gate(
        context=context,
        attempt_number=1,
        outcome=SimpleNamespace(value="fail"),
        rejection_feedback=SimpleNamespace(
            schema=[],
            pillar=[f"plan names {name} at {_PHONE} directly"],
            consistency=[],
            legal=[],
        ),
        feedback_messages=[],
    )

    rows = _raw_steps(rls_ctx.dsn, run_id)
    assert len(rows) == 1
    row = rows[0]
    # Exact row semantics: output_envelope ONLY (no error, no rationale).
    assert row["step_kind"] == "self_evaluate_gate"
    assert row["step_seq"] == 1
    assert row["status"] == "completed"
    assert row["error"] is None
    assert row["decision_rationale"] is None
    env = row["output_envelope"]
    assert env["attempt_number"] == 1
    assert env["outcome"] == "fail"
    surface = json.dumps(env, default=str)
    assert _PHONE not in surface and name not in surface
    assert "<customer_name>" in env["reasons"]["pillar"][0]


# --- 4. collapse.record_terminal_verdict -------------------------------------


def test_collapse_terminal_verdict_row_is_redacted(rls_ctx):
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanOutOfScope,
        SuggestedSpecialist,
    )
    from orchestrator.collapse import record_terminal_verdict

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)
    name = _populated_registry(rls_ctx.dsn, tenant)

    plan = CampaignPlanOutOfScope(
        tenant_id=UUID(tenant),
        run_id=UUID(run_id),
        generated_at=datetime.now(UTC),
        out_of_scope_reason=(
            f"Owner asked us to ring {name} on {_PHONE} about a review; "
            "reputation specialist owns that domain, not sales recovery."
        ),
        suggested_specialist=SuggestedSpecialist.REPUTATION,
    )
    record_terminal_verdict(UUID(tenant), UUID(run_id), plan)

    rows = _raw_steps(rls_ctx.dsn, run_id)
    assert len(rows) == 1
    row = rows[0]
    # Exact row semantics: output_envelope + decision_rationale, no error.
    assert row["step_kind"] == "campaign_plan_emitted"
    assert row["step_seq"] == 1
    assert row["status"] == "completed"
    assert row["error"] is None
    assert row["decision_rationale"] == "agent terminal verdict: out_of_scope"
    env = row["output_envelope"]
    assert env["variant"] == "out_of_scope"
    assert env["version"] == "1.0"
    assert env["suggested_specialist"] == "reputation"
    surface = json.dumps(env, default=str)
    assert _PHONE not in surface and name not in surface
    assert "<customer_name>" in env["out_of_scope_reason"]
