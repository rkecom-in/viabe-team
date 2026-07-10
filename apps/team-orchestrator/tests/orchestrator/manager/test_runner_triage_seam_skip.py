"""VT-606 round-3 test-adequacy item (b) — a RUNNER-level test proving the bug caught before
commit stays fixed: when the triage seam's ``skip_legacy_dispatch=True`` (enforce mode routing a
turn itself), ``runner.webhook_pipeline_run`` must still run its OWN close-out —
``close_webhook_run`` (the run does not dangle 'running' forever) and the VT-88
``maybe_escalate_support`` fallback — exactly as it would for any other clean turn. An earlier
draft used an early ``return`` here that skipped both; this test drives the REAL
``webhook_pipeline_run`` (via DBOS, live Postgres) to lock the fix in, not just a code read.

Mirrors ``tests/orchestrator/test_run_control_realdb.py``'s own harness (tenant seed +
``DBOS.start_workflow(webhook_pipeline_run, ...)``). ``dispatch_brain`` is spied (never actually
called — proving skip_legacy_dispatch correctly skipped it) rather than exercised for real; the
triage seam itself is monkeypatched to a canned ``skip_legacy_dispatch=True`` result so this test
isolates runner.py's OWN control flow, not triage_seam's/triage.py's own classification (covered
separately in test_triage_seam*.py).
"""

from __future__ import annotations

import os
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

pytest.importorskip("dbos")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — runner triage-seam-skip test skipped",
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
                (f"VT606-runner-{uuid4().hex[:8]}",),
            ).fetchone()[0]
        )


def test_skip_legacy_dispatch_still_closes_the_run_and_fires_the_fallback(
    substrate, monkeypatch: pytest.MonkeyPatch
):
    import dbos as _dbos
    import psycopg

    import orchestrator.agent.dispatch as dispatch_mod
    from orchestrator.manager.triage_seam import TriageSeamResult
    from orchestrator.runner import webhook_pipeline_run

    tenant = _seed_tenant_brain_ready(substrate)

    dispatch_brain_calls = []
    monkeypatch.setattr(
        dispatch_mod,
        "dispatch_brain",
        lambda **k: dispatch_brain_calls.append(1) or dispatch_mod.DispatchResult(
            final_status="completed", terminal_path=None
        ),
    )

    escalate_calls = []
    import orchestrator.owner_surface.support_bot as support_bot_mod

    monkeypatch.setattr(
        support_bot_mod, "maybe_escalate_support", lambda **k: escalate_calls.append(k)
    )

    import orchestrator.manager.triage_seam as triage_seam_mod

    monkeypatch.setattr(
        triage_seam_mod,
        "triage_seam",
        lambda *a, **k: TriageSeamResult(outcome="new_task", task_id=uuid4(), skip_legacy_dispatch=True),
    )

    message_sid = f"SM{uuid4().hex}"
    run_id = str(uuid5(NAMESPACE_URL, message_sid))
    fields = {
        "MessageSid": message_sid,
        "From": "+15551110099",
        "To": "+15552220099",
        "Body": "please win back my lapsed customers",
        "NumMedia": "0",
    }

    with _dbos.SetWorkflowID(f"vt606-runner-skip-{message_sid}"):
        handle = _dbos.DBOS.start_workflow(webhook_pipeline_run, tenant, run_id, fields)
    result = handle.get_result()

    # dispatch_brain must NEVER be called — the triage seam owned this turn's routing.
    assert dispatch_brain_calls == []

    # close_webhook_run still ran — the pipeline_runs row is 'completed', not dangling 'running'.
    with psycopg.connect(substrate, autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM pipeline_runs WHERE id = %s", (run_id,)
        ).fetchone()[0]
    assert status == "completed", "the run was left dangling — close_webhook_run did not fire"

    # the VT-88 fallback still fires for this turn (final_status defaults to 'completed', matching
    # the reject-path convention) — never silently dropped just because dispatch_brain was skipped.
    assert len(escalate_calls) == 1

    assert result["run_id"] == run_id
    assert result["tenant_id"] == tenant


