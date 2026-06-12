"""VT-374 ``rerun_from`` — app-level re-dispatch (plan §8; F2/F3/F10/F11).

Re-run is honest RE-DISPATCH, not history time-travel: a refused kind refuses
(I8 — never a silent no-op), every gate re-evaluates, approvals are never
inherited (I2), and prefix steps re-execute only if the kind's entry point
requires them.

VT-375 C1 (Cowork ruling 20260611T234500Z, Option A): the F10 open-approval
409 gate has a TOCTOU window — an owner approval can arm AFTER the gate passed
but DURING the rerun's execution. Every arm re-checks open approvals once its
work/dispatch is done; an overlap closes the lineage row ``'escalated'`` +
emits a ``run_control_rerun_overlap`` pipeline_log alert and surfaces
``outcome='escalated_overlap'`` (``rerun_from`` returns :class:`RerunResult`).
The guarantee stack is mig-128 (never two open approvals) + detect-and-escalate
(never a silent keep) — see ``_finalize_outcome``.

Lineage stamping (the contract's "decide and document" point):

- EVERY arm mints a fresh uuid4 run id (F3) and INSERTS the lineage-stamped
  ``pipeline_runs`` row itself — that INSERT *is* the open. Synchronous arms
  (plan_generate, plan_deliver) close the row on completion. Async DBOS arms
  (auto_discovery, ingestion) close the row IMMEDIATELY after dispatch
  (status 'completed', ``final_outcome='dispatched_async'`` + the structural
  child dispatch params in metadata): the async child carries its OWN
  (non-pipeline_runs) observability today, so a perpetually-'running' row
  would be a lie, not a handle.
- agent_dispatch: the fresh uuid4 id is PASSED INTO the dispatch workflow
  (``rerun_run_id`` final param) — ``_open_agent_run`` adopts it instead of
  the deterministic uuid5(work_item_id) identity, the lineage columns are
  stamped on that NEW row at insert, and the workflow closes it on
  completion. Overrides bind to that id — the id the seam actually passes to
  ``consume_override``. The executor mints a NEW draft batch per execution,
  so a rerun re-enters owner approval (I2) rather than reusing artifacts.

Override pre-registration binding:

- agent_dispatch / plan_generate / plan_deliver → bound to the minted run id.
- auto_discovery / ingestion → next-run pins (``workflow_id`` NULL) with a
  short TTL (these entry points carry no externally-settable run identity);
  the only run of that kind expected inside the TTL is the rerun itself.

Top-level imports are stdlib + the dep-less registry only; everything heavy
(dbos, psycopg types, orchestrator entry points) imports lazily per arm.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from orchestrator.run_control.registry import KIND_RERUN_POLICY, REGISTRY, RERUNNABLE

logger = logging.getLogger(__name__)

# VT-376 rerun-slot advisory-lock NAMESPACE (Cowork plan ruling 20260612T015000Z:
# hashtext collisions across features are silent, so every pg_advisory user documents a
# distinct keyspace). The EXISTING advisory-lock users in this codebase:
#   * observability/audit_log._CHAIN_LOCK_KEY — the fixed INT literal 8080_8080 (one
#     global audit chain ⇒ one key; no hashtext involved).
#   * api/razorpay_subscribe — pg_advisory_xact_lock(hashtext('subscribe:{tenant_id}'))
#     serializing per-tenant subscription creates.
# This lock hashes 'rerun-slot:{tenant_id}' — the prefix guarantees the INPUT string can
# never equal a 'subscribe:' string, so collision with razorpay (or the audit chain's
# fixed key) is only the generic 1-in-2^32 hashtext collision all three users already
# share. A cross-feature collision degrades to brief false serialization (this lock is
# xact-scoped and held for milliseconds), never deadlock or corruption.
_RERUN_SLOT_NS = "rerun-slot:"

# pipeline_runs.run_type → registry workflow_kind. Legacy run_types map onto
# webhook_inbound; rerun-minted rows (and seam-opened rows) write run_type =
# workflow_kind, so the mapping is identity for the new kinds.
RUN_TYPE_TO_KIND: dict[str, str] = {
    "twilio_inbound": "webhook_inbound",
    "orchestrator": "webhook_inbound",
    "agent_dispatch": "agent_dispatch",
    "auto_discovery": "auto_discovery",
    "plan_generate": "plan_generate",
    "plan_deliver": "plan_deliver",
    "ingestion": "ingestion",
    "trial_sweep": "trial_sweep",
    "campaign_send": "campaign_send",
}

# Mirror of the migration-125 partial-unique terminal set (coordinator._claim_work_item).
_TERMINAL_WORK_ITEM_STATUSES = frozenset({"sent", "rejected", "failed", "cancelled"})

# Next-run override pins minted by a rerun must die fast — the rerun is the only
# run of that kind expected inside the window (F8: NULL-workflow pins REQUIRE expiry).
_NEXT_RUN_OVERRIDE_TTL_S = 3600

# VT-381 F1 — stale-'running' rerun lineage TTL. A hard-killed SYNCHRONOUS rerun
# (plan_generate / plan_deliver — NOT the DBOS-recovered agent_dispatch arm) can die
# between its lineage insert and the best-effort failed-close, leaving a 'running'
# lineage row that ``_refuse_on_inflight_rerun`` would 409 on FOREVER. A 'running'
# lineage row OLDER than this TTL is treated as orphaned: the stale-close path closes it
# ('completed' + ``final_outcome='aborted_stale'`` — the mig-052 house pattern) and the
# new rerun PROCEEDS. A FRESH 'running' row still 409s (a genuinely-in-flight rerun).
_RERUN_STALE_TTL_S = 3600

_REASON_MAX_LEN = 500  # matches the migration-131 reason cap (F7)


class RerunRefused(RuntimeError):
    """Refusal the ops API maps to HTTP: 409 (conflict) / 422 (invalid target) /
    503 (name-registry build failure — redaction fails CLOSED, A1)."""

    def __init__(self, message: str, *, code: int = 409) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RerunResult:
    """``rerun_from``'s outcome-bearing return (VT-375 C1, Cowork ruling
    20260611T234500Z — Option A).

    ``outcome``:

    - ``'completed'`` — the arm finished (or dispatched) with NO open approval at
      the post-arm re-check; the lineage row closed on the normal house pattern
      (agent_dispatch's row stays 'running' for its child workflow to close).
    - ``'escalated_overlap'`` — an owner approval armed DURING the rerun's
      execution window (the gate-window TOCTOU the 409 gate cannot close). The
      arm's effects STAND (the ruling: no rollback — e.g. a minted plan version
      stays), the lineage row closed ``status='escalated'`` with
      ``final_outcome='rerun_overlapped_open_approval'``, and a structured
      ``run_control_rerun_overlap`` alert was emitted — escalated and disclosed,
      never silently kept.

    The ops API is the sole production consumer; it surfaces ``outcome`` in the
    /rerun response verbatim.
    """

    run_id: UUID
    outcome: str  # 'completed' | 'escalated_overlap'


def rerun_from(
    source_run_id: UUID | str,
    from_step: str,
    overrides: list[dict[str, Any]] | None = None,
    *,
    requested_by: UUID | str,
) -> RerunResult:
    """Re-dispatch the source run's workflow_kind from ``from_step``.

    Returns :class:`RerunResult` — the fresh run id PLUS an ``outcome``
    ('completed' | 'escalated_overlap'). The ops API /rerun handler is the sole
    production consumer and surfaces the outcome verbatim.

    Refuses (RerunRefused): unknown source run; kind not in RERUNNABLE (I8 policy);
    unknown step for the kind; ANY open pending approval for the tenant (409, F10 —
    the structural guarantee is migration-128's one-open-per-tenant partial unique;
    this 409 is the UX layer on top); invalid override pins (allowed-keys / pure_return
    / non-controllable step); kind-specific preflight failures (terminal work item,
    no grounded profile, no plan version, no connector identity).

    Order: validate → [rerun-slot lock: 409 gates (open approval + in-flight rerun) →
    arm preflight → pre-register overrides → lineage insert] → lock released → dispatch →
    post-arm overlap re-check (VT-375 C1, Cowork ruling 20260611T234500Z / Option A).
    The VT-376 rerun-slot lock (``pg_advisory_xact_lock(hashtext('rerun-slot:{tenant}'))``)
    serializes rerun-vs-RERUN for one tenant — the double-click hazard the panel button
    introduces — across EXACTLY the gate-check → lineage-row-insert window. The arm's
    dispatch/LLM work runs via the closure each arm returns, AFTER the lock context
    exited; the live owner-approval arm path takes no lock, so a priority inversion is
    impossible by construction (arm-vs-rerun overlap stays detect-and-escalate, the C1
    ruling).
    The 409 gate has a TOCTOU window: an owner approval can arm AFTER the gate
    passed but DURING the arm's execution. Every arm therefore re-checks the
    tenant's open approvals (the same ``find_open_approval_for_tenant`` helper the
    gate uses) once its work/dispatch is done — synchronous arms (plan_generate /
    plan_deliver) after their work, async arms (agent_dispatch / auto_discovery /
    ingestion) after dispatch, BEFORE their lineage close so an overlap closes the
    row 'escalated' instead of 'completed'/'dispatched_async'. On overlap the
    arm's effects STAND (no rollback — the ruling), the row closes 'escalated'
    with ``final_outcome='rerun_overlapped_open_approval'``, and a
    ``run_control_rerun_overlap`` alert lands in pipeline_log — never a silent
    keep. See :func:`_finalize_outcome`.

    Source-run rows are never mutated, with the one documented exception of the
    agent_dispatch lineage stamp (see module docstring).
    """
    source = _load_source_run(source_run_id)
    if source is None:
        raise RerunRefused(f"unknown source run {source_run_id}", code=422)
    run_type = source["run_type"] or ""
    kind = RUN_TYPE_TO_KIND.get(run_type)
    if kind is None:
        raise RerunRefused(f"source run has unmapped run_type {run_type!r}", code=422)
    if kind not in RERUNNABLE:
        raise RerunRefused(
            f"workflow_kind {kind!r} is not re-runnable "
            f"(side-effect policy: {KIND_RERUN_POLICY[kind]!r}, I8/F11)",
            code=422,
        )
    if (kind, from_step) not in REGISTRY:
        raise RerunRefused(f"unknown step {from_step!r} for kind {kind!r}", code=422)

    tenant_id = _as_uuid(source["tenant_id"])
    validated = _validate_overrides(kind, from_step, overrides or [])

    # VT-376: gate-check → lineage insert under the per-tenant rerun-slot lock; the
    # returned dispatch closure runs AFTER the lock released (never held across
    # dispatch/LLM work — see the docstring + _rerun_slot_lock).
    with _rerun_slot_lock(tenant_id):
        _refuse_on_open_approval(tenant_id)
        _refuse_on_inflight_rerun(tenant_id, _as_uuid(source_run_id))
        if kind == "agent_dispatch":
            dispatch = _rerun_agent_dispatch(
                tenant_id, _as_uuid(source_run_id), from_step, validated, requested_by
            )
        elif kind == "auto_discovery":
            dispatch = _rerun_auto_discovery(
                tenant_id, _as_uuid(source_run_id), from_step, validated, requested_by
            )
        elif kind == "plan_generate":
            dispatch = _rerun_plan_generate(
                tenant_id, _as_uuid(source_run_id), from_step, validated, requested_by
            )
        elif kind == "plan_deliver":
            dispatch = _rerun_plan_deliver(
                tenant_id, _as_uuid(source_run_id), from_step, validated, requested_by
            )
        elif kind == "ingestion":
            dispatch = _rerun_ingestion(
                tenant_id,
                _as_uuid(source_run_id),
                from_step,
                validated,
                requested_by,
                source.get("trigger_payload"),
            )
        else:  # unreachable — RERUNNABLE pinned the kinds above
            raise RerunRefused(f"no re-dispatch arm for kind {kind!r}", code=422)
    return dispatch()


# --- refusal gates ------------------------------------------------------------------


@contextmanager
def _rerun_slot_lock(tenant_id: UUID) -> Iterator[None]:
    """VT-376 rerun-slot lock — serializes rerun-vs-RERUN for ONE tenant (the panel
    button's double-click hazard; Cowork plan ruling 20260612T015000Z, arm a).

    Held across EXACTLY the gate-check → lineage-row-insert window (milliseconds of DB
    reads + at most a handful of inserts): the helpers inside the block run on their own
    connections, and every one of their writes commits before this context exits, so the
    next lock holder reads the winner's committed state. ``rerun_from`` invokes the arm's
    dispatch closure AFTER this context exits — the lock is never held across DBOS
    dispatch / LLM / delivery work, and the live ``request_owner_approval`` arm path takes
    no lock at all (inversion impossible by construction; arm-vs-rerun stays
    detect-and-escalate per the C1 ruling).

    ``pg_advisory_xact_lock`` is transaction-scoped: released when the wrapping
    transaction ends (commit, error, or session death) — no unlock path to forget.
    Keyspace: ``hashtext(_RERUN_SLOT_NS + tenant_id)`` — see the constant's comment for
    the collision analysis against the existing advisory users (audit_log chain key,
    razorpay 'subscribe:').
    """
    from orchestrator.graph import get_pool  # lazy — dep-less module import

    with get_pool().connection() as conn, conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"{_RERUN_SLOT_NS}{tenant_id}",),
        )
        yield


def _refuse_on_inflight_rerun(tenant_id: UUID, source_run_id: UUID) -> None:
    """VT-376 double-click gate — runs UNDER the rerun-slot lock. 409 while an UNFINISHED
    rerun of the SAME source run exists (a lineage row with ``rerun_of_run_id = source``
    still 'running'). The winner inserts its lineage row before releasing the lock, so two
    racing clicks serialize: the loser acquires the lock, observes the winner's committed
    'running' row, and refuses here — never two live lineage rows for one source. A
    FINISHED prior rerun (row closed: completed / escalated / paused…) does NOT refuse —
    deliberately re-running the same source later is legitimate.

    VT-381 F1 — stale-TTL leg: a hard-killed SYNCHRONOUS rerun (plan_generate /
    plan_deliver — NOT the DBOS-recovered agent_dispatch arm) can die between its lineage
    insert and the best-effort failed-close, leaving a 'running' lineage row that would
    hold THIS gate forever. A 'running' lineage row OLDER than ``_RERUN_STALE_TTL_S`` is
    therefore treated as orphaned: the stale-close path closes it ('completed' +
    ``final_outcome='aborted_stale'`` — the mig-052 house pattern, same as
    ``_close_lineage_row``) and the new rerun PROCEEDS (no refusal). A FRESH 'running' row
    (younger than the TTL) still 409s — a genuinely-in-flight rerun (the double-click
    serialization above) must never be aborted out from under itself. Stale rows are aged
    on ``started_at`` (the lineage insert stamps it at row open)."""
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT id::text, "
            "started_at < now() - make_interval(secs => %s) AS is_stale "
            "FROM pipeline_runs "
            "WHERE tenant_id = %s AND rerun_of_run_id = %s AND status = 'running' "
            "ORDER BY started_at LIMIT 1",
            (_RERUN_STALE_TTL_S, str(tenant_id), str(source_run_id)),
        ).fetchone()
    if row is None:
        return  # no in-flight rerun — proceed
    if not isinstance(row, dict):
        row = dict(zip(("id", "is_stale"), row, strict=True))
    if row["is_stale"]:
        # Orphaned stale 'running' row — close it on the house pattern and PROCEED.
        _close_lineage_row(
            tenant_id,
            _as_uuid(row["id"]),
            meta={"final_outcome": "aborted_stale"},
        )
        logger.info(
            "run_control rerun: closed stale 'running' lineage row tenant=%s source=%s "
            "stale_run=%s (aborted_stale) — proceeding",
            tenant_id,
            source_run_id,
            row["id"],
        )
        return
    raise RerunRefused(
        "a rerun of this source run is already in flight — wait for it to finish",
        code=409,
    )


def _refuse_if_paused(tenant_id: UUID, workflow_kind: str) -> None:
    """A6: the synchronous plan arms call generator/delivery INTERNALS directly,
    bypassing the workflow seams' own pause holds — so an active (tenant, kind)
    hold must refuse the rerun here, or /rerun becomes a pause bypass.
    check_pause never raises (F9 two-tier)."""
    from orchestrator.run_control import check_pause

    if check_pause(tenant_id, workflow_kind):
        raise RerunRefused(
            f"tenant paused for {workflow_kind} — release before rerun", code=409
        )


def _refuse_on_open_approval(tenant_id: UUID) -> None:
    """F10: 409 while the tenant has ANY open approval — the owner's YES must never be
    ambiguous about WHICH run it approves. The structural layer (migration-128 partial
    unique + request_owner_approval step-0b refusal) backstops this UX check."""
    from orchestrator.agent.approval_resume import find_open_approval_for_tenant
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        approval = find_open_approval_for_tenant(conn, tenant_id)
    if approval is not None:
        raise RerunRefused(
            "tenant has an open pending approval — resolve it before re-running (F10)",
            code=409,
        )


def _validate_overrides(
    kind: str, from_step: str, overrides: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Registry validation, defense-in-depth with the ops API's 422 layer (F6/I7)."""
    validated: list[dict[str, Any]] = []
    for item in overrides:
        step_name = item.get("step_name") or from_step
        entry = REGISTRY.get((kind, step_name))
        if entry is None:
            raise RerunRefused(f"unknown override step {step_name!r} for {kind!r}", code=422)
        if entry.tier != "controllable":
            raise RerunRefused(
                f"step ({kind!r}, {step_name!r}) is observed-only — not controllable",
                code=422,
            )
        pinned_input = item.get("pinned_input")
        pinned_output = item.get("pinned_output")
        if pinned_output and not entry.pure_return:
            raise RerunRefused(
                f"pinned_output is legal only for pure_return steps; "
                f"({kind!r}, {step_name!r}) is not",
                code=422,
            )
        if pinned_input:
            illegal = set(pinned_input) - set(entry.allowed_keys)
            if illegal:
                raise RerunRefused(
                    f"pinned_input keys {sorted(illegal)!r} not allow-listed for "
                    f"({kind!r}, {step_name!r})",
                    code=422,
                )
        if not pinned_input and not pinned_output:
            raise RerunRefused(
                f"override for ({kind!r}, {step_name!r}) pins nothing", code=422
            )
        validated.append(
            {
                "step_name": step_name,
                "pinned_input": pinned_input,
                "pinned_output": pinned_output,
                "reason": item.get("reason"),
            }
        )
    return validated


# --- per-kind arms ------------------------------------------------------------------


def _rerun_agent_dispatch(
    tenant_id: UUID,
    source_run_id: UUID,
    from_step: str,
    validated: list[dict[str, Any]],
    requested_by: UUID | str,
) -> Callable[[], RerunResult]:
    """Re-dispatch the SAME work item — non-terminal only (CAS guard owns regression).

    VT-376 two-phase shape (all arms): the function BODY is the pre-dispatch phase and
    runs UNDER the rerun-slot lock (preflight reads + override registration + lineage
    insert — milliseconds); it returns the dispatch closure ``rerun_from`` invokes AFTER
    the lock released (DBOS dispatch + the C1 finalize never hold the lock).

    F3 fresh identity (A4): a NEW uuid4 run id is minted here, inserted with the
    lineage stamp, and passed into the dispatch workflow as ``rerun_run_id`` —
    ``_open_agent_run`` adopts it instead of uuid5(work_item_id), so the rerun
    never continues (or mutates) the source run row. Overrides bind to the new
    id — the id the seam passes to ``consume_override``. The workflow closes the
    row on completion.

    C1 overlap re-check (post-dispatch): the dispatch is KEPT on overlap — it
    already happened, and the child workflow re-enters its own owner-approval
    gates (I2: approvals are never inherited), so an overlapped dispatch cannot
    silently send. The overlap close targets the row state this arm leaves it in
    ('running' — the child owns the completion close): it stamps 'escalated' now;
    the child's unconditional completion close may later overwrite that stamp, so
    the durable overlap record is the ``run_control_rerun_overlap`` pipeline_log
    alert + the surfaced 'escalated_overlap' outcome, not the row status alone.
    """
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT id::text AS id, item_id, agent, status FROM agent_work_items "
            "WHERE tenant_id = %s AND run_id = %s",
            (str(tenant_id), str(source_run_id)),
        ).fetchone()
    if row is None:
        raise RerunRefused("source run has no agent work item", code=422)
    if not isinstance(row, dict):
        row = dict(zip(("id", "item_id", "agent", "status"), row, strict=True))
    if row["status"] in _TERMINAL_WORK_ITEM_STATUSES:
        raise RerunRefused(
            f"work item is terminal ({row['status']!r}) — re-run refused; the status "
            "CAS guard forbids regression",
            code=409,
        )

    new_run_id = uuid4()
    _register_overrides(
        tenant_id,
        "agent_dispatch",
        validated,
        workflow_id=new_run_id,  # the fresh identity the seam passes to consume
        requested_by=requested_by,
    )
    _insert_lineage_row(tenant_id, new_run_id, "agent_dispatch", source_run_id, from_step)

    def _dispatch() -> RerunResult:
        from dbos import DBOS  # lazy — dep-less module import

        from orchestrator.agents.coordinator import agent_dispatch_workflow

        try:
            DBOS.start_workflow(
                agent_dispatch_workflow,
                str(tenant_id),
                row["item_id"],
                row["agent"],
                row["id"],
                str(new_run_id),
            )
        except Exception as exc:
            _close_lineage_row_failed(tenant_id, new_run_id, exc)
            raise
        logger.info(
            "run_control rerun: agent_dispatch re-dispatched tenant=%s work_item=%s from=%s "
            "new_run=%s",
            tenant_id,
            row["id"],
            from_step,
            new_run_id,
        )
        # completed_meta=None: the child workflow owns this row's completion close.
        return _finalize_outcome(
            tenant_id, new_run_id, "agent_dispatch", source_run_id, completed_meta=None
        )

    return _dispatch


