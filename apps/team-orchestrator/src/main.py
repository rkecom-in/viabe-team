"""FastAPI entrypoint for the Viabe Team orchestrator (VT-3.3a).

Run locally:  uvicorn main:app --app-dir src

The lifespan launches DBOS on startup — never on import (DBOS connects to
Postgres and recovers interrupted workflows on launch).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from orchestrator.api import router


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    from dbos_config import launch_dbos, shutdown_dbos
    from orchestrator.auth.prod_safety import assert_prod_safe_flags
    from orchestrator.dbos_purge import register_purge_scheduler
    from orchestrator.observability.envelopes import (
        validate_registry_completeness,
    )
    from orchestrator.scheduled_triggers import register_scheduled_triggers

    # VT-434 fail-closed boot guard (FIRST — before any effect): refuse to boot
    # under EXPECTED_ENV=prod if a dangerous test/mock auth-bypass / send-mock
    # flag is on (TEAM_TWILIO_VERIFY_MOCK_MODE → static-OTP login bypass;
    # TEAM_TWILIO_MOCK_MODE → real sends silently dropped). Prod detection
    # mirrors VT-362's EXPECTED_ENV signal; dev/CI is a no-op.
    assert_prod_safe_flags()

    # VT-179 boot hook (CL-419 / VT-179 fix-1): validate the typed-envelope
    # registry covers every step_kind=<literal> in source. Fail-fast at
    # FastAPI startup — second guard alongside dbos_config.launch_dbos's
    # call, since both web-process and worker-process boot paths must
    # enforce registry-source consistency.
    validate_registry_completeness()

    # VT-181 boot hook: every @tool_step decorator's step_kind must be in
    # STEP_KIND_REGISTRY. Imports tool modules to populate registry first.
    import orchestrator.agent.tools.compose_output  # noqa: F401
    from orchestrator.observability.decorators import (
        validate_tool_step_registry,
    )

    validate_tool_step_registry()

    # Register scheduled workflows BEFORE launch_dbos so the registered
    # set is in the registry when ``_launch`` (``_dbos.py:523``) computes
    # the launch-time ``app_version`` hash (line 530:
    # ``GlobalParams.app_version = self._registry.compute_app_version()``)
    # and when ``_launch`` drains the deferred-poller queue at
    # ``_dbos.py:683``. ``register_purge_scheduler`` is an explicit
    # call — importing ``orchestrator.dbos_purge`` has no registration
    # side effect, so test fixtures that import the module purely for
    # ``purge_terminal_workflow_inputs`` do not poison the DBOS
    # registry. Cross-process consistency: every process that runs
    # main.py registers in the same order before launch, so the
    # launch-time ``app_version`` hash includes the purge workflow on
    # every process, and the recovery filter at ``_recovery.py:58``
    # (``get_pending_workflows(executor_id, app_version)``) matches.
    #
    # Pytest-fixture isolation: ``shutdown_dbos`` clears
    # ``_dbos_global_registry.dbos`` so the next ``register_poller``
    # call (this one, on the next process's lifespan or the next
    # pytest fixture's launch) takes the deferred-poller branch
    # (``_dbos.py:256``) instead of submitting to the destroyed
    # instance's None executor.
    register_purge_scheduler()
    # VT-28: 4 scheduled trigger workflows. Same register-before-launch
    # contract as register_purge_scheduler — see scheduled_triggers.py
    # docstring for the DBOS app_version invariant.
    register_scheduled_triggers()
    # VT-210: fan-out ingestion scheduler. Same contract.
    from orchestrator.integrations.scheduler import (
        register_ingestion_scheduler,
    )

    register_ingestion_scheduler()
    # VT-202: proactive alerts sweep + daily digest. Same contract.
    from orchestrator.alerts.scheduler import register_alert_scheduler

    register_alert_scheduler()
    # VT-113: daily 10:00 IST email-deliverability check (Resend bounce/complaint). Same contract.
    from orchestrator.alerts.email_deliverability import (
        register_email_deliverability_scheduler,
    )

    register_email_deliverability_scheduler()
    # VT-222: Drive Push delta workflow + 6h renewal + 10min polling
    # fallback. Same register-before-launch contract.
    from orchestrator.integrations.drive_push import (
        register_drive_push_scheduler,
    )

    register_drive_push_scheduler()
    # VT-227: daily 3 AM IST purge of twilio_inbound_replay rows >24h.
    from orchestrator.observability.twilio_replay_purge import (
        register_twilio_replay_purge_scheduler,
    )

    register_twilio_replay_purge_scheduler()
    # VT-226: webhook_metrics writer DBOS workflow (no schedule; invoked
    # imperatively by the admin endpoint).
    from orchestrator.observability.webhook_metrics_writer import (
        register_webhook_metrics_workflow,
    )

    register_webhook_metrics_workflow()
    # VT-384: the L3 auto-send HOLD workflow (l3_hold_workflow). Same
    # register-before-launch contract — the workflow must be in the DBOS
    # registry when launch_dbos() computes the app_version hash so the
    # executor's start_l3_hold + DBOS recovery of a parked hold resolve.
    from orchestrator.agents.l3_hold import register_l3_hold

    register_l3_hold()
    # VT-418: the L2 owner-approve→send driver (l2_send_workflow). Same
    # register-before-launch contract — the workflow must be in the DBOS
    # registry when launch_dbos() computes the app_version hash so the runner's
    # start_l2_send + DBOS recovery of a parked/crashed send run resolve. The
    # reconciler sweep (l2_approved_send_sweep_scheduled) registers with the
    # other scheduled triggers in register_scheduled_triggers (above).
    from orchestrator.agents.l2_send import register_l2_send

    register_l2_send()
    # VT-431: the autonomous agent-coordinator dispatch loop. Same
    # register-before-launch contract — applies @DBOS.workflow to
    # agent_dispatch_workflow + agent_coordinator_scheduled and @DBOS.scheduled
    # (AGENT_COORDINATOR_CRON) to the sweep, so all three are in the DBOS
    # registry when launch_dbos() computes the app_version hash and the daily
    # sweep + DBOS recovery of an in-flight dispatch resolve. This is the
    # activation of the previously-dark loop (the coordinator was built but
    # never registered). Downstream customer sends STAY gated — the executor's
    # detection is structurally fail-closed (empty MARKETING_CONSENT_VERSIONS on
    # prod) and the CL-425 owner_inputs basis is re-checked fail-closed in the
    # dispatch workflow; this only wires the dispatch loop, not any send gate.
    from orchestrator.agents.coordinator import register_agent_coordinator

    register_agent_coordinator()
    launch_dbos()
    # VT-280/VT-281: seed the VTR REF# keying secret from env (VT_REF_HMAC_KEY) so the
    # de-identified views never emit NULL refs before the first VTR read. Best-effort at the
    # lifespan level — a missing key (e.g. dev/CI) is logged loudly, not a startup crash (the
    # orchestrator does far more than the VTR surface); the digest path is ref-independent.
    try:
        from orchestrator.privacy.vtr import bootstrap_vtr_ref_secret

        bootstrap_vtr_ref_secret()
    except Exception:
        logging.getLogger(__name__).exception(
            "VT-281 bootstrap_vtr_ref_secret failed at startup (VT_REF_HMAC_KEY set?)"
        )
    # VT-374 N4: warm the run-control pause cache from workflow_controls so a
    # post-restart control-read error still fails CLOSED for scopes that were paused
    # before the restart (the F9 guarantee is best-effort-after-restart by design).
    # warm_pause_cache itself never raises; the try/except guards the import path —
    # a warm failure must never block worker boot.
    try:
        from orchestrator.run_control import warm_pause_cache

        warm_pause_cache()
    except Exception:
        logging.getLogger(__name__).exception(
            "VT-374 warm_pause_cache failed at startup (best-effort)"
        )
    # VT-481: reap runs stranded status='running' by a prior process that died mid-run
    # (a deploy-restart). DBOS cannot recover a prior-app-version row, so these never close
    # on their own. Runs AFTER launch_dbos() so DBOS's own same-version recovery has already
    # fired; the >1h age floor keeps it clear of any live in-flight run. Best-effort: a reaper
    # failure must never block boot (reap_orphan_runs never raises, but guard the import too).
    #
    # VT-560: these three are a STARTUP CATCH-UP only. The STEADY-STATE re-sweep now runs on
    # the @DBOS.scheduled substrate (scheduled_triggers.py: stalled_task_sweep_scheduled +
    # silent_terminal_sweep_scheduled every 10 min, orphan_run_reaper_scheduled hourly) — a
    # long-lived process would otherwise never re-sweep, so the VT-557 retry ladder never
    # progressed and the VT-552 detector never re-fired. Keep the boot calls for the first pass.
    try:
        from orchestrator.orphan_reaper import (
            detect_silent_terminal_runs,
            reap_orphan_runs,
            reap_stalled_manager_tasks,
        )

        reap_orphan_runs()
        # VT-525 (B2): surface manager_tasks stranded active with no runnable step (same
        # best-effort startup discipline; the >1h floor keeps it clear of a task mid-planning).
        reap_stalled_manager_tasks()
        # VT-552 (B1 part-2b): open incidents for runs that completed with no final_outcome
        # (silent terminals — the owner never heard), same best-effort startup discipline.
        detect_silent_terminal_runs()
    except Exception:
        logging.getLogger(__name__).exception(
            "VT-481 orphan-reaper failed at startup (best-effort)"
        )
    yield
    shutdown_dbos()


app = FastAPI(title="Viabe Team Orchestrator", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — process is up and the API is mounted."""
    return {"status": "ok"}
