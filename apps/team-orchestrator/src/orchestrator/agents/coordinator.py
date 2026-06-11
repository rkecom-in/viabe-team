"""VT-369 Gap-5 PR-1 — master coordinator: deterministic daily dispatch sweep.

The coordinator is a PURE CONSUMER of the Gap-4 roadmap (``business_plan.seams`` —
``items_for_agent`` / ``report_item_status``; it never imports the LLM ``generator`` and never
edits items). The sweep itself is deterministic and zero-LLM (Pillar 1); the LLM lives in the
per-item ``agent_dispatch_workflow`` → specialist executor, never here.

Per tenant, per registered agent, the sweep (plan §1.1):
  1.  reads ``items_for_agent(tid, agent, statuses=('accepted', 'in_progress'))``
  2.  skips any item with an OPEN ``agent_work_items`` row (the migration-125 partial unique —
      claim is a single race-safe ``INSERT … ON CONFLICT DO NOTHING``)
  3.  skips when ``AGENT_AUTONOMY_GLOBAL_FREEZE`` is set (sweep-level kill switch); the
      per-(tenant, agent) ``tenant_agent_autonomy.frozen`` check is the PR-2 :func:`is_frozen` seam
  3.5 skips the tenant when NOT ``_owner_inputs_enabled`` (CL-425: customer PII must never reach
      the Anthropic API without the owner_inputs processing basis) — counted
      ``skipped_no_owner_inputs``, NO status write (the item stays ``accepted``)
  3.7 skips the tenant while ANY open ``pending_approvals`` row exists (per-tenant approval-queue
      serialization, plan §4.1) — counted ``skipped_open_approval``, retried next sweep
  4.  dispatches at most ``MAX_DISPATCHES_PER_TENANT_PER_SWEEP`` (= 1) item per tenant per sweep
  5.  INSERTs ``agent_work_items(status='dispatched')`` and advances the roadmap item
      ``accepted → in_progress`` via the Gap-4 seam (re-dispatch of an already-``in_progress``
      item — a prior work item failed — writes no redundant version)
  6.  starts :func:`agent_dispatch_workflow` (DBOS) with IDs ONLY.

Best-effort per tenant: one tenant's failure never halts the sweep.

IDs-in-state rule (plan §3d): workflow inputs/outputs, ``AgentItemContext`` and
``ItemExecutionResult`` carry ONLY UUIDs/statuses/counters — never names, fact bundles, or draft
params. All PII is re-read from RLS tables by the executor. CL-390: logs carry IDs only.

Registration follows the house register-before-launch pattern (``register_ingestion_scheduler`` /
``register_webhook_metrics_workflow``): import-time is DBOS-side-effect-free;
``register_agent_coordinator()`` applies ``@DBOS.workflow`` + ``@DBOS.scheduled`` and is called
from ``main.py`` lifespan BEFORE ``launch_dbos()``. Chosen over an import-time
``@DBOS.scheduled`` because every post-VT-200 scheduled surface in this repo defers decoration to
an explicit register fn (app_version must not shift on a stray import).
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4, uuid5

from dbos import DBOS

from orchestrator.business_plan.store import OWNING_AGENTS
from orchestrator.observability.log import log_event

logger = logging.getLogger(__name__)

# Plan §1.1: daily, no collision with the 2/3/4/7/8/9am trigger cluster. Written as the plan's
# literal "0 10 * * *" — NOTE the existing registry mixes IST-labelled literals (weekly/trial) and
# UTC-correct crons (VT-304+); hour-10 collides with neither convention's existing slots.
AGENT_COORDINATOR_CRON = "0 10 * * *"

# Sweep-level kill switch (live in PR-1; the per-(tenant, agent) frozen flag is PR-2).
GLOBAL_FREEZE_ENV = "AGENT_AUTONOMY_GLOBAL_FREEZE"

MAX_DISPATCHES_PER_TENANT_PER_SWEEP = 1

# Mirrors the migration-125 CHECK + partial-unique predicate.
WORK_ITEM_STATUSES = frozenset(
    {
        "dispatched",
        "drafting",
        "awaiting_approval",
        "approved",
        "sending",
        "sent",
        "rejected",
        "failed",
        "cancelled",
    }
)
WORK_ITEM_TERMINAL_STATUSES = frozenset({"sent", "rejected", "failed", "cancelled"})
# Statuses an executor may legally report back ('dispatched' is coordinator-minted only).
_RESULT_STATUSES = WORK_ITEM_STATUSES - {"dispatched"}
# VT-374 CAS substrate: every in-workflow status write passes expected_from=<this tuple> so a
# stale writer (DBOS recovery replay, a rerun re-dispatch) can never regress a terminal state
# ('sent' must never become 'drafting' again — STEP-0 §3.3 fix).
_NON_TERMINAL_STATUSES = tuple(sorted(WORK_ITEM_STATUSES - WORK_ITEM_TERMINAL_STATUSES))

# Fixed namespace for the deterministic per-work-item pipeline_runs id (DBOS recovery re-runs the
# workflow body; uuid5 + ON CONFLICT keeps the run row exactly-once).
_AGENT_RUN_NAMESPACE = UUID("0f8a4f2e-9d3b-5c61-8a7e-2b1d6e4c9a50")


# ---------------------------------------------------------------------------
# Specialist-agent contract (plan §1.2) — IDs + counters ONLY, no PII.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentItemContext:
    """Everything a specialist gets: IDs only. The executor re-reads all PII from RLS tables."""

    tenant_id: str
    item_id: str
    agent: str
    work_item_id: str
    run_id: str


@dataclass(frozen=True)
class ItemExecutionResult:
    """Everything a specialist returns: a work-item status + IDs + counters. NO PII — this value
    becomes a DBOS workflow output (checkpointed in dbos system tables; the IDs-in-state rule)."""

    work_item_status: str
    batch_id: str | None = None
    counters: dict[str, int] = field(default_factory=dict)


@runtime_checkable
class SpecialistAgent(Protocol):
    """The closed Gap-5 specialist surface. ``name`` MUST be a member of
    ``business_plan.store.OWNING_AGENTS`` (and equal its registry key)."""

    name: str

    def execute_item(self, ctx: AgentItemContext) -> ItemExecutionResult: ...


# ---------------------------------------------------------------------------
# Registry — static, closed (plan §1.2). Keys are validated against OWNING_AGENTS
# at import time (fail-loud); the executor CLASSES are imported lazily at first
# get_registry() call so this module never cycles into the executor's imports.
# ---------------------------------------------------------------------------

_REGISTRY_SPEC: dict[str, tuple[str, str]] = {
    "sales_recovery": ("orchestrator.agents.sales_recovery_executor", "SalesRecoveryAgent"),
}


def _validate_registry_keys(keys: Any) -> None:
    """Fail-loud: every registry key must be a real dispatchable owning agent.
    ``unassigned`` is the no-agent marker, never a dispatch target."""
    allowed = OWNING_AGENTS - {"unassigned"}
    unknown = set(keys) - allowed
    if unknown:
        raise RuntimeError(
            f"AGENT_REGISTRY keys {sorted(unknown)} are not dispatchable owning agents; "
            f"allowed: {sorted(allowed)} (business_plan.store.OWNING_AGENTS)"
        )


def _validate_registry(registry: dict[str, SpecialistAgent]) -> None:
    """Fail-loud: keys ⊆ OWNING_AGENTS and every instance's ``name`` equals its key."""
    _validate_registry_keys(registry.keys())
    for key, impl in registry.items():
        name = getattr(impl, "name", None)
        if name != key:
            raise RuntimeError(
                f"registry agent under key {key!r} reports name={name!r}; "
                "the key and the agent's name must match"
            )


