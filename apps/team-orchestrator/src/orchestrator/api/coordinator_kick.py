"""VT-431 — on-demand coordinator kick endpoint (G35 live-prove path).

POST /api/orchestrator/coordinator/kick

Internal-secret gated (X-Internal-Secret / INTERNAL_API_SECRET — the same secret used
by every internal-call endpoint in this repo). Calls kick_coordinator() for a single
tenant and returns the CoordinatorSweepSummary as JSON.

All coordinator gates are preserved — this endpoint only triggers the existing autonomous
loop on-demand; it does NOT bypass:
  - CL-425 consent gate (owner_inputs basis — fail-closed)
  - VT-474 send-checkpoint (autonomy rails)
  - VT-476 dev send-guard (EXPECTED_ENV)
  - AGENT_AUTONOMY_GLOBAL_FREEZE kill switch

PII-safe: the response carries only counters/flags/timestamps (CoordinatorSweepSummary),
never names, phone numbers, or customer data (CL-390).
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import asdict

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


class CoordinatorKickBody(BaseModel):
    tenant_id: str


@router.post("/api/orchestrator/coordinator/kick")
def coordinator_kick(
    body: CoordinatorKickBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict:
    """Trigger an on-demand coordinator sweep for one tenant.

    Requires the X-Internal-Secret header (same INTERNAL_API_SECRET used by all
    internal endpoints). Returns the CoordinatorSweepSummary as JSON (counts/flags
    only — no PII).
    """
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail={"code": "unauthorized"})

    from orchestrator.agents.coordinator import kick_coordinator

    try:
        summary = kick_coordinator(body.tenant_id)
    except Exception:
        logger.exception(
            "coordinator_kick: kick_coordinator failed (tenant=%s)", body.tenant_id
        )
        raise HTTPException(
            status_code=500, detail={"code": "coordinator_error"}
        ) from None

    return asdict(summary)
