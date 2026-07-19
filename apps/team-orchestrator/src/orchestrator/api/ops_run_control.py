"""VT-374 — run-control ops API (plan §7; contract §Ops API): the VT-375/376 panel's write leg.

Six endpoints under ``/api/orchestrator/ops/run-control``, ALL on the Gap-6 auth stack
(:mod:`orchestrator.api.ops_common`): ``X-Internal-Secret`` + ``X-Operator-Jwt`` (exp REQUIRED) +
:func:`require_vtr_action` assignment gate, called EXACTLY ONCE per handler; the returned VERIFIED
id is the only value ever used as attribution. The ops_audit row is written BEFORE the mutation in
the SAME txn (audit-or-nothing), except ``/rerun`` where the attempt-audit commits first because
``rerun_from`` owns its own txns/dispatch (the VT-300 "every attempt audits" posture).

IDOR posture, stated precisely (plan F12 / VT-293/294): row-targeted mutations
(``/cancel-override``, ``/rerun``, ``/override`` WITH ``workflow_id``, ``/timeline``) derive the
tenant FROM THE TARGET ROW server-side — transport auth runs BEFORE the derive so an
unauthenticated caller cannot probe row existence. Tenant-scoped mutations (``/pause``,
``/release``, next-run ``/override``) take ``tenant_id`` from the body and REQUIRE the
operator↔tenant assignment gate.

Write-time redaction (plan §5, I7): ``pinned_input``/``pinned_output``/``reason`` flow through
:func:`orchestrator.privacy.pii_redactor.redact` WITH ``name_registry=make_name_registry(tenant)``
— the FIRST production consumer of the VT-170 registry. Registry-build failure fails CLOSED for
the write: 503, never an unredacted row. ``/pause`` read-back (F9): 200 only after a verifying
re-read observes the committed hold.

Override validation (F6/F14): unknown step / observed tier / pause-only (N3) / non-allowed pinned
keys / pinned_output on a non-``pure_return`` step / gate-manifest module name → 422. Logging
(CL-390): metadata only — kinds, step names, key NAMES, counts; never reason or pinned values.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import APIRouter, Header, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from orchestrator.alerts.pii_scrub import scrub_pii
from orchestrator.api.ops_common import (
    audit,
    require_vtr_action,
    verify_internal_secret,
    verify_operator_jwt,
)
from orchestrator.graph import get_pool
from orchestrator.privacy.customer_registry import make_name_registry
from orchestrator.privacy.pii_redactor import redact
from orchestrator.privacy.vtr import vtr_connection
from orchestrator.run_control import is_paused
from orchestrator.run_control.gate_manifest import GATE_MODULES
from orchestrator.run_control.registry import (
    KIND_RERUN_POLICY,
    REGISTRY,
    RERUNNABLE,
    StepEntry,
)
from orchestrator.run_control.rerun import RUN_TYPE_TO_KIND, RerunRefused, rerun_from

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/orchestrator/ops/run-control")

_VALID_KINDS = frozenset(kind for kind, _ in REGISTRY)
# N3: pause-only controllable boundaries — ANY override write for these is 422 (the API is the
# pinned enforcement point; the registry entry carries allowed_keys=∅ but so do other steps).
_PAUSE_ONLY_STEPS = frozenset({("webhook_inbound", "dispatch_brain")})
# F14 defense-in-depth: a step NAMED like a gate module gets the explicit manifest rejection, not
# a generic unknown-step 422 (gate steps can never BE in REGISTRY — import-time raise).
_GATE_MODULE_BASENAMES = frozenset(m.rsplit(".", 1)[-1] for m in GATE_MODULES)
_NEXT_RUN_EXPIRY_DEFAULT = timedelta(days=7)  # plan §4 F8 default for workflow_id-NULL overrides
_REASON_CAP = 500


# ---------------------------------------------------------------------------
# Bodies (contract §Ops API — exactly as pinned; operator_id everywhere for the
# body==claim equality leg of the Gap-6 gate)
# ---------------------------------------------------------------------------


class PauseBody(BaseModel):
    operator_id: str
    tenant_id: str
    workflow_kind: str
    reason: str = ""


class ReleaseBody(BaseModel):
    operator_id: str
    tenant_id: str
    workflow_kind: str


class OverrideBody(BaseModel):
    operator_id: str
    tenant_id: str
    workflow_kind: str
    step_name: str
    workflow_id: str | None = None  # target pipeline_runs.id; NULL = next-run
    pinned_input: dict[str, Any] | None = None
    pinned_output: dict[str, Any] | None = None
    reason: str = ""
    expires_at: datetime | None = None


class CancelOverrideBody(BaseModel):
    operator_id: str
    override_id: str
    # NOTE: deliberately NO tenant_id — the tenant is DERIVED from the override row server-side so
    # a client cannot pair a foreign override with an assigned tenant (the VT-293/294 IDOR rule).


class RerunBody(BaseModel):
    operator_id: str
    source_run_id: str
    from_step: str
    overrides: list[dict[str, Any]] = []
    # NOTE: deliberately NO tenant_id — derived from the source run row (VT-293/294).


class RedriveTaskBody(BaseModel):
    operator_id: str
    task_id: str
    # NOTE: deliberately NO tenant_id — derived from the manager_tasks row server-side (VT-293/294).


class KillCampaignBody(BaseModel):
    operator_id: str
    campaign_id: str
    reason: str = ""
    # NOTE: deliberately NO tenant_id — derived from the campaigns row server-side (VT-293/294).


class TakeoverBody(BaseModel):
    operator_id: str
    tenant_id: str
    reason: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_uuid(value: str, field: str) -> str:
    """400 on a non-UUID id before it reaches a uuid-typed SQL param (the run-control idiom)."""
    try:
        uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid {field}") from None
    return value


def _require_kind(workflow_kind: str) -> str:
    if workflow_kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown workflow_kind {workflow_kind!r}; one of {sorted(_VALID_KINDS)}",
        )
    return workflow_kind


def _gate(
    *,
    x_internal_secret: str | None,
    x_operator_jwt: str | None,
    body_operator_id: str,
    tenant_id: str,
    deny_action: str,
    deny_target_kind: str = "tenant",
    deny_target_id: str | None = None,
) -> str:
    """Run the shared VTR-action gate on a service-pool cursor (the deny-audit write needs the
    privileged role — app_vtr_role has no ops_audit grant). Returns the VERIFIED operator id."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        return require_vtr_action(
            cur,
            x_internal_secret=x_internal_secret,
            x_operator_jwt=x_operator_jwt,
            body_operator_id=body_operator_id,
            tenant_id=tenant_id,
            deny_action=deny_action,
            deny_target_kind=deny_target_kind,
            deny_target_id=deny_target_id,
        )


