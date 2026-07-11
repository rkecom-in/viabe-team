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

STATUS vs IMPERATIVE split (dominant Tier-1 loop_stall fix, 2026-07-10): the connect signal
is now TWO regexes. ``_CONNECT_IMPERATIVE_RE`` = base/imperative verbs ("connect my sheet") ->
the MINT branch. ``_CONNECT_STATE_RE`` = past-participle/state references ("is it connected?",
"was it never connected?") -> a STATUS-ANSWER branch that reads the durable connection state
from the DB and replies HONESTLY (never re-dumps the OAuth URL). Previously the single regex
matched "connect(ed)" so a pure status QUESTION dumped the link and looped on push-back.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# Deterministic connect signal — split into IMPERATIVE vs STATE (2026-07-10). NOTE: the shared
# pre_filter_gate._INTEGRATION_INTENT_RE is too narrow for the REAL first-contact phrasings — it
# requires the provider noun immediately after "my/the", so it MISSES "connect my GOOGLE sheet",
# "can we get this connected?", and the Hinglish "Google Sheet ... kaise jodu" (verified against
# the i_sheets scenarios). Those are exactly the asks that stall on the LLM. So this route uses its
# own connect-VERB + provider detector (still fully deterministic, no LLM). The entry gate is WIDE
# (either regex fires the route); the branch inside then distinguishes a request-to-connect from a
# connection-STATUS question so a pure status ask is ANSWERED, not answered-with-a-URL-dump.
#
# _CONNECT_IMPERATIVE_RE — base/imperative verbs only (the owner wants to connect NOW). The (ed|ing)
# state suffixes are deliberately REMOVED from connect/link so "connected"/"linked" do NOT fire it.
_CONNECT_IMPERATIVE_RE = re.compile(
    r"\b(connect|link|integrate|sync|set\s*up|setup|hook\s*up|jod(o|u|na|iye)?|jud)\b",
    re.IGNORECASE,
)
# _CONNECT_STATE_RE — connection STATE/status references (usually a status question, e.g.
# "is it connected?", "was it never connected?"). Answered from the DB; never re-dumps the URL.
_CONNECT_STATE_RE = re.compile(
    r"\b(connect(ed|ion)|linked|synced|hooked\s*up)\b",
    re.IGNORECASE,
)
_SHEETS_RE = re.compile(r"\b(google\s*sheet|sheets?|spreadsheet)\b", re.IGNORECASE)
_SHOPIFY_RE = re.compile(r"\bshopify\b", re.IGNORECASE)


def _connected_or_healthy(tenant_id: UUID | str, provider: str) -> bool:
    """DB-truth 'connected' from EITHER source of record: a ``tenant_oauth_tokens`` row (the
    OAuth-install truth ``is_connector_connected`` reads) OR an enabled+ok ``tenant_connector_
    status`` row (the VT-210 operational truth ``read_integration_health`` reads). They genuinely
    diverge — a status-only tenant answered "not connected" against a healthy, syncing connector
    (the reconnect_broken_sync fabrication residual, §2 judge 2026-07-11). Either row grounds an
    honest "shows connected on my side"."""
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM tenant_oauth_tokens
                WHERE tenant_id = %(t)s AND connector_id = %(c)s
            ) OR EXISTS (
                SELECT 1 FROM tenant_connector_status
                WHERE tenant_id = %(t)s AND connector_id = %(c)s
                  AND enabled AND last_status = 'ok'
            ) AS connected
            """,
            {"t": str(tenant_id), "c": provider},
        ).fetchone()
    if row is None:
        return False
    return bool(row["connected"] if isinstance(row, dict) else row[0])


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

        is_imperative = bool(_CONNECT_IMPERATIVE_RE.search(text))
        is_state = bool(_CONNECT_STATE_RE.search(text))
        if not (is_imperative or is_state):
            return None  # no connect verb/state -> not a connect ask -> normal pipeline

        provider = _detect_provider(text)
        if provider is None:
            return None  # no single unambiguous provider -> let the brain classify

        from orchestrator.onboarding.shopify_onboarding import (
            _pending_is_unexpired,
            _send,
            read_integration_state,
        )

        state = read_integration_state(tenant_id)
        live_flow = state is not None and _pending_is_unexpired(state.get("pending_owner_input"))
        label = "Google Sheet" if provider == "google_sheet" else "Shopify"

        # STATUS-QUESTION branch — a connection-state reference with NO imperative. Answer HONESTLY
        # from the DB; NEVER mint/dump a URL (dumping-then-repeating the link on push-back was the
        # dominant Tier-1 loop_stall / ignored_speech_act trust-breaker).
        if is_state and not is_imperative:
            if _connected_or_healthy(tenant_id, provider):
                answer = f"Yes — your {label} is connected."
            elif live_flow:
                answer = (
                    f"Not yet — the connection isn't finished. Once you've approved on the "
                    f"{label} page, reply 'done' and I'll confirm."
                )
            else:
                answer = f"No — your {label} isn't connected yet. Want me to set it up?"
            _send(recipient, answer, tenant_id=tenant_id)
            logger.info(
                "connector_first_contact: answered connection-status question (deterministic, no URL) "
                "provider=%s tenant=%s",
                provider,
                tenant_id,
            )
            return {"done": False, "phase": "status_answer", "routed": "connector_status_answered"}

        # MINT branch (imperative present) — the EXISTING behavior, plus a CHECK-FIRST lead
        # (T11 residual, §2 judge x3 2026-07-11): a "check and reconnect it" / "sync seems stuck"
        # imperative on an ALREADY-CONNECTED provider dumped a bare OAuth link with no check — the
        # owner's pushback then had no honest prior and the turn spiralled (repeat-link / stall).
        # When the connection exists, the SAME mint message now LEADS with the checked status, so
        # the link reads as a deliberate fresh re-auth, not an evasion of the check they asked for.
        if live_flow:
            return None  # a LIVE connector flow is in progress -> NOT first contact (no double-mint)

        if provider == "google_sheet":
            from orchestrator.integrations.sheets_oauth import start_sheets_oauth

            check_lead = ""
            try:
                if _connected_or_healthy(tenant_id, provider):
                    check_lead = (
                        f"I checked — your {label} shows connected on my side. If new data "
                        "still isn't coming through, let's re-authorize it fresh.\n\n"
                    )
            except Exception:  # noqa: BLE001 — the check is a lead-in; never block the mint
                logger.warning(
                    "connector_first_contact: connected-check failed (mint proceeds) tenant=%s",
                    tenant_id,
                )

            result = start_sheets_oauth(tenant_id)  # mints URL + arms phase_2_auth
            _send(
                recipient,
                f"{check_lead}"
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