def _drive_skip_turn(substrate, monkeypatch, tenant, *, body):
    """Shared harness: drive the REAL webhook_pipeline_run with a canned skip_legacy_dispatch=True
    triage result and dispatch_brain/escalate spied out, so the D1 in-turn-wait (VT-623 Head3) is the
    only behaviour under test. Returns the message_sid."""
    import dbos as _dbos

    import orchestrator.agent.dispatch as dispatch_mod
    import orchestrator.manager.triage_seam as triage_seam_mod
    import orchestrator.owner_surface.support_bot as support_bot_mod
    from orchestrator.manager.triage_seam import TriageSeamResult
    from orchestrator.runner import webhook_pipeline_run

    monkeypatch.setattr(
        dispatch_mod,
        "dispatch_brain",
        lambda **k: dispatch_mod.DispatchResult(final_status="completed", terminal_path=None),
    )
    monkeypatch.setattr(support_bot_mod, "maybe_escalate_support", lambda **k: None)
    monkeypatch.setattr(
        triage_seam_mod,
        "triage_seam",
        lambda *a, **k: TriageSeamResult(
            outcome="new_task", task_id=uuid4(), skip_legacy_dispatch=True
        ),
    )

    message_sid = f"SM{uuid4().hex}"
    run_id = str(uuid5(NAMESPACE_URL, message_sid))
    fields = {
        "MessageSid": message_sid,
        "From": "+15551110099",
        "To": "+15552220099",
        "Body": body,
        "NumMedia": "0",
    }
    with _dbos.SetWorkflowID(f"vt623-d1wait-{message_sid}"):
        handle = _dbos.DBOS.start_workflow(webhook_pipeline_run, tenant, run_id, fields)
    handle.get_result()
    return message_sid


def test_d1_wait_suppresses_fallback_when_async_reply_lands_late(
    substrate, monkeypatch: pytest.MonkeyPatch
):
    """VT-623 Head3 B1 — the bounded in-turn wait must POLL: when the async manager_task emits its owner
    reply a poll-tick AFTER the wait starts, the loop picks it up and the D1 fallback ('I'm on it') is
    SUPPRESSED. Reply-detector returns False on the first probe then True (models a late-but-in-window
    reply)."""
    import orchestrator.runner as runner_mod

    tenant = _seed_tenant_brain_ready(substrate)

    probe_calls = {"n": 0}

    def _late_reply(_tenant_id, _sid):
        probe_calls["n"] += 1
        return probe_calls["n"] >= 2  # False on the 1st probe, True thereafter

    fallback_calls = []
    monkeypatch.setattr(runner_mod, "_brain_emitted_owner_reply", _late_reply)
    monkeypatch.setattr(
        runner_mod, "_send_completed_no_reply_fallback", lambda *a: fallback_calls.append(a)
    )
    monkeypatch.setattr(runner_mod, "_D1_INTURN_WAIT_MAX_POLLS", 5)
    monkeypatch.setattr(runner_mod, "_D1_INTURN_WAIT_POLL_S", 0.01)

    _drive_skip_turn(substrate, monkeypatch, tenant, body="win back my lapsed customers")

    # The wait looped at least once (1st probe False) THEN broke on the late reply — the fallback that
    # sends the redundant "I'm on it" was never invoked.
    assert fallback_calls == [], "D1 fallback fired despite the async reply arriving in-window"
    assert probe_calls["n"] >= 2, "the wait did not re-poll after the first (False) probe"


def test_d1_wait_times_out_then_fires_fallback_once_when_no_reply(
    substrate, monkeypatch: pytest.MonkeyPatch
):
    """VT-623 Head3 B1 — when the async task never emits within the bounded window, the wait exhausts its
    polls (strictly bounded — never hangs the turn) and the D1 fallback fires EXACTLY ONCE. No double-send:
    the fallback is the single owner-facing message on a silent turn."""
    import orchestrator.runner as runner_mod

    tenant = _seed_tenant_brain_ready(substrate)

    probe_calls = {"n": 0}

    def _never_replies(_tenant_id, _sid):
        probe_calls["n"] += 1
        return False

    fallback_calls = []
    monkeypatch.setattr(runner_mod, "_brain_emitted_owner_reply", _never_replies)
    monkeypatch.setattr(
        runner_mod, "_send_completed_no_reply_fallback", lambda *a: fallback_calls.append(a)
    )
    monkeypatch.setattr(runner_mod, "_D1_INTURN_WAIT_MAX_POLLS", 3)
    monkeypatch.setattr(runner_mod, "_D1_INTURN_WAIT_POLL_S", 0.01)

    _drive_skip_turn(substrate, monkeypatch, tenant, body="win back my lapsed customers")

    # Bounded: exactly one fallback send, and the wait probed the full budget (3 in-loop) plus the D1
    # check (1) before giving up — proof the loop terminated on the poll cap, not early.
    assert len(fallback_calls) == 1, "silent turn must fire the honest fallback exactly once"
    assert probe_calls["n"] >= runner_mod._D1_INTURN_WAIT_MAX_POLLS + 1