# Import-time fail-loud (plan §1.2): a typo'd registry key dies at import, not at 10am.
_validate_registry_keys(_REGISTRY_SPEC)

_registry_cache: dict[str, SpecialistAgent] | None = None


def get_registry() -> dict[str, SpecialistAgent]:
    """The static agent registry. Executor classes import HERE (call time), not at module import —
    coordinator.py must stay importable while sibling executor modules land, and the executor is
    free to import this module's types without a cycle. Validated fail-loud on first build."""
    global _registry_cache
    if _registry_cache is None:
        import importlib

        built: dict[str, SpecialistAgent] = {}
        for key, (module_name, class_name) in _REGISTRY_SPEC.items():
            module = importlib.import_module(module_name)
            built[key] = getattr(module, class_name)()
        _validate_registry(built)
        _registry_cache = built
    return _registry_cache


# ---------------------------------------------------------------------------
# PR-2 seam — tenant_agent_autonomy.frozen (table ships with the autonomy substrate).
# ---------------------------------------------------------------------------


def is_frozen(tenant_id: UUID | str, agent: str) -> bool:
    """The per-(tenant, agent) kill switch (VT-369 PR-2): ``tenant_agent_autonomy.frozen``.
    Fail-CLOSED on a read error — a freeze check that can't be answered must not dispatch
    (the inverse of the journey intercept's fail-open: skipping a dispatch is safe, blocking
    owner-inbound isn't; here the safe direction is NOT dispatching)."""
    try:
        from orchestrator.agents.autonomy import get_autonomy

        return get_autonomy(tenant_id, agent).frozen
    except Exception:  # noqa: BLE001 — can't verify ⇒ don't dispatch
        logger.exception(
            "is_frozen check failed tenant=%s agent=%s — treating as FROZEN", tenant_id, agent
        )
        return True