def _rerun_auto_discovery(
    tenant_id: UUID,
    source_run_id: UUID,
    from_step: str,
    validated: list[dict[str, Any]],
    requested_by: UUID | str,
) -> Callable[[], RerunResult]:
    """Re-dispatch the discovery engine. Seed is rebuilt from the tenants row; raw
    city is unrecoverable BY DESIGN (VT-317 coarsens it at signup), and the sources
    tolerate a missing city. Re-emit cost ≤ the engine's own $0.018 ceiling (I8 reuse).

    The lineage row is minted and closed IMMEDIATELY after dispatch (A3:
    'completed' + ``final_outcome='dispatched_async'``) — the async child workflow
    has its own (non-pipeline_runs) observability today; a perpetual-'running' row
    would lie. The C1 overlap re-check runs post-dispatch BEFORE that close, so an
    overlap closes the row 'escalated' instead (the dispatch itself is kept — it
    already happened). The seed itself (business_name / whatsapp_number) is
    name-bearing, so the metadata records only the child workflow name (CL-390:
    IDs/enums only)."""
    seed = _seed_from_tenant(tenant_id)
    if seed is None:
        raise RerunRefused("tenant row not found for discovery seed", code=422)
    new_run_id = uuid4()
    _register_overrides(
        tenant_id,
        "auto_discovery",
        validated,
        workflow_id=None,  # entry point carries no settable run identity → next-run pin
        requested_by=requested_by,
    )
    _insert_lineage_row(tenant_id, new_run_id, "auto_discovery", source_run_id, from_step)

    def _dispatch() -> RerunResult:
        from dbos import DBOS

        from orchestrator.onboarding.auto_discovery import auto_discovery_workflow

        try:
            DBOS.start_workflow(auto_discovery_workflow, str(tenant_id), seed)
        except Exception as exc:
            _close_lineage_row_failed(tenant_id, new_run_id, exc)
            raise
        logger.info(
            "run_control rerun: auto_discovery dispatched tenant=%s new_run=%s",
            tenant_id,
            new_run_id,
        )
        return _finalize_outcome(
            tenant_id,
            new_run_id,
            "auto_discovery",
            source_run_id,
            completed_meta={
                "final_outcome": "dispatched_async",
                "child_workflow": "auto_discovery_workflow",
            },
        )

    return _dispatch


