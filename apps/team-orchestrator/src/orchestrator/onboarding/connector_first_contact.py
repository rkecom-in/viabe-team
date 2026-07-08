"""VT-626 — deterministic FIRST-CONTACT connect route.

The manager holds no direct integration-mint tool. The RESUME gate
(``connector_resume.maybe_resume_connector_onboarding``) only fires on an EXISTING
connector state (after a mint armed ``phase_2_auth``). So the FIRST "connect my Google
Sheet / Shopify" ask had no deterministic net — it fell through to ``dispatch_brain``
and relied on the LLM emitting ``spawn_integration``, which intermittently stalled to a
D1 "I'm on it" or narrated a fake handoff (~2/5 runs). Same LLM-gated-handoff failure
class as VT-623 (Sales-Recovery lane).

This module is the deterministic first-contact net. It reuses the existing connect-intent
regex (``pre_filter_gate._INTEGRATION_INTENT_RE`` — already false-negative biased so
ambiguous asks fall through to the brain), adds only a deterministic provider extractor,
and mints/kicks-off WITHOUT the LLM:
  - google_sheet -> ``start_sheets_oauth`` mints the real OAuth link-out + arms phase_2_auth.
  - shopify      -> ``begin_shopify_onboarding`` deterministic discovery kickoff (the shop
                    domain is required before a URL can be built; the resume gate mints
                    once the owner replies with it).

Mirrors ``connector_resume`` exactly: opt-out/DSR wins first, defers when the integration
loop owns the turn (runner-level guard), never re-mints a live flow (idempotency), and is
FAIL-OPEN (any error -> None -> the normal pipeline runs). It runs BEFORE the consent gate
like the resume gate: a deterministic mint never transmits to the LLM, so no consent check.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# Deterministic connect signal. NOTE: the shared pre_filter_gate._INTEGRATION_INTENT_RE is too
# narrow for the REAL first-contact phrasings — it requires the provider noun immediately after
# "my/the", so it MISSES "connect my GOOGLE sheet", "can we get this connected?", and the Hinglish
# "Google Sheet ... kaise jodu" (verified against the i_sheets scenarios). Those are exactly the
# asks that stall on the LLM. So this route uses its own connect-VERB + provider detector (still
# fully deterministic, no LLM): fire only when a connect verb co-occurs with a single unambiguous
# provider. Empirically catches the real English + Hinglish asks with no false-fire on non-connect
# messages ("my google sheet has 500 rows", "send a message to my shopify customers").
_CONNECT_VERB_RE = re.compile(
    r"\b(connect(ed|ing)?|link(ed|ing)?|integrate|sync|set\s*up|setup|hook\s*up|jod(o|u|na|iye)?|jud)\b",
    re.IGNORECASE,
)
_SHEETS_RE = re.compile(r"\b(google\s*sheet|sheets?|spreadsheet)\b", re.IGNORECASE)
_SHOPIFY_RE = re.compile(r"\bshopify\b", re.IGNORECASE)


def _detect_provider(body: str) -> str | None:
    """'google_sheet' | 'shopify' | None. None when ambiguous (both/neither) so the ask
    falls through to the brain — never guess a provider."""
    is_sheets = bool(_SHEETS_RE.search(body))
    is_shopify = bool(_SHOPIFY_RE.search(body))
    if is_sheets and not is_shopify:
        return "google_sheet"
    if is_shopify and not is_sheets:
        return "shopify"
    return None


def maybe_start_connector_onboarding(
    tenant_id: UUID | str,
    body: str,
    message_sid: str | None,
    recipient: str | None,
) -> dict[str, Any] | None:
    """First-contact connect route. Returns a result dict (routed) when it handled the turn,
    or None to fall through to the normal pipeline. FAIL-OPEN."""
    try:
        from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

        text = body or ""
        if matches_opt_out_or_dsr(text):
            return None  # DPDP opt-out / DSR always wins (mirror the resume gate)
        if not _CONNECT_VERB_RE.search(text):
            return None  # no connect verb -> not a connect ask -> normal pipeline

        provider = _detect_provider(text)
        if provider is None:
            return None  # no single unambiguous provider -> let the brain classify

        from orchestrator.onboarding.shopify_onboarding import (
            _pending_is_unexpired,
            read_integration_state,
        )

        state = read_integration_state(tenant_id)
        if state is not None and _pending_is_unexpired(state.get("pending_owner_input")):
            return None  # a LIVE connector flow is in progress -> NOT first contact (no double-mint)

        if provider == "google_sheet":
            from orchestrator.integrations.sheets_oauth import start_sheets_oauth
            from orchestrator.onboarding.shopify_onboarding import _send

            result = start_sheets_oauth(tenant_id)  # mints URL + arms phase_2_auth
            _send(
                recipient,
                "Tap this link to connect your Google account, pick the sheet you use for "
                "your customers/orders, then reply 'done' here:\n"
                f"{result['authorize_url']}",
                tenant_id=tenant_id,
            )
            logger.info(
                "connector_first_contact: minted google_sheet OAuth link (deterministic) tenant=%s",
                tenant_id,
            )
            return {"done": False, "phase": "phase_2_auth", "routed": "sheets_first_contact_minted"}

        # provider == "shopify": no shop domain yet -> deterministic discovery kickoff. The resume
        # gate mints once the owner replies with the *.myshopify.com address.
        from orchestrator.onboarding.shopify_onboarding import begin_shopify_onboarding

        begin_shopify_onboarding(tenant_id, recipient)
        logger.info(
            "connector_first_contact: kicked off shopify discovery (deterministic) tenant=%s",
            tenant_id,
        )
        return {"done": False, "phase": "phase_1_discovery", "routed": "shopify_first_contact_discovery"}
    except Exception:  # noqa: BLE001 — fail-open: a first-contact miss must never break the pipeline
        logger.exception(
            "connector_first_contact: maybe_start_connector_onboarding failed (fail-open) tenant=%s",
            tenant_id,
        )
        return None