# ---------------------------------------------------------------------------
# Sweep summary
# ---------------------------------------------------------------------------


@dataclass
class CoordinatorSweepSummary:
    """Deterministic sweep counters (no PII — logged verbatim). Tenant-level skip gates
    (owner_inputs / open approval) count ONCE per skipped tenant; item-level gates count per
    item."""

    swept_at_utc: str = ""
    global_freeze: bool = False
    tenants_scanned: int = 0
    dispatched: int = 0
    skipped_open_work_item: int = 0
    skipped_no_owner_inputs: int = 0
    skipped_open_approval: int = 0
    skipped_no_agent: int = 0
    skipped_frozen: int = 0  # always 0 in PR-1 (is_frozen seam)
    tenant_failures: int = 0


# ---------------------------------------------------------------------------
# Consent gate (CL-425) — fail-closed, mirrors runner._brain_owner_inputs_ok.
# ---------------------------------------------------------------------------


def _owner_inputs_ok(tenant_id: str) -> bool:
    """CL-425 fail-closed consent check. The specialist executor transmits customer PII to
    Anthropic during drafting; ``tenants.owner_inputs`` is the lawful processing basis. Any error
    reading the flag fails CLOSED — never dispatch/draft on an unknown consent state."""
    try:
        from orchestrator.memory.l0_writer import _owner_inputs_enabled

        return _owner_inputs_enabled(UUID(tenant_id))
    except Exception:  # noqa: BLE001 — fail-closed on any consent-check error
        logger.warning(
            "agent_coordinator: owner_inputs consent check failed (tenant=%s); fail-closed",
            tenant_id,
        )
        return False


# ---------------------------------------------------------------------------
# Sweep body — deterministic, zero-LLM (Pillar 1)
# ---------------------------------------------------------------------------


def agent_coordinator_scheduled(scheduled_time: datetime, actual_time: datetime) -> None:
    """DBOS scheduled handler — daily coordinator sweep. NO LLM in the sweep itself
    (the LLM lives in the dispatched per-item workflows). Best-effort: a sweep failure
    must not crash the scheduler."""
    try:
        run_coordinator_sweep_body(now=actual_time)
    except Exception:  # noqa: BLE001 — daily sweep is best-effort; next run retries
        logger.exception("VT-369 agent coordinator scheduled sweep failed")


