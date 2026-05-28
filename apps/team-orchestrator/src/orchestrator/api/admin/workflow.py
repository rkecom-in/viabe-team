"""VT-224 admin workflow endpoints — replay."""

from __future__ import annotations

from typing import Any

from dbos import DBOS
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from orchestrator.api.admin._auth import log_admin_call
from orchestrator.api.admin._rate_limit import RateLimitedAdmin

router = APIRouter()


class ReplayBody(BaseModel):
    workflow_id: str
    run_id: str | None = None


@router.post("/api/orchestrator/admin/workflow/replay")
def admin_workflow_replay(
    body: ReplayBody,
    request: Request,
    fp: RateLimitedAdmin,
) -> dict[str, Any]:
    """Replays a DBOS workflow from its last persisted step.

    DBOS-native replay via DBOS.resume_workflow. Returns the resumed
    workflow handle's id + status.
    """
    try:
        handle = DBOS.resume_workflow(body.workflow_id)
        status = handle.get_status()
    except Exception as exc:  # noqa: BLE001
        log_admin_call(
            request=request,
            endpoint="POST /api/orchestrator/admin/workflow/replay",
            response_status=500,
            error_message=repr(exc)[:200],
        )
        raise HTTPException(500, f"replay failed: {exc}") from exc

    result = {
        "replay_id": body.workflow_id,
        "status": str(status),
    }
    log_admin_call(
        request=request,
        endpoint="POST /api/orchestrator/admin/workflow/replay",
        response_status=200,
    )
    return result
