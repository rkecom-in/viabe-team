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

# DF1 cross-tenant / third-party guard (data isolation): a connect/status ask about ANOTHER person's
# business ("check if HIS Shopify is connected", "uski shop ka account") must NEVER emit the OWNER's
# own connection status. HIGH-PRECISION: a third-person possessive bound to a business noun. "is MY
# shopify connected?" carries no third-person possessive, so it self-answers unaffected.
_THIRD_PARTY_SUBJECT_RE = re.compile(
    r"\b(?:his|her|their|uska|uski|unka|unki)\s+(?:\w+\s+)?"
    r"(?:shop|store|business|account|connection|shopify|sheet|sheets|spreadsheet|data|numbers?)\b",
    re.IGNORECASE,
)

# DF1(a) OWNER-DATA-PULL honesty: a request to PULL/IMPORT the owner's OWN data ("pull in my order
# amounts from that sheet", "import my sales") when the named provider is NOT connected must be
# answered HONESTLY — never fabricate having the data. Requires a data-pull VERB and a POSSESSIVE
# owner-data OBJECT ("my orders/sales/...") — a BARE capability question ("can you map fields?")
# carries no possessive data object, so it falls through to the brain.
_OWNER_DATA_PULL_RE = re.compile(
    r"\b(?:pull|import|fetch|bring|load|get|sync|add|include)\b[^.?!]*?"
    r"\b(?:my|mera|meri|mere)\s+(?:\w+\s+){0,2}?"
    r"(?:order|orders|amounts?|dates?|sales?|revenue|customers?|data|numbers?|transactions?|history)\b",
    re.IGNORECASE,
)

# DF1(c) SYNC-FRESHNESS-PUSHBACK: a doubt that FRESH data has arrived — "are you sure? I haven't
# seen new numbers", "nothing's come in", "kuch nahi aaya". On a CONNECTED provider this carries no
# connect verb/state, so it fell through to the brain, which INVENTED a last-sync date (fabrication;
# reconnect_broken_sync_honesty §2 judge). HIGH-PRECISION: a negation/absence marker bound to a
# fresh-data-ARRIVAL word (not a bare "are you sure"), plus the Hinglish "kuch nahi (aaya)". The gate
# that USES this also requires a resolvable, connected/live provider, so a stray match on a non-
# connected tenant simply falls through.
_SYNC_PUSHBACK_RE = re.compile(
    r"(?:"
    # English: a negation within a short span of a 'fresh data showed up' word — "haven't seen new
    # numbers", "nothing has come in", "not updating", "no new data".
    r"\b(?:haven'?t|hasn'?t|didn'?t|not|no|nothing|never)\b[^.?!]{0,40}?"
    r"\b(?:seen|see|come|coming|came|show(?:n|ing|ed|s)?|updat\w*|arriv\w*|refresh\w*|new)\b"
    # Hinglish: "kuch nahi aaya", "kuch naya nahi", "abhi tak nahi aaya".
    r"|\bkuch\b[^.?!]{0,20}?\b(?:nahi|nahin|naya)\b"
    r"|\b(?:nahi|nahin)\b[^.?!]{0,12}?\b(?:aaya|aya|aye|aayi|aayaa)\b"
    r")",
    re.IGNORECASE,
)


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


def _resolve_provider_from_context(tenant_id: UUID | str) -> str | None:
    """DF1(d) — resolve a provider the owner referenced only by BACK-reference ("connect it again",
    "use the same one you already have") from durable context, WITHOUT the LLM. Two ordered, DB-truth
    sources, both fail-soft:
      1. ``tenant_integration_state.current_connector_id`` — the connector the tenant's last/active
         onboarding flow is for (already 'google_sheet' | 'shopify').
      2. the recent conversation window — an EXPLICIT prior provider mention, matched via the SAME
         ``_detect_provider`` (which needs "google sheet"/"sheet"/"spreadsheet" or "shopify"; a bare
         "google" does NOT resolve — no broadening), newest turn first.
    Returns the resolved provider or None (caller then falls through / fails open). Never guesses."""
    try:
        from orchestrator.onboarding.shopify_onboarding import read_integration_state

        state = read_integration_state(tenant_id)
        if state is not None:
            cid = state.get("current_connector_id")
            if cid in ("google_sheet", "shopify"):
                return cid
    except Exception:  # noqa: BLE001 — a state-read miss must not block the conversation-window try
        logger.warning(
            "connector_first_contact: integration-state provider read failed (fail-soft) tenant=%s",
            tenant_id,
        )

    try:
        from orchestrator.conversation_log import active_window

        for turn in reversed(active_window(tenant_id)):  # active_window is oldest-first; scan newest-first
            resolved = _detect_provider(turn.get("text") or "")
            if resolved is not None:
                return resolved
    except Exception:  # noqa: BLE001 — a window-read miss returns None (fall through), never raises
        logger.warning(
            "connector_first_contact: conversation-window provider read failed (fail-soft) tenant=%s",
            tenant_id,
        )
    return None