def run_coordinator_sweep_body(
    now: datetime | None = None,
    *,
    registry: dict[str, SpecialistAgent] | None = None,
) -> CoordinatorSweepSummary:
    """The deterministic coordinator sweep (plan §1.1). See the module docstring for the gate
    order. ``registry`` is injectable for tests; default is :func:`get_registry` (fail-loud)."""
    now = now or datetime.now(timezone.utc)
    summary = CoordinatorSweepSummary(swept_at_utc=now.astimezone(timezone.utc).isoformat())
    if os.environ.get(GLOBAL_FREEZE_ENV):
        summary.global_freeze = True
        logger.warning(
            "agent_coordinator: %s set — sweep dispatches nothing", GLOBAL_FREEZE_ENV
        )
        _log_sweep_summary(summary)
        return summary

    reg = registry if registry is not None else get_registry()
    for tenant_id in _tenants_with_plans():
        summary.tenants_scanned += 1
        try:
            _sweep_one_tenant(tenant_id, reg, summary)
        except Exception:  # noqa: BLE001 — one tenant's failure never halts the sweep
            summary.tenant_failures += 1
            logger.exception(
                "agent_coordinator: sweep failed for tenant %s; sweep continues", tenant_id
            )
    _log_sweep_summary(summary)
    return summary


def kick_coordinator(
    tenant_id: UUID | str,
    *,
    now: datetime | None = None,
    registry: dict[str, SpecialistAgent] | None = None,
) -> CoordinatorSweepSummary:
    """Manual single-tenant kick (plan §6 — exported, unwired). Same gates as the daily sweep,
    including the global freeze."""
    now = now or datetime.now(timezone.utc)
    summary = CoordinatorSweepSummary(swept_at_utc=now.astimezone(timezone.utc).isoformat())
    if os.environ.get(GLOBAL_FREEZE_ENV):
        summary.global_freeze = True
        return summary
    reg = registry if registry is not None else get_registry()
    summary.tenants_scanned = 1
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    try:
        _sweep_one_tenant(tid, reg, summary)
    except Exception:  # noqa: BLE001 — mirror the sweep's per-tenant fail-soft contract
        summary.tenant_failures += 1
        logger.exception("agent_coordinator: kick failed for tenant %s", tid)
    return summary


def _log_sweep_summary(summary: CoordinatorSweepSummary) -> None:
    """Workspace-level sweep observability — counters only (CL-390: no PII)."""
    log_event(
        event_type="agent_coordinator_sweep",
        run_id=uuid4(),
        tenant_id=None,
        severity="info",
        component="agent_coordinator",
        payload=asdict(summary),
    )


def _tenants_with_plans() -> list[UUID]:
    """Tenants holding at least one business_plan version — the only tenants with roadmap items
    to dispatch. Workspace-wide service-role read (the ``_scan_timed_out_approvals`` pattern)."""
    from orchestrator.graph import get_pool

    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT tenant_id FROM business_plan ORDER BY tenant_id")
        rows = cur.fetchall()
    out: list[UUID] = []
    for raw in rows:
        value = raw["tenant_id"] if isinstance(raw, dict) else raw[0]
        out.append(UUID(str(value)))
    return out


