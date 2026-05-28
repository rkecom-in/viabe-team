"""Orchestrator HTTP API router (VT-3.3a)."""

from __future__ import annotations

from fastapi import APIRouter

from orchestrator.api.oauth_callback import router as oauth_callback_router
from orchestrator.api.ops_resolve import router as ops_resolve_router
from orchestrator.api.sheet_push import router as sheet_push_router
from orchestrator.api.twilio_ingress import router as twilio_ingress_router

router = APIRouter()
router.include_router(twilio_ingress_router)
router.include_router(ops_resolve_router)
router.include_router(oauth_callback_router)
router.include_router(sheet_push_router)
