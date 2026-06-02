"""Orchestrator HTTP API router (VT-3.3a)."""

from __future__ import annotations

from fastapi import APIRouter

from orchestrator.api.admin import router as admin_router
from orchestrator.api.consent_capture import router as consent_capture_router
from orchestrator.api.drive_push import router as drive_push_router
from orchestrator.api.integration_push import router as integration_push_router
from orchestrator.api.oauth_callback import router as oauth_callback_router
from orchestrator.api.onboard_step import router as onboard_step_router
from orchestrator.api.ops_resolve import router as ops_resolve_router
from orchestrator.api.owner_verify import router as owner_verify_router
from orchestrator.api.sheet_push import router as sheet_push_router
from orchestrator.api.shopify_webhook import router as shopify_webhook_router
from orchestrator.api.twilio_ingress import router as twilio_ingress_router

router = APIRouter()
router.include_router(twilio_ingress_router)
router.include_router(ops_resolve_router)
router.include_router(owner_verify_router)
router.include_router(oauth_callback_router)
router.include_router(sheet_push_router)
router.include_router(integration_push_router)
router.include_router(shopify_webhook_router)
router.include_router(onboard_step_router)
router.include_router(admin_router)
router.include_router(drive_push_router)
router.include_router(consent_capture_router)