def _sweep_one_tenant(
    tenant_id: UUID,
    registry: dict[str, SpecialistAgent],
    summary: CoordinatorSweepSummary,
) -> None:
    """Apply the tenant-level gates, then dispatch at most ONE item across all agents."""
    from orchestrator.business_plan import seams
    from orchestrator.db import tenant_connection
    from orchestrator.db.wrappers import PendingApprovalsWrapper

    # Gate 3.5 (CL-425): no owner_inputs basis → no dispatch, NO status write.
    if not _owner_inputs_ok(str(tenant_id)):
        summary.skipped_no_owner_inputs += 1
        return
    # Gate 3.7 (plan §4.1 queue serialization): any open approval defers the whole tenant.
    if PendingApprovalsWrapper().find_open_for_tenant(tenant_id) is not None:
        summary.skipped_open_approval += 1
        return

    dispatched_for_tenant = 0
    for agent in sorted(OWNING_AGENTS - {"unassigned"}):
        if dispatched_for_tenant >= MAX_DISPATCHES_PER_TENANT_PER_SWEEP:
            break
        items = seams.items_for_agent(tenant_id, agent, statuses=("accepted", "in_progress"))
        if not items:
            continue
        if agent not in registry:
            # Unregistered owner → counted, never an error, never a status write (plan §1.2).
            summary.skipped_no_agent += len(items)
            continue
        if is_frozen(tenant_id, agent):  # PR-2 seam — always False in PR-1
            summary.skipped_frozen += 1
            continue
        for item in items:
            if dispatched_for_tenant >= MAX_DISPATCHES_PER_TENANT_PER_SWEEP:
                break
            with tenant_connection(tenant_id) as conn:
                work_item_id = _claim_work_item(conn, tenant_id, item.item_id, agent)
            if work_item_id is None:
                # An open work item already covers this roadmap item (dedupe, gate 2).
                summary.skipped_open_work_item += 1
                continue
            if item.status == "accepted":
                # Gap-4 seam write: accepted → in_progress at dispatch (plan §1.3). An item
                # already in_progress (re-dispatch after a failed work item) writes nothing.
                seams.report_item_status(tenant_id, item.item_id, "in_progress", agent=agent)
            DBOS.start_workflow(
                agent_dispatch_workflow, str(tenant_id), item.item_id, agent, work_item_id
            )
            dispatched_for_tenant += 1
            summary.dispatched += 1
            logger.info(
                "agent_coordinator: dispatched tenant=%s agent=%s item=%s work_item=%s",
                tenant_id,
                agent,
                item.item_id,
                work_item_id,
            )


def _claim_work_item(conn: Any, tenant_id: UUID, item_id: str, agent: str) -> str | None:
    """Race-safe claim: INSERT against the migration-125 partial unique (one OPEN work item per
    (tenant, roadmap item)). Returns the new work_item_id, or None when an open row already
    exists (dedupe — including a concurrent sweep losing the race)."""
    row = conn.execute(
        "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
        "VALUES (%s, %s, %s, 'dispatched') "
        "ON CONFLICT (tenant_id, item_id) "
        "WHERE status NOT IN ('sent', 'rejected', 'failed', 'cancelled') "
        "DO NOTHING "
        "RETURNING id::text",
        (str(tenant_id), item_id, agent),
    ).fetchone()
    if row is None:
        return None
    return str(row["id"] if isinstance(row, dict) else row[0])


# ---------------------------------------------------------------------------
# VT-374 run-control seam (kind 'agent_dispatch', step 'execute_item')
# ---------------------------------------------------------------------------


def _hold_for_run_control(tenant_id: str) -> int:
    """Block while (tenant, 'agent_dispatch') is paused; return paused_ms.

    Durable variant (each read its own @DBOS.step + DBOS.sleep) when executing inside a
    DBOS workflow — a paused dispatch survives a worker restart and resumes the hold.
    Plain poll for direct calls (tests / admin kick). check_pause inside never raises
    (F9 two-tier): a control-read outage cannot kill a live dispatch."""
    from orchestrator import run_control

    if DBOS.workflow_id is not None:
        return run_control.hold_while_paused_durable(tenant_id, "agent_dispatch")
    return run_control.hold_while_paused(tenant_id, "agent_dispatch")