def _rerun_plan_generate(
    tenant_id: UUID,
    source_run_id: UUID,
    from_step: str,
    validated: list[dict[str, Any]],
    requested_by: UUID | str,
) -> Callable[[], RerunResult]:
    """Force-regenerate: the explicit force path the contract names — plan_exists is
    deliberately NOT consulted (this wrapper calls the generator/store functions
    directly instead of the workflow), so a rerun mints a NEW version, audit-visible
    in plan history. Refuses rather than minting an ungrounded plan. Synchronous —
    rerun_from owns the run row open/close; the C1 overlap re-check runs after the
    work (the minted version STANDS on overlap — no rollback, per the ruling;
    the escalated close + alert disclose it). Because the internals bypass the
    seam's own pause hold, an active pause refuses at entry (A6). VT-376: the
    pause/grounding gates + lineage insert run under the rerun-slot lock; the LLM
    generate + delivery run in the returned closure, lock-free."""
    from orchestrator.business_plan import generator, store

    _refuse_if_paused(tenant_id, "plan_generate")
    grounding = generator._gather_grounding(tenant_id)
    if not grounding.confirmed_profile or not grounding.bundle:
        raise RerunRefused(
            "no grounded, confirmed profile — an ungrounded plan is never minted",
            code=422,
        )

    new_run_id = uuid4()
    _register_overrides(
        tenant_id, "plan_generate", validated, workflow_id=new_run_id, requested_by=requested_by
    )
    _insert_lineage_row(tenant_id, new_run_id, "plan_generate", source_run_id, from_step)

    def _dispatch() -> RerunResult:
        try:
            result = generator._generate_and_validate(tenant_id, grounding)
            version = store.write_new_version(
                tenant_id,
                summary=result["summary"],
                roadmap=result["roadmap"],
                fact_bundle=grounding.bundle,
                generated_by=generator.GENERATED_BY,
                model_id=result["model_id"],
            )
        except Exception as exc:
            # A2: best-effort close that can NEVER mask the original exception.
            _close_lineage_row_failed(tenant_id, new_run_id, exc)
            raise
        try:
            # Mirrors the workflow: delivery is best-effort; the version is already
            # persisted.
            from orchestrator.business_plan import delivery

            delivery.deliver_plan(tenant_id, version)
        except Exception:  # noqa: BLE001 — best-effort, same posture as the spine workflow
            logger.exception(
                "run_control rerun: plan delivery failed (best-effort) tenant=%s v=%s",
                tenant_id,
                version,
            )
        logger.info(
            "run_control rerun: plan_generate minted v%s tenant=%s new_run=%s",
            version,
            tenant_id,
            new_run_id,
        )
        return _finalize_outcome(
            tenant_id,
            new_run_id,
            "plan_generate",
            source_run_id,
            completed_meta={"final_outcome": "completed", "version": version},
        )

    return _dispatch


