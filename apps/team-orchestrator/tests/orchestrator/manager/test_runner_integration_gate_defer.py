"""VT-608 ruling 1 — the runner-gate DEFER check: when an active loop task's CURRENT step targets
integration_agent, the deterministic legacy gate (``maybe_resume_shopify_onboarding``) must defer
(never be called) so the loop and the legacy gate never race the same tenant_integration_state
writes. With NO such active loop step (the common case, and the only case in legacy/shadow mode),
the gate call site must remain BYTE-IDENTICAL to before this ruling — always invoked.

Drives the REAL ``webhook_pipeline_run`` (DBOS, live Postgres), mirroring
``test_runner_triage_seam_skip.py``'s own harness exactly. ``dispatch_brain`` is stubbed to a clean
completion (this test isolates the gate's own defer decision, not brain dispatch); the triage seam
and support-bot escalation are left REAL (a fresh tenant/task, nothing pending, so they no-op
cleanly) — only ``maybe_resume_shopify_onboarding`` itself is spied.
"""

from __future__ import annotations

import os
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

pytest.importorskip("dbos")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — runner integration-gate-defer test skipped",
)


@pytest.fixture(scope="module")
def substrate():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "test-salt")

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


def _seed_tenant_brain_ready(dsn: str) -> str:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase, owner_inputs) "
                "VALUES (%s, 'founding', 'paid_active', true) RETURNING id",
                (f"VT608-gate-{uuid4().hex[:8]}",),
            ).fetchone()[0]
        )


def _seed_active_integration_task(tenant_id: str) -> None:
    from orchestrator.manager import plan_store
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    plan = ManagerPlan(
        objective="connect a data source",
        steps=[PlanStep(step_seq=1, kind="specialist_dispatch", specialist="integration_agent")],
    )
    task_id = plan_store.create_plan(tenant_id, plan, source_message_sid=f"SM{uuid4().hex}")
    plan_store.claim_next_step(tenant_id, task_id)  # sets current_step_id + status='running'


def _drive_webhook(dsn: str, tenant: str, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, list]:
    import dbos as _dbos

    import orchestrator.agent.dispatch as dispatch_mod
    from orchestrator.runner import webhook_pipeline_run

    monkeypatch.setattr(
        dispatch_mod,
        "dispatch_brain",
        lambda **k: dispatch_mod.DispatchResult(final_status="completed", terminal_path=None),
    )

    resume_calls: list = []
    import orchestrator.onboarding.shopify_onboarding as shopify_onboarding_mod

    real_resume = shopify_onboarding_mod.maybe_resume_shopify_onboarding

    def _spy_resume(*a, **k):
        resume_calls.append(1)
        return real_resume(*a, **k)

    monkeypatch.setattr(shopify_onboarding_mod, "maybe_resume_shopify_onboarding", _spy_resume)

    message_sid = f"SM{uuid4().hex}"
    run_id = str(uuid5(NAMESPACE_URL, message_sid))
    fields = {
        "MessageSid": message_sid,
        "From": "+15551110088",
        "To": "+15552220088",
        "Body": "hello there",
        "NumMedia": "0",
    }
    with _dbos.SetWorkflowID(f"vt608-gate-{message_sid}"):
        handle = _dbos.DBOS.start_workflow(webhook_pipeline_run, tenant, run_id, fields)
    result = handle.get_result()
    return result, resume_calls


def test_active_integration_loop_step_defers_the_legacy_gate(
    substrate, monkeypatch: pytest.MonkeyPatch
):
    tenant = _seed_tenant_brain_ready(substrate)
    _seed_active_integration_task(tenant)

    result, resume_calls = _drive_webhook(substrate, tenant, monkeypatch)

    assert resume_calls == [], (
        "the legacy gate must NEVER be called while the loop owns an active integration_agent step"
    )
    assert result["tenant_id"] == tenant


def test_no_active_integration_step_calls_the_legacy_gate_unchanged(
    substrate, monkeypatch: pytest.MonkeyPatch
):
    """Pins legacy behavior byte-identical when no loop task exists (the common case today)."""
    tenant = _seed_tenant_brain_ready(substrate)
    # deliberately NO integration task seeded for this tenant.

    result, resume_calls = _drive_webhook(substrate, tenant, monkeypatch)

    assert len(resume_calls) == 1, (
        "the legacy gate must still be called exactly once when the loop does not own an "
        "integration step — the defer check must not change behavior in the common case"
    )
    assert result["tenant_id"] == tenant