def _consume_execute_override(tenant_id: str, run_id: str) -> Any | None:
    """Consume-first claim of the (agent_dispatch, execute_item) one-shot override (F8/N2).

    The v1 registry allow-lists no keys for this step, so a consumed row records the
    operator's intervention (override_id on the timeline) but pins nothing — the
    key-bearing pins live at the executor sub-steps (candidate_build/compose_drafts).
    A control-DB failure proceeds WITHOUT the override, logged loudly — never a new
    exception path that kills the dispatch (F9 spirit)."""
    try:
        from orchestrator.graph import get_pool
        from orchestrator.run_control import consume_override

        with get_pool().connection() as conn:
            return consume_override(
                conn,
                tenant_id=tenant_id,
                workflow_kind="agent_dispatch",
                step_name="execute_item",
                run_id=run_id,
            )
    except Exception:  # noqa: BLE001 — control outage must not fail the work item
        logger.warning(
            "agent_coordinator: execute_item override consume failed (tenant=%s run=%s) — "
            "proceeding without",
            tenant_id,
            run_id,
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Per-item dispatch workflow — the LLM lives downstream of HERE (in the executor)
# ---------------------------------------------------------------------------


def agent_dispatch_workflow(
    tenant_id: str,
    item_id: str,
    agent: str,
    work_item_id: str,
    rerun_run_id: str | None = None,
) -> dict[str, Any]:
    """Execute ONE dispatched work item via the registry agent. DBOS-decorated at registration.

    Order: re-check the CL-425 owner_inputs gate FAIL-CLOSED (consent may have been revoked in
    the sweep→workflow gap; the work item is cancelled so the partial unique frees up), open a
    pipeline_runs row (deterministic uuid5 id — ``pending_approvals.run_id`` FKs it, and DBOS
    recovery re-entering the body stays exactly-once via ON CONFLICT), invoke
    ``execute_item(ctx)``, persist the resulting work-item status.

    VT-374 A4/F3: ``rerun_run_id`` (set ONLY by ``run_control.rerun.rerun_from``) is the
    rerun's FRESH uuid4 run identity — ``_open_agent_run`` adopts it instead of
    uuid5(work_item_id), so a rerun gets its own lineage-stamped run row (pre-inserted by
    rerun.py) and never continues the source run's. As a workflow ARG it is checkpointed,
    so DBOS recovery re-enters with the same identity. ``None`` (every coordinator-sweep
    dispatch) keeps the deterministic uuid5 path.

    IDs-in-state (plan §3d): inputs/outputs carry ONLY UUIDs + statuses + counters (DBOS
    checkpoints them in its system tables). Fail-soft: ANY failure marks the work item 'failed'
    and returns a status dict — this workflow never raises (one item's failure must not poison
    the queue; the next sweep may re-dispatch)."""
    try:
        if not _owner_inputs_ok(tenant_id):
            _set_work_item_status(
                tenant_id, work_item_id, "cancelled", expected_from=_NON_TERMINAL_STATUSES
            )
            log_event(
                event_type="agent_dispatch_skipped_no_owner_inputs",
                run_id=uuid4(),
                tenant_id=UUID(tenant_id),
                severity="info",
                component="agent_coordinator",
                payload={"work_item_id": work_item_id, "item_id": item_id, "agent": agent},
            )
            return {
                "status": "skipped_no_owner_inputs",
                "work_item_id": work_item_id,
                "item_id": item_id,
            }
        run_id = _open_agent_run(tenant_id, work_item_id, rerun_run_id=rerun_run_id)
        _set_work_item_status(
            tenant_id,
            work_item_id,
            "drafting",
            run_id=run_id,
            expected_from=_NON_TERMINAL_STATUSES,
        )
        impl = get_registry().get(agent)
        if impl is None:
            raise KeyError(f"agent {agent!r} is not registered")  # → fail-soft 'failed' below
        ctx = AgentItemContext(
            tenant_id=tenant_id,
            item_id=item_id,
            agent=agent,
            work_item_id=work_item_id,
            run_id=run_id,
        )
        # VT-374 — the (agent_dispatch, execute_item) controllable boundary: hold while
        # the tenant's agent_dispatch kind is paused, then consume-first claim any
        # pre-registered override for this run (the CL-425 gate above stays FIRST —
        # a pause hold never defers a consent check).
        paused_ms = _hold_for_run_control(tenant_id)
        override = _consume_execute_override(tenant_id, run_id)
        if paused_ms or override is not None:
            # B1 dead-columns fix: one run_control_intervention timeline row with the
            # mig-131 override_id / paused_ms COLUMNS populated. record_intervention
            # never raises — a timeline miss must not alter control semantics (F9).
            from orchestrator.observability.pipeline_observability import (
                record_intervention,
            )

            record_intervention(
                tenant_id,
                run_id,
                workflow_kind="agent_dispatch",
                step_name="execute_item",
                override_id=override.id if override is not None else None,
                paused_ms=paused_ms or None,
                action="override_consumed" if override is not None else "released",
            )
        result = impl.execute_item(ctx)
        status = result.work_item_status
        if status not in _RESULT_STATUSES:
            logger.error(
                "agent_dispatch_workflow: agent %s returned invalid work_item_status %r "
                "(work_item=%s); recording 'failed'",
                agent,
                status,
                work_item_id,
            )
            status = "failed"
        _set_work_item_status(
            tenant_id, work_item_id, status, expected_from=_NON_TERMINAL_STATUSES
        )
        _close_agent_run(tenant_id, run_id, status, work_item_id)
        return {
            "status": status,
            "work_item_id": work_item_id,
            "item_id": item_id,
            "run_id": run_id,
            "batch_id": result.batch_id,
            "counters": dict(result.counters),
        }
    except Exception:  # noqa: BLE001 — fail-soft: never raises (CL-390: IDs only in the log)
        logger.exception(
            "agent_dispatch_workflow failed (tenant=%s agent=%s item=%s work_item=%s)",
            tenant_id,
            agent,
            item_id,
            work_item_id,
        )
        try:
            _set_work_item_status(
                tenant_id, work_item_id, "failed", expected_from=_NON_TERMINAL_STATUSES
            )
            _close_agent_run(
                tenant_id,
                rerun_run_id or _agent_run_id(work_item_id),
                "failed",
                work_item_id,
            )
        except Exception:  # noqa: BLE001 — best-effort terminal write
            logger.exception(
                "agent_dispatch_workflow: failed-state write also failed (work_item=%s)",
                work_item_id,
            )
        return {"status": "failed", "work_item_id": work_item_id, "item_id": item_id}


def _agent_run_id(work_item_id: str) -> str:
    """Deterministic pipeline_runs id for a work item — recovery-safe (uuid5, not uuid4)."""
    return str(uuid5(_AGENT_RUN_NAMESPACE, f"agent_dispatch:{work_item_id}"))


def _open_agent_run(
    tenant_id: str, work_item_id: str, *, rerun_run_id: str | None = None
) -> str:
    """Open the run row this dispatch hangs off (``pending_approvals.run_id`` FKs it).
    Idempotent — deterministic id + ON CONFLICT DO NOTHING (mirrors runner.open_run).

    VT-374 A4: ``rerun_run_id`` (a rerun's fresh uuid4) is adopted verbatim — the
    lineage-stamped row was pre-inserted by rerun.py, so the INSERT here no-ops on
    conflict and the rerun never touches the uuid5 source-run row."""
    from orchestrator.db import tenant_connection

    run_id = rerun_run_id or _agent_run_id(work_item_id)
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'agent_dispatch', 'running') "
            "ON CONFLICT (id) DO NOTHING",
            (run_id, tenant_id),
        )
    return run_id


