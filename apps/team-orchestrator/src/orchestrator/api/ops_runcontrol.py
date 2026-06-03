"""VT-300 — VTR live run-control endpoint (the enforcement leg).

POST /api/orchestrator/ops/run-control   (INTERNAL_API_SECRET; team-web calls server-side)

Records a VTR's pause/steer/override on a LIVE run into run_controls (mig 078), which the graph
consumes at node boundaries. The adversarial review's key finding: team-web's auth alone is
fail-OPEN at this leg (ops_resolve.py does AuthN, not tenant AuthZ). So this endpoint RE-DERIVES
the run's tenant from pipeline_runs server-side (NO tenant param crosses the wire → unspoofable)
and RE-CHECKS operator_assignments server-side, fail-CLOSED. Every attempt audits to ops_audit
(executed OR denied). directive is PII-scrubbed (CL-390/CL-426). Override is VTR-issuable (Fazal).

The EFFECTING (graph reads run_controls at the next node boundary → re-arm interrupt for pause /
consume directive for steer/override) is the orchestrator graph handler; this endpoint is the
authorized, audited write.
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

logger = logging.getLogger(__name__)
router = APIRouter()

_VALID_CONTROLS = ("pause", "steer", "override")


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


@router.post("/api/orchestrator/ops/run-control")
def run_control(
    body: RunControlBody,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail="invalid internal secret")
    if body.control_type not in _VALID_CONTROLS:
        raise HTTPException(status_code=400, detail=f"invalid control_type; one of {_VALID_CONTROLS}")
    try:
        UUID(body.run_id)
        UUID(body.operator_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid run_id / operator_id") from None

    # Scrub PII from the human-typed directive BEFORE it touches the DB (CL-390/CL-426).
    directive: str | None = None
    if body.directive:
        from orchestrator.alerts.pii_scrub import scrub_pii

        directive = scrub_pii(body.directive)[:500]

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

        # 3. Authorized — record the control + audit control_executed (server-resolved tenant).
        cur.execute(
            "INSERT INTO run_controls (run_id, tenant_id, control_type, directive, requested_by) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (body.run_id, tenant_id, body.control_type, directive, body.operator_id),
        )
        ctrl_row = cur.fetchone()
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
    return cast(
        "dict[str, Any]",
        {"ok": True, "control_id": ctrl_id, "tenant_id": tenant_id, "control_type": body.control_type},
    )
