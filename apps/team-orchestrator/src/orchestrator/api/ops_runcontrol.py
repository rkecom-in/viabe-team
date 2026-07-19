"""VT-300 — VTR live run-control endpoint, RE-POINTED at the VT-374 substrate (N1 RETIRE arm).

POST /api/orchestrator/ops/run-control   (INTERNAL_API_SECRET; team-web calls server-side)

History: this endpoint wrote ``run_controls`` (mig 078), consumed at the supervisor campaign-send
fan-out. VT-374 retired that table (mig 131; the STEP-0 inventory confirmed it single-purpose),
so 'pause' now inserts a ``workflow_controls`` hold (tenant derived from the run row,
``workflow_kind='campaign_send'``) which the supervisor seam reads via the run_control executor.
'steer'/'override' are GONE from this leg: 410 pointing at
``POST /api/orchestrator/ops/run-control/override`` (the Gap-6-authed VT-374 API).

Auth + audit posture UNCHANGED (the adversarial-review finding stands): team-web's auth alone is
fail-OPEN at this leg, so the endpoint RE-DERIVES the run's tenant from pipeline_runs server-side
(NO tenant param crosses the wire → unspoofable) and RE-CHECKS operator_assignments server-side,
fail-CLOSED. Every attempt audits to ops_audit (executed OR denied). The directive free text is
redacted at WRITE through the pii_redactor WITH the tenant's name registry (plan §5); a
registry-build failure DROPS the text rather than storing it unredacted — the pause itself still
lands (a safety hold must not be blocked by a redaction dependency; the live UI never populates
directive anyway, per the N1 inventory).

The team-web relay is unchanged in shape: same body, same ``{ok, control_id, tenant_id,
control_type}`` response. A repeat pause is idempotent — the active hold's id is returned (the
partial-unique index allows one active hold per (tenant, kind)).
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from orchestrator.graph import get_pool
from orchestrator.privacy.customer_registry import make_name_registry
from orchestrator.privacy.pii_redactor import redact

logger = logging.getLogger(__name__)
router = APIRouter()

_VALID_CONTROLS = ("pause", "steer", "override")
_GONE_CONTROLS = ("steer", "override")  # VT-374: moved to the run-control substrate API


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


class RunControlBody(BaseModel):
    run_id: str
    operator_id: str
    control_type: str
    directive: str | None = None
    # NOTE: deliberately NO tenant_id — the tenant is DERIVED from run_id server-side so a client
    # cannot pair a foreign run with an assigned tenant (the VT-293/294 IDOR rule).


def _resolve_run_tenant(cur: Any, run_id: str) -> str | None:
    cur.execute("SELECT tenant_id FROM pipeline_runs WHERE id = %s LIMIT 1", (run_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return str(row["tenant_id"] if isinstance(row, dict) else row[0])


def _operator_assigned(cur: Any, operator_id: str, tenant_id: str) -> bool:
    """Active operator_assignments row for (operator, tenant)? Fazal-UUID = VTAdmin break-glass."""
    fazal = (os.environ.get("FAZAL_OWNER_UUID", "") or "").strip()
    if fazal and operator_id == fazal:
        return True
    cur.execute(
        "SELECT 1 FROM operator_assignments "
        "WHERE operator_id = %s AND tenant_id = %s AND unassigned_at IS NULL LIMIT 1",
        (operator_id, tenant_id),
    )
    return cur.fetchone() is not None


def _audit(cur: Any, *, operator_id: str, tenant_id: str | None, action: str, run_id: str, detail: str | None) -> None:
    cur.execute(
        "INSERT INTO ops_audit (operator_id, tenant_id, action, target_kind, target_id, detail) "
        "VALUES (%s, %s, %s, 'run', %s, %s)",
        (operator_id, tenant_id, action, run_id, detail),
    )


def _redacted_reason(tenant_id: str, directive: str | None) -> str | None:
    """Write-time redaction (VT-374 plan §5): pattern + name-registry. Registry failure DROPS the
    text — never store unredacted, never block the safety hold on a redaction dependency."""
    if not directive:
        return None
    try:
        registry = make_name_registry(tenant_id)
        return str(redact(directive, name_registry=registry))[:500]
    except Exception as exc:  # noqa: BLE001 — drop the text, keep the pause
        logger.warning(
            "run_control: name-registry build failed tenant=%s — directive text dropped "
            "(never stored unredacted) exc=%r",
            tenant_id, exc,
        )
        return None


@router.post("/api/orchestrator/ops/run-control")
def run_control(
    body: RunControlBody,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail="invalid internal secret")
    if body.control_type not in _VALID_CONTROLS:
        raise HTTPException(status_code=400, detail=f"invalid control_type; one of {_VALID_CONTROLS}")
    if body.control_type in _GONE_CONTROLS:
        raise HTTPException(
            status_code=410,
            detail=(
                "steer/override moved to the VT-374 run-control substrate; "
                "use POST /api/orchestrator/ops/run-control/override"
            ),
        )
    try:
        UUID(body.run_id)
        UUID(body.operator_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid run_id / operator_id") from None

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        # 1. Re-derive the run's TRUE tenant server-side (never trust a client tenant).
        tenant_id = _resolve_run_tenant(cur, body.run_id)
        if tenant_id is None:
            raise HTTPException(status_code=404, detail="run not found")

        # 2. Re-check assignment server-side, fail-CLOSED. Audit the DENY too.
        if not _operator_assigned(cur, body.operator_id, tenant_id):
            _audit(
                cur, operator_id=body.operator_id, tenant_id=tenant_id,
                action="control_denied", run_id=body.run_id, detail=body.control_type,
            )
            logger.info(
                "run_control DENIED operator=%s run=%s tenant=%s control=%s",
                body.operator_id, body.run_id, tenant_id, body.control_type,
            )
            raise HTTPException(status_code=403, detail="operator not assigned to this run's tenant")

    # Registry-backed redaction opens its own tenant connection — run it BETWEEN pool checkouts,
    # never while a service-pool connection is held (nested-checkout exhaustion hazard).
    reason = _redacted_reason(tenant_id, body.directive)

    with pool.connection() as conn, conn.cursor() as cur:
        # 3. Authorized — set the tenant-wide campaign_send hold on the VT-374 substrate (the
        # supervisor seam holds before fan-out). Idempotent under the one-active partial-unique
        # index: a concurrent/prior active hold's id is returned instead of a second row.
        cur.execute(
            "INSERT INTO workflow_controls (tenant_id, workflow_kind, set_by, reason) "
            "VALUES (%s, 'campaign_send', %s, %s) "
            "ON CONFLICT (tenant_id, workflow_kind) WHERE released_at IS NULL DO NOTHING "
            "RETURNING id",
            (tenant_id, body.operator_id, reason),
        )
        ctrl_row = cur.fetchone()
        if ctrl_row is None:
            cur.execute(
                "SELECT id FROM workflow_controls "
                "WHERE tenant_id = %s AND workflow_kind = 'campaign_send' "
                "AND released_at IS NULL LIMIT 1",
                (tenant_id,),
            )
            ctrl_row = cur.fetchone()
            if ctrl_row is None:
                # Insert lost to a hold that was released in between — contended; client retries.
                raise HTTPException(status_code=409, detail="pause state contended; retry")
        ctrl_id = str(ctrl_row["id"] if isinstance(ctrl_row, dict) else ctrl_row[0])
        _audit(
            cur, operator_id=body.operator_id, tenant_id=tenant_id,
            action="control_executed", run_id=body.run_id,
            detail=f"{body.control_type}:{ctrl_id}",
        )

    logger.info(
        "run_control OK operator=%s run=%s tenant=%s control=%s id=%s",
        body.operator_id, body.run_id, tenant_id, body.control_type, ctrl_id,
    )
    # C5: state the NEW semantics in the response. Legacy 'pause' is no longer a per-run steer —
    # it now sets a tenant-wide campaign_send hold on the VT-374 substrate that stays until it is
    # explicitly released (the old run_controls auto-expiry is gone). Callers/operators must know
    # the hold is sticky and tenant-scoped, and how to lift it.
    return cast(
        "dict[str, Any]",
        {
            "ok": True,
            "control_id": ctrl_id,
            "tenant_id": tenant_id,
            "control_type": body.control_type,
            "detail": (
                "tenant-wide campaign_send hold until released via "
                "POST /api/orchestrator/ops/run-control/release"
            ),
        },
    )
