"""T14 (supersedes the VT-609 Package 4 enforce-bypass acceptance) — the onboarding-journey gate
(VT-367) runs in EVERY mode, with a VT-608-style DEFER when the loop genuinely owns the tenant's
onboarding (an active manager_task whose CURRENT step targets onboarding_conductor).

WHY the reversal: VT-609 disabled the gate in enforce on the assumption the Manager brain reliably
spawns onboarding_conductor. The §2 judge measured that false (onboarding_privacy_skeptic: 0/4
conductor spawns; the kickoff, the volunteered profile fields, and the setup-status ask all
completed SILENT → D1 "I'm on it" → ignored_speech_act + loop_stall; the fields were never
recorded). The gate is the deterministic floor; the conductor still owns the conversation whenever
it actually got spawned (the defer).

Drives the REAL ``webhook_pipeline_run`` (DBOS, live Postgres), mirroring
``test_runner_integration_gate_defer.py``'s own harness exactly. ``dispatch_brain`` is stubbed to a
clean completion (this test isolates the gate's own routing decision, not brain dispatch); only
``maybe_handle_journey_reply`` itself is spied.
"""

from __future__ import annotations

import os
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

pytest.importorskip("dbos")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — runner onboarding-mode-gate test skipped",
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


def _seed_tenant_with_active_journey(dsn: str) -> str:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_id = str(
            conn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase, owner_inputs) "
                "VALUES (%s, 'founding', 'trial', true) RETURNING id",
                (f"VT609-gate-{uuid4().hex[:8]}",),
            ).fetchone()[0]
        )
        conn.execute(
            "INSERT INTO onboarding_journey (tenant_id, status, question_queue, cursor, "
            "answers, skipped) VALUES (%s, 'active', '[]'::jsonb, 0, '{}'::jsonb, '[]'::jsonb)",
            (tenant_id,),
        )
    return tenant_id


def _drive_webhook(tenant: str, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, list]:
    import dbos as _dbos

    import orchestrator.agent.dispatch as dispatch_mod
    from orchestrator.runner import webhook_pipeline_run

    monkeypatch.setattr(
        dispatch_mod,
        "dispatch_brain",
        lambda **k: dispatch_mod.DispatchResult(final_status="completed", terminal_path=None),
    )

    journey_calls: list = []
    import orchestrator.onboarding.journey as journey_mod

    real_maybe = journey_mod.maybe_handle_journey_reply

    def _spy_maybe(*a, **k):
        journey_calls.append(1)
        return real_maybe(*a, **k)

    monkeypatch.setattr(journey_mod, "maybe_handle_journey_reply", _spy_maybe)

    message_sid = f"SM{uuid4().hex}"
    run_id = str(uuid5(NAMESPACE_URL, message_sid))
    fields = {
        "MessageSid": message_sid,
        "From": "+15551110099",
        "To": "+15552220099",
        "Body": "hello there",
        "NumMedia": "0",
    }
    with _dbos.SetWorkflowID(f"vt609-gate-{message_sid}"):
        handle = _dbos.DBOS.start_workflow(webhook_pipeline_run, tenant, run_id, fields)
    result = handle.get_result()
    return result, journey_calls


def test_legacy_mode_calls_journey_gate_unchanged(substrate, monkeypatch: pytest.MonkeyPatch):
    """Pins legacy behavior byte-identical — the gate must still be called exactly once, same as
    before this row (default mode; no TEAM_MANAGER_LOOP_MODE set)."""
    monkeypatch.delenv("TEAM_MANAGER_LOOP_MODE", raising=False)
    tenant = _seed_tenant_with_active_journey(substrate)

    result, calls = _drive_webhook(tenant, monkeypatch)

    assert len(calls) == 1, "legacy mode must call the journey gate unchanged"
    assert result["tenant_id"] == tenant


def test_shadow_mode_calls_journey_gate_unchanged(substrate, monkeypatch: pytest.MonkeyPatch):
    """Amendment A1 — shadow must stay legacy-shaped for the owner-facing path (observational-only
    triage runs SEPARATELY); the journey gate itself is unaffected by shadow mode."""
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "shadow")
    tenant = _seed_tenant_with_active_journey(substrate)

    result, calls = _drive_webhook(tenant, monkeypatch)

    assert len(calls) == 1, "shadow mode must call the journey gate unchanged"
    assert result["tenant_id"] == tenant


def test_enforce_mode_calls_journey_gate_when_no_conductor_task(
    substrate, monkeypatch: pytest.MonkeyPatch
):
    """T14 acceptance: enforce mode runs the deterministic journey gate whenever the loop does NOT
    own the tenant's onboarding — the common case, because the brain's conductor spawn is
    intermittent (measured 0/4 on privacy_skeptic). Without this the turn completes silent → D1."""
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "enforce")
    tenant = _seed_tenant_with_active_journey(substrate)

    result, calls = _drive_webhook(tenant, monkeypatch)

    assert len(calls) == 1, "enforce mode must run the journey gate when no conductor task is live"
    assert result["tenant_id"] == tenant


def test_enforce_mode_defers_to_live_conductor_task(substrate, monkeypatch: pytest.MonkeyPatch):
    """T14 defer: an active manager_task whose CURRENT step targets onboarding_conductor owns the
    turn — the gate must skip (no dual-writer race on the journey state)."""
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "enforce")
    tenant = _seed_tenant_with_active_journey(substrate)

    from orchestrator.manager import plan_store
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep

    plan = ManagerPlan(
        objective="finish onboarding",
        steps=[PlanStep(step_seq=1, kind="specialist_dispatch", specialist="onboarding_conductor")],
    )
    task_id = plan_store.create_plan(tenant, plan, source_message_sid=f"SM{uuid4().hex}")
    plan_store.claim_next_step(tenant, task_id)  # sets current_step_id + status='running'

    result, calls = _drive_webhook(tenant, monkeypatch)

    assert calls == [], "a live conductor-owned task must make the journey gate defer"
    assert result["tenant_id"] == tenant
