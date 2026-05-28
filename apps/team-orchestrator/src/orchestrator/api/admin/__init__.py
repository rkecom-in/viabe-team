"""VT-224 admin endpoints aggregator.

Mounts under /api/orchestrator/admin/...

All routes gated by X-Team-Admin-Token header (see _auth.py).
"""

from __future__ import annotations

from fastapi import APIRouter

from orchestrator.api.admin.connector import router as connector_router
from orchestrator.api.admin.health import router as health_router
from orchestrator.api.admin.workflow import router as workflow_router

router = APIRouter()
router.include_router(connector_router)
router.include_router(workflow_router)
router.include_router(health_router)

__all__ = ["router"]