def _registry_or_503(tenant_id: str) -> Callable[[str], bool]:
    """Build the tenant's customer-name registry for write-time redaction — VT-170's FIRST
    production consumer. Fail CLOSED (plan §5): a registry-build failure refuses the write with
    503 rather than ever storing unredacted free text; the operator retries, or omits the
    reason/pins to proceed without anything needing redaction."""
    try:
        return make_name_registry(tenant_id)
    except Exception as exc:  # noqa: BLE001 — ANY registry failure must refuse the write
        logger.error(
            "run_control: name-registry build FAILED tenant=%s exc=%r", tenant_id, exc
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "customer-name registry unavailable; refusing to store unredacted text — "
                "retry, or omit reason/pinned fields"
            ),
        ) from exc


def _redact_text(text: str, registry: Callable[[str], bool]) -> str:
    """Free-text hygiene before any DB write: pattern + name-registry redaction, clamp 500."""
    return str(redact(text, name_registry=registry))[:_REASON_CAP]


def _redact_pins(
    pins: dict[str, Any] | None, registry: Callable[[str], bool]
) -> dict[str, Any] | None:
    """Redact pinned jsonb VALUES (keys preserved — they were allowed-keys-validated upstream)."""
    if pins is None:
        return None
    out = redact(pins, name_registry=registry)
    return dict(out) if isinstance(out, dict) else {"_redacted": out}