def _rerun_plan_deliver(
    tenant_id: UUID,
    source_run_id: UUID,
    from_step: str,
    validated: list[dict[str, Any]],
    requested_by: UUID | str,
) -> Callable[[], RerunResult]:
    """Re-deliver the LATEST plan version — the delivered_parts bitmap makes this
    resumable by design (only unset parts send; reuse-safe per I8). Synchronous —
    the C1 overlap re-check runs after the delivery work, before the close.
    Calls the delivery internals directly, bypassing the seam's own pause hold —
    so an active pause refuses at entry (A6). VT-376: gates + lineage insert run
    under the rerun-slot lock; the delivery burst runs in the closure, lock-free."""
    from orchestrator.business_plan import delivery, store

    _refuse_if_paused(tenant_id, "plan_deliver")
    plan = store.get_active_plan(tenant_id)
    if plan is None:
        raise RerunRefused("no plan version to deliver", code=422)

    new_run_id = uuid4()
    _register_overrides(
        tenant_id, "plan_deliver", validated, workflow_id=new_run_id, requested_by=requested_by
    )
    _insert_lineage_row(tenant_id, new_run_id, "plan_deliver", source_run_id, from_step)

    def _dispatch() -> RerunResult:
        delivery.deliver_plan(tenant_id, plan.version)  # never raises (per-part best-effort)
        logger.info(
            "run_control rerun: plan_deliver v%s tenant=%s new_run=%s",
            plan.version,
            tenant_id,
            new_run_id,
        )
        return _finalize_outcome(
            tenant_id,
            new_run_id,
            "plan_deliver",
            source_run_id,
            completed_meta={"final_outcome": "completed", "version": plan.version},
        )

    return _dispatch


