"""VT-608 fix round, CRITICAL 1 — the connector-routing dispatcher in front of the deterministic
onboarding-resume gates.

``tenant_integration_state`` (031) has exactly ONE row per tenant — before this fix,
``runner.py`` called ``shopify_onboarding.maybe_resume_shopify_onboarding`` UNCONDITIONALLY for
every tenant with a live discovery/auth-phase pending, regardless of which connector the pending
actually named. A Sheets-flow tenant sitting at ``(phase_2_auth, awaiting='oauth_completion')``
was silently misrouted into the Shopify-only hook (dead-ending the flow, or worse, firing a
Shopify ingest off a Sheets "done" reply if a stale Shopify token happened to exist for that
tenant). This module is the single call site ``runner.py`` now uses instead — it reads the
tenant's ``current_connector_id`` ONCE and routes to the correct connector-specific hook, so each
hook's OWN internals stay exactly as they were (Shopify's is untouched — "byte-identical" per the
fix-round instruction).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


def maybe_resume_connector_onboarding(
    tenant_id: UUID | str,
    body: str,
    message_sid: str | None,
    recipient: str | None,
) -> dict[str, Any] | None:
    """Route to the correct connector's deterministic resume hook. FAIL-OPEN throughout — a
    read/routing failure falls through to the normal pipeline (``None``), never blocks.

    Routing:
      - NO state row at all, OR ``current_connector_id in (None, 'shopify')`` ->
        ``maybe_resume_shopify_onboarding`` UNCONDITIONALLY (byte-identical to pre-fix behavior —
        that function's OWN internal ``read_integration_state`` call already no-ops correctly on
        "no row"/"no connector chosen yet", so this dispatcher must not short-circuit that call
        itself; a pinned regression test proves the legacy gate is still CALLED, not merely
        equivalent-in-result, for the common no-task case).
      - ``'google_sheet'`` -> ``maybe_resume_sheets_onboarding``.
      - anything else (a corrupt/unrecognized value) -> an HONEST blocked posture: logged loudly
        for ops, the message is NOT consumed as a fabricated onboarding step (falls through to the
        normal brain rather than guessing at recovery).
    """
    try:
        from orchestrator.onboarding.shopify_onboarding import read_integration_state

        state = read_integration_state(tenant_id)
        connector_id = state.get("current_connector_id") if state is not None else None

        if connector_id in (None, "shopify"):
            from orchestrator.onboarding.shopify_onboarding import (
                maybe_resume_shopify_onboarding,
            )

            return maybe_resume_shopify_onboarding(tenant_id, body, message_sid, recipient)

        if connector_id == "google_sheet":
            from orchestrator.onboarding.sheets_resume import maybe_resume_sheets_onboarding

            return maybe_resume_sheets_onboarding(tenant_id, body, message_sid, recipient)

        logger.warning(
            "VT-608 connector_resume: unrecognized current_connector_id=%r tenant=%s — "
            "declining to guess a resume step (falls through to the normal pipeline)",
            connector_id, tenant_id,
        )
        return None
    except Exception:  # noqa: BLE001 — owner-inbound HOT PATH: any failure falls through
        logger.exception(
            "maybe_resume_connector_onboarding failed tenant=%s — fall through", tenant_id
        )
        return None


__all__ = ["maybe_resume_connector_onboarding"]