def _close_agent_run(
    tenant_id: str, run_id: str, work_item_status: str, work_item_id: str
) -> None:
    """Close the dispatch run. Terminal metadata is IDs + status only (CL-390)."""
    from psycopg.types.json import Jsonb

    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status = 'completed', ended_at = now(), "
            "terminal_state_metadata = %s "
            "WHERE id = %s AND tenant_id = %s",
            (
                Jsonb({"work_item_status": work_item_status, "work_item_id": work_item_id}),
                run_id,
                tenant_id,
            ),
        )


def _set_work_item_status(
    tenant_id: str,
    work_item_id: str,
    status: str,
    *,
    run_id: str | None = None,
    expected_from: tuple[str, ...] | None = None,
) -> None:
    """Advance the work-item ledger row (RLS via tenant_connection).

    VT-374 CAS guard: with ``expected_from`` the UPDATE applies only while the current
    status is in that set (``AND status = ANY(...)``) — a stale writer (DBOS recovery
    replaying the workflow body, a rerun re-dispatch) can never regress a terminal state.
    A CAS no-op is logged, never raised: the newer state wins by design. ``None`` keeps
    the legacy unconditional write for callers outside the dispatch workflow."""
    if status not in WORK_ITEM_STATUSES:
        raise ValueError(f"unknown work-item status {status!r}")
    if expected_from is not None:
        unknown = set(expected_from) - WORK_ITEM_STATUSES
        if unknown:
            raise ValueError(f"unknown expected_from statuses {sorted(unknown)!r}")
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        if expected_from is None:
            conn.execute(
                "UPDATE agent_work_items SET status = %s, run_id = COALESCE(%s, run_id), "
                "updated_at = now() "
                "WHERE tenant_id = %s AND id = %s",
                (status, run_id, tenant_id, work_item_id),
            )
            return
        cur = conn.execute(
            "UPDATE agent_work_items SET status = %s, run_id = COALESCE(%s, run_id), "
            "updated_at = now() "
            "WHERE tenant_id = %s AND id = %s AND status = ANY(%s)",
            (status, run_id, tenant_id, work_item_id, list(expected_from)),
        )
        if cur.rowcount == 0:
            logger.warning(
                "agent_coordinator: status CAS no-op (work_item=%s -> %r; current state "
                "not in expected_from) — stale write suppressed",
                work_item_id,
                status,
            )