def _rerun_ingestion(
    tenant_id: UUID,
    source_run_id: UUID,
    from_step: str,
    validated: list[dict[str, Any]],
    requested_by: UUID | str,
    trigger_payload: dict[str, Any] | None,
) -> Callable[[], RerunResult]:
    """Re-dispatch one connector pull. The connector identity must ride on the source
    run's trigger_payload (rerun-minted rows and seam-opened ingestion rows write it);
    a cursor-based pull is reuse-safe (I8).

    The lineage row is minted and closed IMMEDIATELY after dispatch (A3:
    'completed' + ``final_outcome='dispatched_async'`` + the child dispatch
    params) — the async child workflow has its own (non-pipeline_runs)
    observability today; a perpetual-'running' row would lie. The C1 overlap
    re-check runs post-dispatch BEFORE that close, so an overlap closes the row
    'escalated' instead (the dispatch itself is kept — it already happened)."""
    connector_id = (trigger_payload or {}).get("connector_id")
    if not connector_id:
        raise RerunRefused(
            "source run carries no connector identity (trigger_payload.connector_id)",
            code=422,
        )
    new_run_id = uuid4()
    _register_overrides(
        tenant_id,
        "ingestion",
        validated,
        workflow_id=None,  # scheduler entry carries no settable run identity → next-run pin
        requested_by=requested_by,
    )
    _insert_lineage_row(
        tenant_id,
        new_run_id,
        "ingestion",
        source_run_id,
        from_step,
        payload={"connector_id": str(connector_id)},
    )

    def _dispatch() -> RerunResult:
        from dbos import DBOS

        from orchestrator.integrations.scheduler import ingest_one_connector

        try:
            DBOS.start_workflow(ingest_one_connector, tenant_id, str(connector_id))
        except Exception as exc:
            _close_lineage_row_failed(tenant_id, new_run_id, exc)
            raise
        logger.info(
            "run_control rerun: ingestion dispatched tenant=%s connector=%s new_run=%s",
            tenant_id,
            connector_id,
            new_run_id,
        )
        return _finalize_outcome(
            tenant_id,
            new_run_id,
            "ingestion",
            source_run_id,
            completed_meta={
                "final_outcome": "dispatched_async",
                "child_workflow": "ingest_one_connector",
                "connector_id": str(connector_id),
            },
        )

    return _dispatch