def _resolve_run_tenant(conn: Any, run_id: str) -> str:
    """Server-side tenant derivation from the run row (VT-293/294 — never a client tenant_id)."""
    row = conn.execute(
        "SELECT tenant_id FROM pipeline_runs WHERE id = %s LIMIT 1", (run_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return str(row["tenant_id"] if isinstance(row, dict) else row[0])


def _resolve_task_tenant(conn: Any, task_id: str) -> str:
    """Server-side tenant derivation from the manager_tasks row (VT-293/294 — never a client id)."""
    row = conn.execute(
        "SELECT tenant_id FROM manager_tasks WHERE id = %s LIMIT 1", (task_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return str(row["tenant_id"] if isinstance(row, dict) else row[0])


def _resolve_campaign_tenant(conn: Any, campaign_id: str) -> str:
    """Server-side tenant derivation from the campaigns row (VT-293/294 — never a client id)."""
    row = conn.execute(
        "SELECT tenant_id FROM campaigns WHERE id = %s LIMIT 1", (campaign_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return str(row["tenant_id"] if isinstance(row, dict) else row[0])


def _as_utc(value: datetime) -> datetime:
    """Normalize a pydantic-parsed datetime: a naive value is taken as UTC (never local)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ---------------------------------------------------------------------------
# /pause + /release — tenant-scoped (tenant from body + assignment gate)
# ---------------------------------------------------------------------------


@router.post("/pause")
def pause(
    body: PauseBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """Set the (tenant, workflow_kind) hold. 409 if already paused. 200 ONLY after a verifying
    read-back observes the committed hold (F9) — a pause the executor cannot see is a failure,
    not a success."""
    _require_uuid(body.tenant_id, "tenant_id")
    _require_kind(body.workflow_kind)
    operator = _gate(
        x_internal_secret=x_internal_secret,
        x_operator_jwt=x_operator_jwt,
        body_operator_id=body.operator_id,
        tenant_id=body.tenant_id,
        deny_action="workflow_pause_denied",
        deny_target_kind="workflow_control",
        deny_target_id=f"{body.tenant_id}:{body.workflow_kind}",
    )
    reason: str | None = None
    if body.reason:
        reason = _redact_text(body.reason, _registry_or_503(body.tenant_id))

    pool = get_pool()
    with pool.connection() as conn:
        with conn.transaction(), conn.cursor() as cur:
            # Audit BEFORE the mutation, same txn — a hold without its audit row is impossible;
            # the 409 path below rolls BOTH back (no mutation, no executed-audit).
            audit(
                cur,
                operator_id=operator,
                tenant_id=body.tenant_id,
                action="workflow_pause",
                target_kind="workflow_control",
                target_id=f"{body.tenant_id}:{body.workflow_kind}",
                detail=f"kind={body.workflow_kind} reason_len={len(reason or '')}",
            )
            cur.execute(
                "INSERT INTO workflow_controls (tenant_id, workflow_kind, set_by, reason) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, workflow_kind) WHERE released_at IS NULL DO NOTHING "
                "RETURNING id",
                (body.tenant_id, body.workflow_kind, operator, reason),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=409, detail="already paused")
            control_id = str(row["id"] if isinstance(row, dict) else row[0])

    # F9 verifying read-back: fresh read AFTER commit, through the executor's own predicate.
    try:
        verified = is_paused(body.tenant_id, body.workflow_kind)
    except Exception as exc:
        logger.error(
            "pause read-back ERRORED tenant=%s kind=%s exc=%r",
            body.tenant_id, body.workflow_kind, exc,
        )
        raise HTTPException(
            status_code=500,
            detail="pause stored but the verifying read-back errored (F9); state unconfirmed",
        ) from exc
    if not verified:
        raise HTTPException(
            status_code=500,
            detail="pause stored but the verifying read-back did not observe it (F9)",
        )
    logger.info(
        "pause OK operator=%s tenant=%s kind=%s control=%s",
        operator, body.tenant_id, body.workflow_kind, control_id,
    )
    return {
        "ok": True,
        "control_id": control_id,
        "tenant_id": body.tenant_id,
        "workflow_kind": body.workflow_kind,
    }


@router.post("/release")
def release(
    body: ReleaseBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """Release the active (tenant, workflow_kind) hold. 404 when none is active."""
    _require_uuid(body.tenant_id, "tenant_id")
    _require_kind(body.workflow_kind)
    operator = _gate(
        x_internal_secret=x_internal_secret,
        x_operator_jwt=x_operator_jwt,
        body_operator_id=body.operator_id,
        tenant_id=body.tenant_id,
        deny_action="workflow_release_denied",
        deny_target_kind="workflow_control",
        deny_target_id=f"{body.tenant_id}:{body.workflow_kind}",
    )
    pool = get_pool()
    with pool.connection() as conn:
        with conn.transaction(), conn.cursor() as cur:
            audit(
                cur,
                operator_id=operator,
                tenant_id=body.tenant_id,
                action="workflow_release",
                target_kind="workflow_control",
                target_id=f"{body.tenant_id}:{body.workflow_kind}",
                detail=f"kind={body.workflow_kind}",
            )
            cur.execute(
                "UPDATE workflow_controls SET released_at = now(), released_by = %s "
                "WHERE tenant_id = %s AND workflow_kind = %s AND released_at IS NULL "
                "RETURNING id",
                (operator, body.tenant_id, body.workflow_kind),
            )
            row = cur.fetchone()
            if row is None:
                # Rolls the audit row back too — no mutation, no executed-audit.
                raise HTTPException(status_code=404, detail="no active pause")
            control_id = str(row["id"] if isinstance(row, dict) else row[0])
    logger.info(
        "release OK operator=%s tenant=%s kind=%s control=%s",
        operator, body.tenant_id, body.workflow_kind, control_id,
    )
    return {
        "ok": True,
        "control_id": control_id,
        "tenant_id": body.tenant_id,
        "workflow_kind": body.workflow_kind,
    }


# ---------------------------------------------------------------------------
# /override + /cancel-override
# ---------------------------------------------------------------------------


def _validate_override_step(workflow_kind: str, step_name: str, body: OverrideBody) -> Any:
    """All the 422 legs (F6/F14/N3) against the pinned registry. Returns the StepEntry."""
    if step_name in _GATE_MODULE_BASENAMES:
        raise HTTPException(
            status_code=422,
            detail=f"step {step_name!r} is a gate-manifest surface; structurally "
            "non-overridable (I2/F14)",
        )
    entry = REGISTRY.get((workflow_kind, step_name))
    if entry is None:
        raise HTTPException(
            status_code=422, detail=f"unknown step ({workflow_kind}, {step_name})"
        )
    if (workflow_kind, step_name) in _PAUSE_ONLY_STEPS:
        raise HTTPException(
            status_code=422,
            detail=f"step {step_name!r} is a pause-only boundary (N3); overrides never match it",
        )
    if entry.tier != "controllable":
        raise HTTPException(
            status_code=422,
            detail=f"step {step_name!r} is {entry.tier}-tier — timeline display only, "
            "not controllable",
        )
    if body.pinned_input is None and body.pinned_output is None:
        raise HTTPException(
            status_code=422, detail="nothing pinned: pinned_input or pinned_output required"
        )
    if body.pinned_input is not None:
        if not body.pinned_input:
            raise HTTPException(status_code=422, detail="pinned_input must be non-empty")
        bad = set(body.pinned_input) - set(entry.allowed_keys)
        if bad:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"pinned_input keys not allow-listed for {step_name!r}: {sorted(bad)}; "
                    f"allowed: {sorted(entry.allowed_keys)} (I7 — customer-visible/identity "
                    "keys are never allow-listed)"
                ),
            )
    if body.pinned_output is not None and not entry.pure_return:
        raise HTTPException(
            status_code=422,
            detail=f"pinned_output is legal only for pure_return steps; {step_name!r} has "
            "DB-mediated effects (F6 scenario A)",
        )
    return entry


@router.post("/override")
def override(
    body: OverrideBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """Pre-register a one-shot step pin. WITH ``workflow_id`` = row-targeted (tenant derived from
    the run row); WITHOUT = next-run, tenant-scoped, ``expires_at`` REQUIRED (default 7d, F8)."""
    _require_uuid(body.tenant_id, "tenant_id")
    _require_kind(body.workflow_kind)
    # Transport auth BEFORE any row derive so an unauthenticated caller cannot probe run ids.
    verify_internal_secret(x_internal_secret)
    verify_operator_jwt(x_operator_jwt)
    _validate_override_step(body.workflow_kind, body.step_name, body)

    expires_at: datetime | None = _as_utc(body.expires_at) if body.expires_at else None
    if body.workflow_id is None:
        if expires_at is None:
            expires_at = datetime.now(timezone.utc) + _NEXT_RUN_EXPIRY_DEFAULT
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=422, detail="expires_at must be in the future")
    else:
        _require_uuid(body.workflow_id, "workflow_id")

    pool = get_pool()
    with pool.connection() as conn:
        tenant_id = (
            _resolve_run_tenant(conn, body.workflow_id)
            if body.workflow_id is not None
            else body.tenant_id
        )
        with conn.cursor() as cur:
            # Gate on the DERIVED tenant (row-targeted) / body tenant (next-run) — F12.
            operator = require_vtr_action(
                cur,
                x_internal_secret=x_internal_secret,
                x_operator_jwt=x_operator_jwt,
                body_operator_id=body.operator_id,
                tenant_id=tenant_id,
                deny_action="step_override_denied",
                deny_target_kind="step",
                deny_target_id=f"{body.workflow_kind}:{body.step_name}",
            )
        if body.workflow_id is not None and tenant_id != body.tenant_id:
            # Surfaced only AFTER the gate (an unassigned caller never learns run→tenant
            # pairings); the derived tenant governed the gate regardless (VT-293/294).
            raise HTTPException(status_code=422, detail="tenant_id does not match the target run")

    # Write-time redaction (plan §5, I7) — registry fail-closed (503), never an unredacted row.
    registry = _registry_or_503(tenant_id)
    pinned_input = _redact_pins(body.pinned_input, registry)
    pinned_output = _redact_pins(body.pinned_output, registry)
    reason = _redact_text(body.reason, registry) if body.reason else None

    override_id = str(uuid.uuid4())  # minted app-side so the audit row (written FIRST) names it
    with pool.connection() as conn:
        with conn.transaction(), conn.cursor() as cur:
            keys = sorted(set(body.pinned_input or {}) | set(body.pinned_output or {}))
            audit(
                cur,
                operator_id=operator,
                tenant_id=tenant_id,
                action="step_override",
                target_kind="step_override",
                target_id=override_id,
                detail=(
                    f"kind={body.workflow_kind} step={body.step_name} "
                    f"workflow_id={body.workflow_id or 'next-run'} keys={keys}"
                ),
            )
            cur.execute(
                "INSERT INTO step_overrides (id, tenant_id, workflow_kind, step_name, "
                "workflow_id, pinned_input, pinned_output, reason, created_by, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    override_id,
                    tenant_id,
                    body.workflow_kind,
                    body.step_name,
                    body.workflow_id,
                    Jsonb(pinned_input) if pinned_input is not None else None,
                    Jsonb(pinned_output) if pinned_output is not None else None,
                    reason,
                    operator,
                    expires_at,
                ),
            )
    logger.info(
        "override OK operator=%s tenant=%s kind=%s step=%s id=%s workflow_id=%s",
        operator, tenant_id, body.workflow_kind, body.step_name, override_id,
        body.workflow_id or "next-run",
    )
    return {
        "ok": True,
        "override_id": override_id,
        "tenant_id": tenant_id,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


@router.post("/cancel-override")
def cancel_override(
    body: CancelOverrideBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """Cancel ONE unconsumed override. Tenant DERIVED from the override row (VT-293/294);
    transport auth verified BEFORE the derive so existence is not probeable."""
    _require_uuid(body.override_id, "override_id")
    verify_internal_secret(x_internal_secret)
    verify_operator_jwt(x_operator_jwt)
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT tenant_id, workflow_kind, step_name, consumed_at, cancelled_at "
            "FROM step_overrides WHERE id = %s LIMIT 1",
            (body.override_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="override not found")
        g = (
            dict(row)
            if isinstance(row, dict)
            else dict(zip(
                ("tenant_id", "workflow_kind", "step_name", "consumed_at", "cancelled_at"), row
            ))
        )
        tenant_id = str(g["tenant_id"])
        with conn.cursor() as cur:
            operator = require_vtr_action(
                cur,
                x_internal_secret=x_internal_secret,
                x_operator_jwt=x_operator_jwt,
                body_operator_id=body.operator_id,
                tenant_id=tenant_id,
                deny_action="step_override_cancel_denied",
                deny_target_kind="step_override",
                deny_target_id=body.override_id,
            )
        if g["consumed_at"] is not None:
            raise HTTPException(status_code=409, detail="override already consumed")
        if g["cancelled_at"] is not None:
            raise HTTPException(status_code=409, detail="override already cancelled")
        with conn.transaction(), conn.cursor() as cur:
            audit(
                cur,
                operator_id=operator,
                tenant_id=tenant_id,
                action="step_override_cancel",
                target_kind="step_override",
                target_id=body.override_id,
                detail=f"kind={g['workflow_kind']} step={g['step_name']}",
            )
            cur.execute(
                "UPDATE step_overrides SET cancelled_at = now() "
                "WHERE id = %s AND consumed_at IS NULL AND cancelled_at IS NULL RETURNING id",
                (body.override_id,),
            )
            if cur.fetchone() is None:
                # Lost the race with a consuming run — rolls the audit row back too.
                raise HTTPException(status_code=409, detail="override consumed concurrently")
    logger.info(
        "cancel_override OK operator=%s tenant=%s id=%s", operator, tenant_id, body.override_id
    )
    return {"ok": True, "override_id": body.override_id, "tenant_id": tenant_id}


# ---------------------------------------------------------------------------
# /rerun — row-targeted app-level re-dispatch (plan §8)
# ---------------------------------------------------------------------------


@router.post("/rerun")
def rerun(
    body: RerunBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """App-level re-dispatch from a step (plan §8 — re-dispatch, NOT time-travel). Tenant DERIVED
    from the source run row. :class:`RerunRefused` (non-rerunnable kind / open approval F10 /
    unknown step) maps to its carried status (default 409). The attempt-audit commits BEFORE the
    dispatch — ``rerun_from`` owns its own txns, so atomic audit-with-mutation is not expressible
    here; an audited-but-refused attempt is the intended trace (the VT-300 posture).

    Response carries ``outcome`` ('completed' | 'escalated_overlap') — VT-375 C1 (Cowork ruling
    20260611T234500Z, Option A). HTTP stays **200 for 'escalated_overlap'** deliberately: the
    rerun DID run (the dispatch happened / the version mint stands — no rollback, per the
    ruling); what overlapped is an owner approval that armed mid-flight, and that is disclosed —
    the run row closes 'escalated', a ``run_control_rerun_overlap`` alert lands in pipeline_log,
    and the panel's disclosure copy explains it. A non-2xx here would falsely tell the operator
    nothing happened."""
    _require_uuid(body.source_run_id, "source_run_id")
    verify_internal_secret(x_internal_secret)
    verify_operator_jwt(x_operator_jwt)
    pool = get_pool()
    with pool.connection() as conn:
        tenant_id = _resolve_run_tenant(conn, body.source_run_id)
        with conn.cursor() as cur:
            operator = require_vtr_action(
                cur,
                x_internal_secret=x_internal_secret,
                x_operator_jwt=x_operator_jwt,
                body_operator_id=body.operator_id,
                tenant_id=tenant_id,
                deny_action="run_rerun_denied",
                deny_target_kind="run",
                deny_target_id=body.source_run_id,
            )

    # Defense-in-depth write-time redaction of override-spec free text/pins BEFORE they reach
    # rerun_from's pre-registration (redact is idempotent — double-redaction is a no-op).
    overrides = body.overrides
    if any(k in spec for spec in overrides for k in ("pinned_input", "pinned_output", "reason")):
        registry = _registry_or_503(tenant_id)
        redacted: list[dict[str, Any]] = []
        for spec in overrides:
            spec = dict(spec)
            for key in ("pinned_input", "pinned_output"):
                pin = spec.get(key)
                if pin is None:
                    continue
                # C4: a present-but-non-object pin must NOT silently skip redaction — a string or
                # list could carry a customer name straight past the registry. Refuse it (422).
                if not isinstance(pin, dict):
                    raise HTTPException(
                        status_code=422,
                        detail=f"{key} must be a JSON object when present (got {type(pin).__name__})",
                    )
                spec[key] = _redact_pins(pin, registry)
            if isinstance(spec.get("reason"), str) and spec["reason"]:
                spec["reason"] = _redact_text(spec["reason"], registry)
            redacted.append(spec)
        overrides = redacted

    with pool.connection() as conn, conn.cursor() as cur:
        audit(
            cur,
            operator_id=operator,
            tenant_id=tenant_id,
            action="run_rerun",
            target_kind="run",
            target_id=body.source_run_id,
            detail=f"from_step={body.from_step} overrides={len(overrides)}",
        )

    try:
        result = rerun_from(
            body.source_run_id, body.from_step, overrides, requested_by=operator
        )
    except RerunRefused as exc:
        # RerunRefused carries .code (rerun.py: 409 default / 503 fail-closed / 409 open-approval);
        # read the right attribute — 'status_code' never existed on it, so the old getattr always
        # fell back to 409 and swallowed the 503/422 distinction (C3).
        status = int(getattr(exc, "code", 409))
        # Refusal text echoes kinds/steps/counts; scrub anyway before it leaves (CL-390).
        raise HTTPException(status_code=status, detail=scrub_pii(str(exc))) from exc
    logger.info(
        "rerun OK operator=%s tenant=%s source=%s from_step=%s new_run=%s outcome=%s",
        operator, tenant_id, body.source_run_id, body.from_step, result.run_id, result.outcome,
    )
    return {
        "ok": True,
        "new_run_id": str(result.run_id),
        # C1/Option A: 'completed' | 'escalated_overlap' — 200 either way (see docstring).
        "outcome": result.outcome,
        "source_run_id": body.source_run_id,
        "tenant_id": tenant_id,
    }


# ---------------------------------------------------------------------------
# /timeline/{run_id} — read-only (no audit; mirrors the Gap-6 read endpoints)
# ---------------------------------------------------------------------------

# run_control_intervention rows are the companion observability records ABOUT a seam —
# never the controllable seam itself — and they carry a colon-namespaced step_name
# ('<workflow_kind>:<step_name>'). They are ALWAYS observed, even though the part after
# the colon matches a controllable registry step. Exclude them explicitly.
_INTERVENTION_STEP_KIND = "run_control_intervention"

# VT-376: the per-kind why-copy the panel renders on NON-rerunnable kinds (B2 shows it
# verbatim next to the absent rerun button). Pinned to the KIND_RERUN_POLICY 'forbidden'
# set; the import-time check below keeps the two from drifting apart.
_FORBIDDEN_RERUN_WHY = {
    "webhook_inbound": "message-dedup semantics",
    "trial_sweep": "duplicate-nudge risk",
    "campaign_send": "kg-duplication",
}
if set(_FORBIDDEN_RERUN_WHY) != {k for k, v in KIND_RERUN_POLICY.items() if v == "forbidden"}:
    raise RuntimeError(
        "run_control API: _FORBIDDEN_RERUN_WHY must cover exactly the rerun-forbidden kinds"
    )


def _controllable_entry(run_type: Any, step_kind: Any, step_name: Any) -> StepEntry | None:
    """The REGISTRY entry iff the step is a REGISTERED controllable seam, else None —
    the substrate of the per-step 'tier' + 'allowed_keys' response annotations (pure
    annotation; NO view/RLS change — derived from the registry at read time).

    Controllable requires: (a) the step is NOT a run_control_intervention companion row,
    (b) the run_type maps to a known workflow_kind (RUN_TYPE_TO_KIND), and (c) the step_name's
    registry part resolves to a REGISTRY entry whose tier is 'controllable'. Everything else —
    unregistered kinds, brain micro-steps (langgraph node names), observed-tier registry
    entries, intervention rows — is None ⇒ 'observed' (the honest 'not controllable' label,
    plan §1) with allowed_keys=[]."""
    if step_kind == _INTERVENTION_STEP_KIND:
        return None
    kind = RUN_TYPE_TO_KIND.get(run_type or "")
    if not kind or not step_name:
        return None
    # The registry step part — bare step_name for real rows; the post-colon part defensively
    # (a non-intervention row should not be namespaced, but never index past a stray colon).
    registry_step = str(step_name).rsplit(":", 1)[-1]
    entry = REGISTRY.get((kind, registry_step))
    return entry if entry is not None and entry.tier == "controllable" else None


@router.get("/timeline/{run_id}")
def timeline(
    run_id: str,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """Step timeline for one run, read as ``app_vtr_role`` through the mig-131/132 views
    ONLY (keys-only / explicit-projection envelopes by construction — the view is the PII
    boundary, plan §6). Active workflow_controls holds ride along so the panel never shows
    "not paused" during a hold. GET carries no body: the JWT claim id itself feeds the
    gate's equality leg.

    VT-376 panel annotations (all derived at read time, no view change):
      * per-step ``tier`` ('controllable' | 'observed') + ``allowed_keys`` — registry key
        NAMES for controllable steps (I7: config/ID-class names, safe to show; the values
        the VTR pins are blind-written), [] otherwise so the override dialog can never
        render a field for a non-controllable row;
      * run-level ``rerunnable`` (workflow_kind in RERUNNABLE) + ``forbidden_reason`` —
        the pinned why-copy for the rerun-forbidden kinds, None when rerunnable (or when
        the run_type maps to no known kind)."""
    _require_uuid(run_id, "run_id")
    verify_internal_secret(x_internal_secret)
    claim = verify_operator_jwt(x_operator_jwt)
    claim_operator = str(claim["operator_id"])
    pool = get_pool()
    # Tenant derived from the run row (VT-293/294); run_type rides along for the
    # run-level rerunnable annotation. Transport auth ran BEFORE this derive.
    with pool.connection() as conn:
        run_row = conn.execute(
            "SELECT tenant_id, run_type FROM pipeline_runs WHERE id = %s LIMIT 1", (run_id,)
        ).fetchone()
    if run_row is None:
        raise HTTPException(status_code=404, detail="run not found")
    if not isinstance(run_row, dict):
        run_row = dict(zip(("tenant_id", "run_type"), run_row))
    tenant_id = str(run_row["tenant_id"])
    workflow_kind = RUN_TYPE_TO_KIND.get(run_row["run_type"] or "")
    operator = _gate(
        x_internal_secret=x_internal_secret,
        x_operator_jwt=x_operator_jwt,
        body_operator_id=claim_operator,
        tenant_id=tenant_id,
        deny_action="timeline_read_denied",
        deny_target_kind="run",
        deny_target_id=run_id,
    )
    # VT-377 (mig-134): the VERIFIED operator scopes the views themselves (defense in depth
    # behind the gate above; FAZAL break-glass rides the admin role inside vtr_connection).
    with vtr_connection(operator_id=operator) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM vtr_step_timeline WHERE run_id = %s ORDER BY step_seq",
            (run_id,),
        )
        steps = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT tenant_id, workflow_kind, set_at, released_at FROM vtr_workflow_controls "
            "WHERE tenant_id = %s AND released_at IS NULL",
            (tenant_id,),
        )
        controls = [dict(r) for r in cur.fetchall()]
    # Pure response annotation (no view/RLS change): tag each step controllable | observed
    # + its registry allowed-key NAMES so the panel can badge observed steps and build the
    # override form without a second round-trip (VT-376).
    for s in steps:
        entry = _controllable_entry(s.get("run_type"), s.get("step_kind"), s.get("step_name"))
        s["tier"] = "controllable" if entry is not None else "observed"
        s["allowed_keys"] = sorted(entry.allowed_keys) if entry is not None else []
    rerunnable = workflow_kind in RERUNNABLE
    # VT-376 pre-flight: the rerun dialog needs the tenant's open-approval STATE — a
    # boolean ONLY, never the approval row (CL-390/content-leak constraint). Same helper
    # the /rerun 409 gate uses; server 409 remains the authority, this is dialog sugar.
    # Fail-safe TRUE on read error: the dialog warns rather than green-lighting blind.
    try:
        from orchestrator.agent.approval_resume import find_open_approval_for_tenant
        from orchestrator.db import tenant_connection

        with tenant_connection(uuid.UUID(str(tenant_id))) as approval_conn:
            open_approval = find_open_approval_for_tenant(approval_conn, tenant_id) is not None
    except Exception:  # noqa: BLE001 — pre-flight read failure must not break the timeline
        open_approval = True
    logger.info(
        "timeline OK operator=%s tenant=%s run=%s steps=%d active_controls=%d rerunnable=%s",
        operator, tenant_id, run_id, len(steps), len(controls), rerunnable,
    )
    return {
        "run_id": run_id,
        "tenant_id": tenant_id,
        # VT-376 run-level rerun annotation: the button renders ONLY where rerunnable;
        # forbidden kinds carry the why-copy (None for rerunnable / unmapped run types).
        "rerunnable": rerunnable,
        "forbidden_reason": None if rerunnable else _FORBIDDEN_RERUN_WHY.get(workflow_kind),
        "open_approval": open_approval,
        "steps": steps,
        "active_controls": controls,
    }


# ---------------------------------------------------------------------------
# /programs/{tenant_id} — read-only programs projection (VT-375 Phase B, B1)
# ---------------------------------------------------------------------------
#
# The panel's tenant-tile → program-tile read surface. Past = terminal pipeline_runs
# (+ rerun lineage); running = non-terminal runs (active_hold flagged from the live
# workflow_controls read); upcoming_7d = COMPUTED forecast, NO new state (trial sweep
# dates, queued agent work, roadmap month windows). Same Gap-6 read posture as
# /timeline: internal secret + operator JWT (exp required) + the assignment gate
# (tenant from the PATH, like /pause's tenant-scoped gate). pipeline_runs reads on the
# service pool (matching /timeline); the holds read goes through the app_vtr_role view
# (vtr_workflow_controls) so the panel never shows raw control rows.
#
# ``degraded``: true when the workflow_controls (holds) read raises — a fail-OPEN read
# path (the projection still returns past/running/upcoming; the panel surfaces the
# pause-state-unverifiable copy). Hold rows NEVER carry reason text (the view excludes
# it by construction; we re-pin the column allowlist here defensively).

_PROGRAMS_PAST_LIMIT = 50  # newest terminal runs per tenant (contract pin)
_UPCOMING_WINDOW = timedelta(days=7)
# Terminal = NOT 'running' (the pipeline_runs status CHECK list; migration 052).
_RUNNING_STATUS = "running"
# vtr_workflow_controls is exactly these 4 structural columns (mig-131) — re-pinned so a
# view edit that widened the projection never leaks reason/operator ids through here.
_HOLD_COLUMNS = ("tenant_id", "workflow_kind", "set_at", "released_at")


def _row_to_dict(row: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return dict(zip(columns, row, strict=True))


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


_RUN_COLUMNS = (
    "id",
    "run_type",
    "status",
    "started_at",
    "ended_at",
    "rerun_of_run_id",
    "rerun_from_step",
    "step_count",
)


def _run_projection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(row["id"]),
        "run_type": row["run_type"],
        "status": row["status"],
        "started_at": _iso(row["started_at"]),
        "ended_at": _iso(row["ended_at"]),
        "rerun_of_run_id": str(row["rerun_of_run_id"]) if row["rerun_of_run_id"] else None,
        "rerun_from_step": row["rerun_from_step"],
        "step_count": row["step_count"],
    }


def _roadmap_agent(item: dict[str, Any]) -> str:
    """The roadmap item's owning_agent CLOSED enum (schema.py OWNING_AGENTS), or 'unassigned'.

    Defense-in-depth against an unexpected/free-text value sneaking onto this VTR label: only a
    value already in the closed enum is surfaced; anything else degrades to 'unassigned'. The
    import is lazy so this dep-light API module never pulls the business_plan store at import."""
    from orchestrator.business_plan.store import OWNING_AGENTS

    agent = item.get("owning_agent")
    return agent if isinstance(agent, str) and agent in OWNING_AGENTS else "unassigned"


def _compute_upcoming_7d(conn: Any, tenant_id: str, now: datetime) -> list[dict[str, Any]]:
    """COMPUTED forecast — NO new state. Three sources, each derived from rows that
    already exist (trial start + config, queued work items, the latest plan roadmap)."""
    horizon = now + _UPCOMING_WINDOW
    upcoming: list[dict[str, Any]] = []

    # --- trial sweep dates: tenants.trial_started_at + config/trial.yaml -------------
    # Only an active, un-subscribed trial forecasts a sweep (mirrors trial_evaluator's
    # in-scope predicate). warn = trial_end - warn_lead; expiry = trial_end.
    trow = conn.execute(
        "SELECT phase, trial_started_at, paid_conversion_at FROM tenants WHERE id = %s",
        (tenant_id,),
    ).fetchone()
    t = _row_to_dict(trow, ("phase", "trial_started_at", "paid_conversion_at")) if trow else None
    if (
        t is not None
        and t["phase"] == "trial"
        and t["paid_conversion_at"] is None
        and t["trial_started_at"] is not None
    ):
        from orchestrator.billing import trial_evaluator

        cfg = trial_evaluator._config()
        trial_end = t["trial_started_at"] + timedelta(days=int(cfg["trial_days"]))
        warn_at = trial_end - timedelta(days=int(cfg["warn_lead_days"]))
        for label, due in (("trial warning", warn_at), ("trial expiry", trial_end)):
            if now <= due < horizon:
                upcoming.append(
                    {
                        "kind": "trial_sweep",
                        "due_at": due.isoformat(),
                        "label": label,
                        "source": "trial.yaml forecast",
                    }
                )

    # --- queued agent work: non-terminal pre-run agent_work_items -------------------
    # 'dispatched' is the queued-not-yet-executed status the coordinator mints; the next
    # coordinator sweep (AGENT_COORDINATOR_CRON, daily 10:00 UTC) is the forecast due_at.
    from orchestrator.agents.coordinator import AGENT_COORDINATOR_CRON

    qrow = conn.execute(
        "SELECT count(*) FROM agent_work_items "
        "WHERE tenant_id = %s AND status = 'dispatched'",
        (tenant_id,),
    ).fetchone()
    queued = int(qrow[0] if not isinstance(qrow, dict) else qrow["count"])
    if queued:
        upcoming.append(
            {
                "kind": "agent_dispatch",
                "due_at": _next_cron_after(AGENT_COORDINATOR_CRON, now).isoformat(),
                "label": f"next sweep ({queued} queued)",
                "source": "agent_work_items",
            }
        )

    # --- roadmap: latest plan's month windows starting within 7d --------------------
    # month N window starts at plan.created_at + (N-1) months; the plan is the latest
    # business_plan version. Read service-pool style via the same conn (service role).
    prow = conn.execute(
        "SELECT version, roadmap_json, created_at FROM business_plan "
        "WHERE tenant_id = %s ORDER BY version DESC LIMIT 1",
        (tenant_id,),
    ).fetchone()
    if prow is not None:
        plan = _row_to_dict(prow, ("version", "roadmap_json", "created_at"))
        created_at = plan["created_at"]
        for item in plan["roadmap_json"] or []:
            month = item.get("month")
            if not isinstance(month, int) or month < 1:
                continue
            window_start = created_at + timedelta(days=(month - 1) * 30)
            if now <= window_start < horizon:
                upcoming.append(
                    {
                        "kind": "roadmap",
                        "due_at": window_start.isoformat(),
                        # STRUCTURAL ONLY — the month number + the owning_agent CLOSED enum.
                        # item['objective'] is LLM-authored free text from the service-pool plan
                        # read; surfacing it on this VTR projection is a PII-boundary violation
                        # (a roadmap objective can name the owner's business / a customer). The
                        # owning_agent is validated against OWNING_AGENTS at plan-write (schema.py);
                        # an out-of-enum value degrades to a neutral token, never raw free text.
                        "label": f"month {month} window opens ({_roadmap_agent(item)})",
                        "source": f"business_plan v{plan['version']}",
                    }
                )

    upcoming.sort(key=lambda u: u["due_at"])
    return upcoming


def _next_cron_after(cron: str, now: datetime) -> datetime:
    """Next fire of a ``M H * * *`` daily cron strictly after ``now`` (the only cron shape
    the coordinator uses). Falls back to now+1d on any non-daily shape — the forecast is a
    best-effort due_at label, never a scheduler."""
    try:
        minute, hour = cron.split()[0], cron.split()[1]
        m, h = int(minute), int(hour)
    except (ValueError, IndexError):
        return now + timedelta(days=1)
    candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


@router.get("/programs/{tenant_id}")
def programs(
    tenant_id: str,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """Programs projection for one tenant (VT-375 Phase B read surface). Past / running /
    upcoming_7d / holds + a ``degraded`` flag. Read-only — no audit, no mutation (mirrors
    the Gap-6 read endpoints). Tenant comes from the PATH; the assignment gate runs on it
    (tenant-scoped, like /pause). GET carries no body — the JWT claim id feeds the gate's
    equality leg, same as /timeline."""
    _require_uuid(tenant_id, "tenant_id")
    verify_internal_secret(x_internal_secret)
    claim = verify_operator_jwt(x_operator_jwt)
    claim_operator = str(claim["operator_id"])
    operator = _gate(
        x_internal_secret=x_internal_secret,
        x_operator_jwt=x_operator_jwt,
        body_operator_id=claim_operator,
        tenant_id=tenant_id,
        deny_action="programs_read_denied",
    )

    now = datetime.now(timezone.utc)
    pool = get_pool()
    # pipeline_runs + the computed forecast read on the service pool (matches /timeline's
    # pipeline_runs posture; these are run-row/forecast reads, not view reads).
    with pool.connection() as conn:
        past_rows = conn.execute(
            "SELECT id, run_type, status, started_at, ended_at, rerun_of_run_id, "
            "rerun_from_step, step_count FROM pipeline_runs "
            "WHERE tenant_id = %s AND status <> %s "
            "ORDER BY started_at DESC LIMIT %s",
            (tenant_id, _RUNNING_STATUS, _PROGRAMS_PAST_LIMIT),
        ).fetchall()
        running_rows = conn.execute(
            "SELECT id, run_type, status, started_at, ended_at, rerun_of_run_id, "
            "rerun_from_step, step_count FROM pipeline_runs "
            "WHERE tenant_id = %s AND status = %s ORDER BY started_at DESC",
            (tenant_id, _RUNNING_STATUS),
        ).fetchall()
        upcoming_7d = _compute_upcoming_7d(conn, tenant_id, now)

    past = [_run_projection(_row_to_dict(r, _RUN_COLUMNS)) for r in past_rows]

    # Holds read through the app_vtr_role view (the PII boundary). A read error is the ONLY
    # degraded trigger (fail-open): the projection still returns; the panel shows the
    # pause-state-unverifiable copy. active_hold on running runs is derived from the held kinds.
    holds: list[dict[str, Any]] = []
    degraded = False
    try:
        # VT-377 (mig-134): operator-scoped view read (same posture as /timeline).
        with vtr_connection(operator_id=operator) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, workflow_kind, set_at, released_at FROM vtr_workflow_controls "
                "WHERE tenant_id = %s AND released_at IS NULL",
                (tenant_id,),
            )
            for r in cur.fetchall():
                hold = _row_to_dict(r, _HOLD_COLUMNS)
                holds.append(
                    {
                        "workflow_kind": hold["workflow_kind"],
                        "set_at": _iso(hold["set_at"]),
                    }
                )
    except Exception as exc:  # noqa: BLE001 — control-read outage fails OPEN (degraded)
        logger.warning(
            "programs: workflow_controls read FAILED tenant=%s — degraded=true exc=%r",
            tenant_id, exc,
        )
        degraded = True

    # active_hold: a run is held when its workflow_kind is paused. Map the run_type onto the
    # registry workflow_kind first (legacy 'orchestrator'/'twilio_inbound' → webhook_inbound),
    # so a legacy run_type still matches a webhook_inbound hold.
    from orchestrator.run_control.rerun import RUN_TYPE_TO_KIND

    held_kinds = {h["workflow_kind"] for h in holds}
    running = []
    for r in running_rows:
        proj = _run_projection(_row_to_dict(r, _RUN_COLUMNS))
        kind = RUN_TYPE_TO_KIND.get(proj["run_type"] or "", proj["run_type"])
        running.append({**proj, "active_hold": kind in held_kinds})

    logger.info(
        "programs OK operator=%s tenant=%s past=%d running=%d upcoming=%d holds=%d degraded=%s",
        operator, tenant_id, len(past), len(running), len(upcoming_7d), len(holds), degraded,
    )
    return {
        "tenant_id": tenant_id,
        "past": past,
        "running": running,
        "upcoming_7d": upcoming_7d,
        "holds": holds,
        "degraded": degraded,
    }


# ---------------------------------------------------------------------------
# /redrive-task — operator redrive of a dead-lettered (or blocked) manager_task (VT-557)
# ---------------------------------------------------------------------------


@router.post("/redrive-task")
def redrive_task_endpoint(
    body: RedriveTaskBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """VT-557 — reset a dead_letter/blocked manager_task to 'planned' for re-dispatch (attempt=0,
    next_retry_at=NULL). Tenant DERIVED from the task row (VT-293/294 IDOR); transport auth verified
    BEFORE the derive so task existence is not probeable (the cancel-override precedent). The
    assignment gate then runs on the derived tenant. CAS-guarded to the redrivable states → 409 if
    the task is not redrivable (already active / a completed terminal). Audit-in-txn."""
    _require_uuid(body.task_id, "task_id")
    verify_internal_secret(x_internal_secret)
    verify_operator_jwt(x_operator_jwt)
    pool = get_pool()
    with pool.connection() as conn:
        tenant_id = _resolve_task_tenant(conn, body.task_id)
        with conn.transaction(), conn.cursor() as cur:
            operator = require_vtr_action(
                cur,
                x_internal_secret=x_internal_secret,
                x_operator_jwt=x_operator_jwt,
                body_operator_id=body.operator_id,
                tenant_id=tenant_id,
                deny_action="task_redrive_denied",
                deny_target_kind="manager_task",
                deny_target_id=body.task_id,
            )
            # Audit BEFORE the mutation, same txn — the 409 rolls both back (no redrive, no audit).
            audit(
                cur,
                operator_id=operator,
                tenant_id=tenant_id,
                action="task_redrive",
                target_kind="manager_task",
                target_id=body.task_id,
                detail=None,
            )
            from orchestrator.manager.task_store import redrive_task

            applied = redrive_task(tenant_id, body.task_id, conn=cur)
            if not applied:
                raise HTTPException(status_code=409, detail="task not redrivable")
    logger.info(
        "redrive_task OK operator=%s tenant=%s task=%s", operator, tenant_id, body.task_id
    )
    return {"ok": True, "task_id": body.task_id, "tenant_id": tenant_id}


# ---------------------------------------------------------------------------
# /kill-campaign — campaign-targeted true-kill (VT-558)
# ---------------------------------------------------------------------------


@router.post("/kill-campaign")
def kill_campaign(
    body: KillCampaignBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """VT-558 — CAS a non-terminal campaign (proposed|approved) → 'cancelled'. The execute loop
    observes it at entry + each recipient boundary and stops the fan-out. Tenant DERIVED from the
    campaign row (VT-293/294); transport auth BEFORE the derive (anti-probe). 409 if not killable
    (already sent/rejected/failed/cancelled). Audit-in-txn on the service cursor."""
    _require_uuid(body.campaign_id, "campaign_id")
    verify_internal_secret(x_internal_secret)
    verify_operator_jwt(x_operator_jwt)
    pool = get_pool()
    with pool.connection() as conn:
        tenant_id = _resolve_campaign_tenant(conn, body.campaign_id)
        with conn.transaction(), conn.cursor() as cur:
            operator = require_vtr_action(
                cur,
                x_internal_secret=x_internal_secret,
                x_operator_jwt=x_operator_jwt,
                body_operator_id=body.operator_id,
                tenant_id=tenant_id,
                deny_action="campaign_kill_denied",
                deny_target_kind="campaign",
                deny_target_id=body.campaign_id,
            )
            audit(
                cur,
                operator_id=operator,
                tenant_id=tenant_id,
                action="campaign_kill",
                target_kind="campaign",
                target_id=body.campaign_id,
                detail=None,
            )
            cur.execute(
                "UPDATE campaigns SET status = 'cancelled' "
                "WHERE tenant_id = %s AND id = %s AND status IN ('proposed', 'approved')",
                (tenant_id, body.campaign_id),
            )
            if (cur.rowcount or 0) == 0:
                raise HTTPException(status_code=409, detail="campaign not killable")
    logger.info(
        "kill_campaign OK operator=%s tenant=%s campaign=%s", operator, tenant_id, body.campaign_id
    )
    return {"ok": True, "campaign_id": body.campaign_id, "tenant_id": tenant_id}


# ---------------------------------------------------------------------------
# /takeover + /release-takeover — VTR seizes/releases a tenant's automation (VT-558)
# ---------------------------------------------------------------------------


@router.post("/takeover")
def takeover(
    body: TakeoverBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """VT-558 — the operator SEIZES the tenant: pause agent_dispatch + freeze every registered agent
    (atomically cancelling in-flight work). Tenant-scoped (body tenant_id + assignment gate).
    Audit-in-txn on the service cursor. Idempotent."""
    _require_uuid(body.tenant_id, "tenant_id")
    reason = _redact_text(body.reason, _registry_or_503(body.tenant_id)) if body.reason else ""
    pool = get_pool()
    with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        operator = require_vtr_action(
            cur,
            x_internal_secret=x_internal_secret,
            x_operator_jwt=x_operator_jwt,
            body_operator_id=body.operator_id,
            tenant_id=body.tenant_id,
            deny_action="takeover_denied",
            deny_target_kind="tenant",
            deny_target_id=body.tenant_id,
        )
        audit(
            cur,
            operator_id=operator,
            tenant_id=body.tenant_id,
            action="tenant_takeover",
            target_kind="tenant",
            target_id=body.tenant_id,
            detail=f"reason_len={len(reason)}",
        )
        from orchestrator.agents.takeover import take_over_tenant

        # take_over_tenant's autonomy freeze emits tm_audit (needs a Connection, not the cursor);
        # conn + cur share this transaction, so the audit + freeze commit or roll back together.
        result = take_over_tenant(
            body.tenant_id, operator_id=operator, reason=f"takeover:{operator}", conn=conn
        )
    logger.info(
        "takeover OK operator=%s tenant=%s frozen=%d",
        operator, body.tenant_id, len(result["frozen_agents"]),
    )
    return {"ok": True, "tenant_id": body.tenant_id, **result}


@router.post("/release-takeover")
def release_takeover_endpoint(
    body: TakeoverBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """VT-558 — reverse a takeover: release the agent_dispatch hold + unfreeze every registered
    agent (work re-enters via the next sweep). Tenant-scoped + assignment gate. Audit-in-txn."""
    _require_uuid(body.tenant_id, "tenant_id")
    pool = get_pool()
    with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        operator = require_vtr_action(
            cur,
            x_internal_secret=x_internal_secret,
            x_operator_jwt=x_operator_jwt,
            body_operator_id=body.operator_id,
            tenant_id=body.tenant_id,
            deny_action="release_takeover_denied",
            deny_target_kind="tenant",
            deny_target_id=body.tenant_id,
        )
        audit(
            cur,
            operator_id=operator,
            tenant_id=body.tenant_id,
            action="tenant_release_takeover",
            target_kind="tenant",
            target_id=body.tenant_id,
            detail=None,
        )
        from orchestrator.agents.takeover import release_takeover

        result = release_takeover(
            body.tenant_id, operator_id=operator, reason=f"release:{operator}", conn=conn
        )
    logger.info(
        "release_takeover OK operator=%s tenant=%s unfrozen=%d",
        operator, body.tenant_id, len(result["unfrozen_agents"]),
    )
    return {"ok": True, "tenant_id": body.tenant_id, **result}
