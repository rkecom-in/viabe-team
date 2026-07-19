"""VT-608 Package 5 — Google Sheets OAuth-install kickoff for the ``start_oauth`` agent tool.

Mirrors ``onboarding.shopify_onboarding.start_shopify_setup`` exactly, for the ``google_sheet``
connector: mint the VT-289 single-use nonce, build the REAL Google authorize URL, persist the
``tenant_integration_state`` phase_2_auth pending-state (the VT-267 handoff pattern) so the owner's
return to chat (or the picker's own POST, RULING 2) resumes correctly. No manual credential paste
(CL-421) — the owner taps the link, approves in the WA in-app browser, and either returns to chat
("done") or is routed straight to the team-web picker page (post-OAuth spreadsheet/tab selection,
``api/sheet_picker.py``).

Kept out of ``shopify_onboarding.py`` (a Shopify-named module) rather than jammed into it — this is
the Sheets-specific mint step; the PHASE/pending helpers it reuses (``_write_state`` /
``_validated_pending`` / ``PHASE_AUTH``) are already connector-agnostic there and imported, not
duplicated.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

logger = logging.getLogger(__name__)

CONNECTOR_ID = "google_sheet"


def start_sheets_oauth(tenant_id: UUID | str, *, ttl_minutes: int = 10) -> dict[str, str]:
    """Mint the REAL Google OAuth ``authorize_url`` link-out + write the phase_2_auth
    pending-state. Mirrors ``start_shopify_setup`` exactly for the ``google_sheet`` connector.

    Returns ``{"authorize_url": ...}``.
    """
    from orchestrator.integrations.connectors.google_sheet import GoogleSheetConnector
    from orchestrator.integrations.oauth_state import mint_install_state
    from orchestrator.onboarding.shopify_onboarding import (
        PHASE_AUTH,
        _validated_pending,
        _write_state,
    )

    state = mint_install_state(tenant_id, CONNECTOR_ID, ttl_minutes=ttl_minutes)
    authorize_url = GoogleSheetConnector().build_auth_url(UUID(str(tenant_id)), state=state)
    pending_expiry = (
        (datetime.now(UTC) + timedelta(minutes=ttl_minutes))
        .replace(microsecond=0)
        .isoformat()
    )
    pending = _validated_pending(
        awaiting="oauth_completion",
        prompt_text=(
            "Tap this link to connect your Google account, pick the sheet you use for "
            "customers/orders, then reply 'done' here."
        ),
        connector_id=CONNECTOR_ID,
        walkthrough_url=authorize_url,
        expires_at=pending_expiry,
    )
    _write_state(tenant_id, phase=PHASE_AUTH, connector_id=CONNECTOR_ID, pending=pending)
    logger.info(
        "VT-608 start_sheets_oauth tenant=%s connector=%s phase=%s (authorize_url minted)",
        tenant_id, CONNECTOR_ID, PHASE_AUTH,
    )
    return {"authorize_url": authorize_url}


__all__ = ["CONNECTOR_ID", "start_sheets_oauth"]