# --- DB helpers ---------------------------------------------------------------------


def _load_source_run(source_run_id: UUID | str) -> dict[str, Any] | None:
    """Service-pool read: tenant is unknown until the run row resolves it (the intended
    service-role/ops path — pipeline_runs reads here never write)."""
    from orchestrator.graph import get_pool

    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT id, tenant_id, run_type, status, trigger_payload "
            "FROM pipeline_runs WHERE id = %s",
            (str(source_run_id),),
        ).fetchone()
    if row is None:
        return None
    if not isinstance(row, dict):
        row = dict(
            zip(("id", "tenant_id", "run_type", "status", "trigger_payload"), row, strict=True)
        )
    return row


def _seed_from_tenant(tenant_id: UUID) -> dict[str, Any] | None:
    from orchestrator.graph import get_pool

    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT business_name, business_type, whatsapp_number FROM tenants WHERE id = %s",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    if not isinstance(row, dict):
        row = dict(zip(("business_name", "business_type", "whatsapp_number"), row, strict=True))
    # No city: VT-317 discards the raw value at signup (only city_tier survives, which
    # would degrade the GBP query); the discovery sources handle a missing city.
    return {
        "business_name": row["business_name"],
        "business_type": row["business_type"],
        "whatsapp_number": row["whatsapp_number"],
    }