def _last_sync_at(tenant_id: UUID | str, provider: str) -> Any | None:
    """The real ``tenant_connector_status.last_sync_at`` for this connector, or None when the row
    holds no timestamp (or there is no row). Reads DB truth ONLY — never computes or guesses a time —
    so the freshness answer can state what the DB holds and, on None, HONESTLY admit it has none."""
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            """
            SELECT last_sync_at FROM tenant_connector_status
            WHERE tenant_id = %(t)s AND connector_id = %(c)s AND last_sync_at IS NOT NULL
            ORDER BY last_sync_at DESC LIMIT 1
            """,
            {"t": str(tenant_id), "c": provider},
        ).fetchone()
    if row is None:
        return None
    return row["last_sync_at"] if isinstance(row, dict) else row[0]


def _format_sync_time(value: Any) -> str | None:
    """Render a DB ``last_sync_at`` (datetime or ISO string) FAITHFULLY as a date+time — no relative
    fabrication, only what the DB holds. Returns None if it can't parse (caller then takes the
    honest 'no reliable time' path rather than emit an unparseable value)."""
    if value is None:
        return None
    try:
        from datetime import datetime

        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return dt.strftime("%d %b %Y, %H:%M")
    except Exception:  # noqa: BLE001 — unparseable -> honest-admit path, never emit a bad literal
        return None