# ---------------------------------------------------------------------------
# Registration — register-before-launch_dbos() (house pattern, see module docstring)
# ---------------------------------------------------------------------------

_registered = False


def register_agent_coordinator() -> None:
    """Apply ``@DBOS.workflow`` to :func:`agent_dispatch_workflow` and ``@DBOS.workflow`` +
    ``@DBOS.scheduled`` to the sweep handler. Call from ``main.py`` lifespan BEFORE
    ``launch_dbos()`` (mirrors ``register_ingestion_scheduler``; workflow-before-scheduled per
    the VT-215 lesson). Idempotent — duplicate calls must not re-register and shift
    ``app_version`` mid-run."""
    global _registered
    if _registered:
        return
    DBOS.workflow()(agent_dispatch_workflow)
    DBOS.workflow()(agent_coordinator_scheduled)
    DBOS.scheduled(AGENT_COORDINATOR_CRON)(agent_coordinator_scheduled)
    _registered = True


__all__ = [
    "AGENT_COORDINATOR_CRON",
    "AgentItemContext",
    "CoordinatorSweepSummary",
    "GLOBAL_FREEZE_ENV",
    "ItemExecutionResult",
    "MAX_DISPATCHES_PER_TENANT_PER_SWEEP",
    "SpecialistAgent",
    "WORK_ITEM_STATUSES",
    "WORK_ITEM_TERMINAL_STATUSES",
    "agent_coordinator_scheduled",
    "agent_dispatch_workflow",
    "get_registry",
    "is_frozen",
    "kick_coordinator",
    "register_agent_coordinator",
    "run_coordinator_sweep_body",
]