def _register_overrides(
    tenant_id: UUID,
    workflow_kind: str,
    validated: list[dict[str, Any]],
    *,
    workflow_id: UUID | None,
    requested_by: UUID | str,
) -> None:
    """Pre-register the rerun's pins BEFORE dispatch (plan §8.3). Pins + reason are
    redacted at WRITE with the tenant's name registry (§5/F7); step_overrides is
    deny-all RLS → service pool.

    A1 fail-CLOSED: when any override carries pinned_*/reason text, a registry
    build failure REFUSES the rerun (503) — pattern-only redaction is never
    stored (the ops API ``_registry_or_503`` posture, mirrored)."""
    if not validated:
        return
    from psycopg.types.json import Jsonb

    from orchestrator.graph import get_pool

    expires_at = (
        None
        if workflow_id is not None
        else datetime.now(UTC) + timedelta(seconds=_NEXT_RUN_OVERRIDE_TTL_S)
    )
    needs_redaction = any(
        item["pinned_input"] is not None
        or item["pinned_output"] is not None
        or item["reason"]
        for item in validated
    )
    name_registry = _name_registry_for(tenant_id) if needs_redaction else None
    with get_pool().connection() as conn:
        for item in validated:
            pinned_input = _redact(item["pinned_input"], name_registry)
            pinned_output = _redact(item["pinned_output"], name_registry)
            reason = _redact(item["reason"], name_registry)
            if isinstance(reason, str):
                reason = reason[:_REASON_MAX_LEN]
            conn.execute(
                "INSERT INTO step_overrides "
                "(tenant_id, workflow_kind, step_name, workflow_id, pinned_input, "
                " pinned_output, reason, created_by, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(tenant_id),
                    workflow_kind,
                    item["step_name"],
                    str(workflow_id) if workflow_id is not None else None,
                    Jsonb(pinned_input) if pinned_input is not None else None,
                    Jsonb(pinned_output) if pinned_output is not None else None,
                    reason,
                    str(requested_by),
                    expires_at,
                ),
            )


def _name_registry_for(tenant_id: UUID) -> Any:
    """Tenant name registry for write-time redaction — fail CLOSED (A1, plan §5).

    A build failure raises ``RerunRefused`` (503): storing pins/reason with
    pattern-only redaction would let a known customer name through (I7), so the
    rerun refuses instead — the operator retries, or omits the text-bearing
    fields. Mirrors the ops API's ``_registry_or_503``."""
    try:
        from orchestrator.privacy.customer_registry import make_name_registry

        return make_name_registry(str(tenant_id))
    except Exception as exc:  # noqa: BLE001 — ANY registry failure must refuse the write
        logger.error(
            "run_control rerun: name-registry build FAILED tenant=%s exc=%r — "
            "refusing override write (fail closed)",
            tenant_id,
            exc,
        )
        raise RerunRefused(
            "customer-name registry unavailable; refusing to store unredacted "
            "override text — retry, or omit reason/pinned fields",
            code=503,
        ) from exc


def _redact(value: Any, name_registry: Any) -> Any:
    if value is None:
        return None
    from orchestrator.privacy.pii_redactor import redact

    return redact(value, name_registry=name_registry)


def _insert_lineage_row(
    tenant_id: UUID,
    new_run_id: UUID,
    kind: str,
    source_run_id: UUID,
    from_step: str,
    *,
    payload: dict[str, Any] | None = None,
) -> None:
    """Open the rerun's run-of-record with lineage stamped (run_type = workflow_kind).

    ``payload`` is structural identity only (e.g. connector_id) — never message
    content or customer identity (the runner's redaction posture, kept by construction
    here)."""
    from psycopg.types.json import Jsonb

    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs "
            "(id, tenant_id, run_type, status, trigger_payload, rerun_of_run_id, "
            " rerun_from_step) "
            "VALUES (%s, %s, %s, 'running', %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (
                str(new_run_id),
                str(tenant_id),
                kind,
                Jsonb(payload) if payload is not None else None,
                str(source_run_id),
                from_step,
            ),
        )


def _open_approval_now(tenant_id: UUID) -> dict[str, Any] | None:
    """The C1 re-check read — the SAME helper the 409 gate uses
    (``find_open_approval_for_tenant``), so gate and re-check can never disagree
    about what 'open approval' means."""
    from orchestrator.agent.approval_resume import find_open_approval_for_tenant
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        return find_open_approval_for_tenant(conn, tenant_id)