def _maybe_answer_sync_freshness(
    tenant_id: UUID | str, text: str, recipient: str | None
) -> dict[str, Any] | None:
    """DF1(c) — answer a sync/data-FRESHNESS pushback ("are you sure? I haven't seen new numbers",
    "kuch nahi aaya") from DB truth instead of letting the brain invent a last-sync date. Fires ONLY
    when the message is a freshness-doubt phrasing AND a provider resolves (in-message or from
    context) AND that provider is connected/live on our side. States the REAL
    ``tenant_connector_status.last_sync_at`` when the DB holds one; else honestly admits it has no
    reliable sync time and offers a re-check/re-auth. NEVER composes a date the DB doesn't hold.
    Returns a routed result dict when it answered, or None to fall through to the normal pipeline."""
    if not _SYNC_PUSHBACK_RE.search(text):
        return None
    provider = _detect_provider(text) or _resolve_provider_from_context(tenant_id)
    if provider is None or not _connected_or_healthy(tenant_id, provider):
        return None  # no connected provider to be honest ABOUT -> fall through (no fabrication here)

    from orchestrator.onboarding.shopify_onboarding import _send

    label = "Google Sheet" if provider == "google_sheet" else "Shopify"
    try:
        raw = _last_sync_at(tenant_id, provider)
    except Exception:  # noqa: BLE001 — a status-read miss takes the honest-admit path, never fabricates
        logger.warning(
            "connector_first_contact: last_sync_at read failed -> honest no-time answer tenant=%s",
            tenant_id,
        )
        raw = None
    when = _format_sync_time(raw)
    if when is not None:
        answer = (
            f"I checked directly — your {label} shows connected and last synced {when}. I'm not "
            "seeing a break on my side. If new numbers still aren't coming through, I can "
            "re-authorize it fresh — want me to?"
        )
    else:
        answer = (
            f"I checked — your {label} shows connected, but I don't have a reliable last-sync time "
            "on record, so I can't say when data last came through. Want me to re-check the "
            "connection or re-authorize it fresh?"
        )
    _send(recipient, answer, tenant_id=tenant_id)
    logger.info(
        "connector_first_contact: answered sync-freshness pushback from DB truth (has_time=%s) "
        "provider=%s tenant=%s",
        when is not None,
        provider,
        tenant_id,
    )
    return {
        "done": False,
        "phase": "sync_freshness_answer",
        "routed": "connector_sync_freshness_answered",
    }


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

        # DF1 — third-party / cross-tenant guard: a connect/status ask about SOMEONE ELSE'S business
        # is DECLINED honestly, never answered with the owner's own status (a cross-tenant leak + the
        # verbatim-loop breaker). Checked BEFORE the imperative/state branches so it can never emit
        # the owner's connection. Offers the owner their OWN connection instead.
        if _THIRD_PARTY_SUBJECT_RE.search(text):
            from orchestrator.onboarding.shopify_onboarding import _send

            _send(
                recipient,
                "I can only help with your own business — I can't check or connect someone else's "
                "account. Want me to look at your own connection instead?",
                tenant_id=tenant_id,
            )
            logger.info(
                "connector_first_contact: declined third-party connect/status ask (isolation) tenant=%s",
                tenant_id,
            )
            return {
                "done": False,
                "phase": "third_party_declined",
                "routed": "connector_third_party_declined",
            }

        is_imperative = bool(_CONNECT_IMPERATIVE_RE.search(text))
        is_state = bool(_CONNECT_STATE_RE.search(text))
        is_data_pull = bool(_OWNER_DATA_PULL_RE.search(text))  # DF1(a)
        if not (is_imperative or is_state or is_data_pull):
            # DF1(c) SYNC-FRESHNESS-PUSHBACK — a pure freshness pushback ("are you sure? I haven't
            # seen new numbers", "kuch nahi aaya") carries NO connect verb/state, so it previously
            # fell through to the brain, which invented a last-sync date. Before falling through,
            # answer it from DB truth — but TIGHTLY: only a freshness-doubt phrasing on a resolvable,
            # connected/live provider fires it; everything else still falls through untouched.
            freshness = _maybe_answer_sync_freshness(tenant_id, text, recipient)
            if freshness is not None:
                return freshness
            return None  # not a connect ask and not a sync-freshness pushback -> normal pipeline

        provider = _detect_provider(text)
        if provider is None:
            # DF1(d) PROVIDER-RESOLVE-FROM-CONTEXT — the owner named the provider only by BACK-
            # reference ("connect it again", "use the same one you already have"). Resolve it from
            # durable context (integration-state connector_id / an explicit prior conversation
            # mention) BEFORE giving up. Never broadens a bare "Google" -> google_sheet.
            provider = _resolve_provider_from_context(tenant_id)
            if provider is None:
                return None  # still no single unambiguous provider -> let the brain classify
            logger.info(
                "connector_first_contact: resolved provider=%s from context (no in-message provider) "
                "tenant=%s",
                provider,
                tenant_id,
            )

        from orchestrator.onboarding.shopify_onboarding import (
            _pending_is_unexpired,
            _send,
            read_integration_state,
        )

        state = read_integration_state(tenant_id)
        live_flow = state is not None and _pending_is_unexpired(state.get("pending_owner_input"))
        label = "Google Sheet" if provider == "google_sheet" else "Shopify"

        # DF1(a) OWNER-DATA-PULL honesty branch — a PURE data-pull ask ("pull in my order amounts
        # from that sheet", no connect verb). If the provider is NOT connected, answer HONESTLY (never
        # fabricate having the data) + point at the one connect step. If it IS connected, fall through
        # to the brain (which can actually pull). Gated to a pure data-pull so a "connect ... and pull"
        # imperative still mints below. Never dumps a bare OAuth URL for a data question.
        if is_data_pull and not (is_imperative or is_state):
            if _connected_or_healthy(tenant_id, provider):
                return None  # connected -> the brain can actually pull the data
            # Copy is careful NOT to promise the data exists (§2 judge, i_sheets_partial): connecting
            # only retrieves what's ACTUALLY in the sheet — if the owner's sheet lacks those columns,
            # implying "connect and I'll pull them" is a false promise. Condition explicitly. Also
            # never reference "the link" unless one is actually attached (live_flow carries it).
            if live_flow:
                answer = (
                    f"I can't see your {label} yet — the connection isn't finished. Approve on the "
                    f"{label} page and reply 'done'. Then I can pull in whatever's actually in the "
                    f"sheet — if those columns aren't there, we'd add them first."
                )
            else:
                answer = (
                    f"I can't see your {label} yet — it isn't connected, so I don't have that data. "
                    f"Once you connect it, I can pull in whatever's actually in the sheet — if order "
                    f"amounts or dates aren't columns there, we'd add them (or connect a source that "
                    f"has them). Want me to send the connect link?"
                )
            _send(recipient, answer, tenant_id=tenant_id)
            logger.info(
                "connector_first_contact: owner-data-pull on unconnected provider -> honest not-"
                "connected (no fabrication) provider=%s tenant=%s",
                provider, tenant_id,
            )
            return {"done": False, "phase": "data_pull_not_connected",
                    "routed": "connector_owner_data_not_connected"}

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
