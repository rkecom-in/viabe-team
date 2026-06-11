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
from orchestrator.run_control.registry import REGISTRY
from orchestrator.run_control.rerun import RerunRefused, rerun_from

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
    here; an audited-but-refused attempt is the intended trace (the VT-300 posture)."""
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
        new_run_id = rerun_from(
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
        "rerun OK operator=%s tenant=%s source=%s from_step=%s new_run=%s",
        operator, tenant_id, body.source_run_id, body.from_step, new_run_id,
    )
    return {
        "ok": True,
        "new_run_id": str(new_run_id),
        "source_run_id": body.source_run_id,
        "tenant_id": tenant_id,
    }


# ---------------------------------------------------------------------------
# /timeline/{run_id} — read-only (no audit; mirrors the Gap-6 read endpoints)
# ---------------------------------------------------------------------------


@router.get("/timeline/{run_id}")
def timeline(
    run_id: str,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """Step timeline for one run, read as ``app_vtr_role`` through the mig-131 views ONLY
    (keys-only envelopes by construction — the view is the PII boundary, plan §6). Active
    workflow_controls holds ride along so the panel never shows "not paused" during a hold.
    GET carries no body: the JWT claim id itself feeds the gate's equality leg."""
    _require_uuid(run_id, "run_id")
    verify_internal_secret(x_internal_secret)
    claim = verify_operator_jwt(x_operator_jwt)
    claim_operator = str(claim["operator_id"])
    pool = get_pool()
    with pool.connection() as conn:
        tenant_id = _resolve_run_tenant(conn, run_id)
    operator = _gate(
        x_internal_secret=x_internal_secret,
        x_operator_jwt=x_operator_jwt,
        body_operator_id=claim_operator,
        tenant_id=tenant_id,
        deny_action="timeline_read_denied",
        deny_target_kind="run",
        deny_target_id=run_id,
    )
    with vtr_connection() as conn, conn.cursor() as cur:
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
    logger.info(
        "timeline OK operator=%s tenant=%s run=%s steps=%d active_controls=%d",
        operator, tenant_id, run_id, len(steps), len(controls),
    )
    return {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "steps": steps,
        "active_controls": controls,
    }