def _finalize_outcome(
    tenant_id: UUID,
    new_run_id: UUID,
    kind: str,
    source_run_id: UUID,
    *,
    completed_meta: dict[str, Any] | None,
) -> RerunResult:
    """Post-arm C1 convergence re-check (Cowork ruling 20260611T234500Z — Option A).

    The arm's work/dispatch is DONE and KEPT (the ruling: no rollback; a minted
    plan version stands). The 409 gate passed with ZERO open approvals, so ANY
    approval open at this re-check armed DURING the rerun's execution window —
    the gate-window TOCTOU overlap. Overlap → close the lineage row
    ``status='escalated'`` (a mig-052 CHECK member; house close pattern with
    ``final_outcome='rerun_overlapped_open_approval'``), emit the structured
    ``run_control_rerun_overlap`` alert (severity error, component run_control —
    the nightly alert pipeline scans pipeline_log), and surface
    ``outcome='escalated_overlap'``. No overlap → the normal close (when
    ``completed_meta`` is provided; agent_dispatch passes None because its child
    workflow owns the completion close) and ``outcome='completed'``.

    A re-check READ failure follows the A2 posture: failed-close + re-raise —
    an unverifiable convergence must never be laundered into 'completed'.
    """
    try:
        approval = _open_approval_now(tenant_id)
    except Exception as exc:
        _close_lineage_row_failed(tenant_id, new_run_id, exc)
        raise
    if approval is not None:
        overlap_meta = dict(completed_meta or {})
        overlap_meta["final_outcome"] = "rerun_overlapped_open_approval"
        _close_lineage_row(tenant_id, new_run_id, meta=overlap_meta, status="escalated")
        _emit_overlap_alert(tenant_id, new_run_id, kind, source_run_id, approval.get("id"))
        logger.error(
            "run_control rerun: OVERLAP — approval armed during rerun execution; run "
            "escalated tenant=%s kind=%s new_run=%s approval=%s",
            tenant_id,
            kind,
            new_run_id,
            approval.get("id"),
        )
        return RerunResult(run_id=new_run_id, outcome="escalated_overlap")
    if completed_meta is not None:
        _close_lineage_row(tenant_id, new_run_id, meta=completed_meta)
    return RerunResult(run_id=new_run_id, outcome="completed")


def _emit_overlap_alert(
    tenant_id: UUID,
    new_run_id: UUID,
    kind: str,
    source_run_id: UUID,
    approval_id: Any,
) -> None:
    """Best-effort structured ``run_control_rerun_overlap`` alert — the nightly
    alert pipeline scans pipeline_log. Mirrors the ``_emit_degraded`` posture
    (run_control/__init__.py): log_event imports lazily (heavy chain) and the
    whole emission is guarded — an alert failure must never alter the escalation
    close. Payload is IDs/enums only (CL-390)."""
    try:
        from orchestrator.observability.log import log_event  # lazy — heavy import chain

        log_event(
            event_type="run_control_rerun_overlap",
            run_id=new_run_id,
            tenant_id=str(tenant_id),
            severity="error",
            component="run_control",
            payload={
                "workflow_kind": kind,
                "source_run_id": str(source_run_id),
                "approval_id": str(approval_id) if approval_id else None,
                "final_outcome": "rerun_overlapped_open_approval",
            },
        )
    except Exception:  # noqa: BLE001 — observability must not break the escalation path
        logger.warning("run_control rerun: overlap-alert emission failed", exc_info=True)


def _close_lineage_row(
    tenant_id: UUID, run_id: UUID, *, meta: dict[str, Any], status: str = "completed"
) -> None:
    """Close a rerun-minted run row — ``status='completed'`` by default.

    ``pipeline_runs_status_check`` has no 'failed' member (migration 052 set);
    the house pattern (coordinator._close_agent_run) is 'completed' + the real
    outcome in ``terminal_state_metadata`` (``final_outcome``: 'completed' /
    'rerun_failed' / 'dispatched_async'). The ONE exception is the C1 overlap
    escalation, which passes ``status='escalated'`` (also a mig-052 CHECK
    member) with ``final_outcome='rerun_overlapped_open_approval'`` — an
    overlapped rerun is a real escalation, not a completion. Metadata is
    IDs/enums only (CL-390)."""
    from psycopg.types.json import Jsonb

    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status = %s, ended_at = now(), "
            "terminal_state_metadata = %s "
            "WHERE id = %s AND tenant_id = %s",
            (status, Jsonb(meta), str(run_id), str(tenant_id)),
        )


def _close_lineage_row_failed(tenant_id: UUID, run_id: UUID, exc: Exception) -> None:
    """Best-effort terminal close for a failed arm (A2) — guarded so a close failure
    can NEVER mask the original exception; callers re-raise the original. The
    exception CLASS name is enum-class metadata (no message text — CL-390)."""
    try:
        _close_lineage_row(
            tenant_id,
            run_id,
            meta={"final_outcome": "rerun_failed", "error_type": type(exc).__name__},
        )
    except Exception:  # noqa: BLE001 — the original exception must propagate, not this one
        logger.exception(
            "run_control rerun: failed-close also failed tenant=%s run=%s", tenant_id, run_id
        )


def _as_uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


__all__ = ["RUN_TYPE_TO_KIND", "RerunRefused", "RerunResult", "rerun_from"]
