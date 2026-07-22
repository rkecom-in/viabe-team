"""VT-691 — WhatsApp-initiated signup: an ``unknown_sender`` inbound BECOMES a signup.

For a WhatsApp-first product (CL-443) the natural front door is the inbound WhatsApp itself.
Flow (consent-gated, DPDP — the gates here never bend):

  1. unknown inbound → the CONSENT ask (in-session interactive quick-reply buttons — the
     inbound just opened the 24h window, so no Meta template is needed; freeform text with a
     typed-exact instruction is the fallback). NO tenant yet; the pending state lives in
     ``whatsapp_signup_sessions`` (mig 180, FORCE-RLS deny-all, service-role only — the
     waitlist_signups pre-tenant-PII posture).
  2. their next reply → consent classification, FULLY DETERMINISTIC (Fazal ruling
     2026-07-22: the signup does not start unless the person EXPLICITLY presses the
     "I agree" button — the consent ask is an in-session interactive quick-reply,
     team_signup_consent_buttons, with an explicit "I do not agree" refusal path for
     DPDP/EU). A tap echoes the button TITLE as the inbound Body, so the grant set is the
     exact-normalized title (a typed byte-identical "I agree" is indistinguishable from a
     tap and equally explicit). Order: opt-out/DSR veto → exact agree-title → consent;
     exact disagree-title → declined; ANYTHING else (incl. free-text "yes") → re-prompt
     with the buttons, bounded. No LLM anywhere in the grant path — the strongest DPDP
     capture posture (finite exact-match outcomes only, per the no-keyword-lists rule).
  3. consent → ``signup.create_whatsapp_signup_tenant`` (consent proof + trial; NO OTP — the
     WhatsApp inbound is already Meta-phone-verified; ``created_via='whatsapp'``) → the
     onboarding journey kicks off in-session via the proven ``"complete setup"`` token path
     and collects the business details the public page asks.

Abuse gates: the whole path is behind ``ENABLE_WHATSAPP_SIGNUP`` (default OFF — unknown_sender
behavior is byte-identical to today); the ingress adds a workspace-wide per-minute bucket for
unknown-sender prompts; per-number the UNIQUE session row is the idempotency anchor and
``consent_prompt_count`` is the hard prompt budget (MAX_CONSENT_PROMPTS, then 'expired' +
silent). A declined session goes SILENT — a refusal is respected, never re-prompted.

Pillar 1: this module is called from a DBOS workflow (``whatsapp_signup_run``) the ingress
starts — classification never runs inside the transport endpoint.

CL-390: never log the raw phone (hash_phone tokens only); the raw number lives ONLY in the
deny-all session table and the tenant row it converts into.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any
from uuid import UUID

from dbos import DBOS

from orchestrator.utils.phone_token import hash_phone

logger = logging.getLogger(__name__)

# --- knobs -------------------------------------------------------------------------------------

#: Hard cap on consent prompts per number (initial prompt + re-asks). Exhausted → 'expired',
#: silent. With explicit buttons a genuine user converges in one tap; three chances is plenty,
#: and a spammer / repeated cold "Hi" burns out fast and goes silent.
MAX_CONSENT_PROMPTS = 3

#: Module-owned retention (pre-tenant PII is outside the tenant DSR purge): stale
#: non-converted sessions are deleted opportunistically at prompt time past this age.
RETENTION_DAYS = 30

# The interactive consent ask (registry entry; canary vt691_consent_buttons_create.py). The
# button TITLES are the consent contract — the grant/refusal sets below are their exact
# normalized forms. NEVER change one without the other, same commit.
INTERACTIVE_CONSENT_TEMPLATE = "team_signup_consent_buttons"
_AGREE_TITLE = "I agree"
_DISAGREE_TITLE = "I do not agree"

# The freeform FALLBACK consent ask (interactive send failure only): same consent text, with a
# typed-exact instruction that matches the same grant set.
CONSENT_PROMPT = (
    "Namaste! This is Viabe Team — an AI teammate that runs everyday business tasks for you "
    "on WhatsApp.\n\n"
    "To create your account I need your consent: I'll process your business data as described "
    "in our data-processing notice (viabe.ai/team/dpdp) and store it in India "
    "(viabe.ai/team/privacy).\n\n"
    "Reply exactly “I agree” to agree and start your free trial — or “I do not agree” to "
    "decline. / शुरू करने के लिए “I agree” लिखें।"
)

DECLINED_ACK = (
    "No problem — I won't message you again. If you change your mind, just say hi anytime."
)

WELCOME_AFTER_CONSENT = (
    "Done! Your Viabe Team account is created and your free trial has started. "
    "Let's set up your business — a few quick questions."
)


def from_scratch_question_queue() -> list[dict[str, Any]]:
    """The seed question queue for a WhatsApp-created tenant (adversarial-verify finding A).

    A web tenant's journey composes its queue FROM the discovery draft (confirm-first); a
    WhatsApp tenant has NO draft and never will (nothing to anchor discovery on), so the
    draft-gated lazy-fill would leave the queue empty forever — welcome promised questions,
    none ever arrived. Seeding an explicit queue at start_journey time closes that: the cursor
    walks it deterministically, and the existing answer machinery (volunteered/out-of-order
    handling, the never-assert business-type taxonomy gate) applies unchanged. Fields = the
    same details the public signup page asks (the VT-691 row's parity contract); language is
    NOT asked (observed from usage, VT-677)."""
    return [
        {"field": "business_name", "kind": "gap", "draft_value": None,
         "prompt_en": "What's your business called?",
         "prompt_hi": "आपके बिज़नेस का नाम क्या है?"},
        {"field": "owner_name", "kind": "gap", "draft_value": None,
         "prompt_en": "And your name?",
         "prompt_hi": "और आपका नाम?"},
        {"field": "business_type", "kind": "gap", "draft_value": None,
         "prompt_en": "What kind of business is it? (e.g. restaurant, salon, kirana/retail, "
                      "services)",
         "prompt_hi": "यह किस तरह का बिज़नेस है? (जैसे रेस्टोरेंट, सैलून, किराना/रिटेल, सर्विसेज़)"},
        {"field": "city", "kind": "gap", "draft_value": None,
         "prompt_en": "Which city are you in?",
         "prompt_hi": "आप किस शहर में हैं?"},
    ]


# --- session CRUD (service-role pool; the table is FORCE-RLS deny-all) --------------------------


def _pool():  # noqa: ANN202
    from orchestrator.graph import get_pool

    return get_pool()


def get_session(phone_e164: str) -> dict[str, Any] | None:
    with _pool().connection() as conn:
        row = conn.execute(
            "SELECT id, status, consent_prompt_count, last_prompt_at, tenant_id "
            "FROM whatsapp_signup_sessions WHERE phone_e164 = %s",
            (phone_e164,),
        ).fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return {
        "id": row[0], "status": row[1], "consent_prompt_count": row[2],
        "last_prompt_at": row[3], "tenant_id": row[4],
    }


def upsert_prompted(phone_e164: str) -> None:
    """Create the session on first contact, or bump the prompt bookkeeping on a re-prompt.
    UNIQUE(phone_e164) makes repeated inbounds idempotent — never a duplicate pending signup."""
    with _pool().connection() as conn:
        conn.execute(
            "INSERT INTO whatsapp_signup_sessions (phone_e164) VALUES (%s) "
            "ON CONFLICT (phone_e164) DO UPDATE SET "
            "  consent_prompt_count = whatsapp_signup_sessions.consent_prompt_count + 1, "
            "  last_prompt_at = now()",
            (phone_e164,),
        )


def mark_consented(phone_e164: str, tenant_id: UUID | str) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE whatsapp_signup_sessions "
            "SET status = 'consented', consented_at = now(), tenant_id = %s "
            "WHERE phone_e164 = %s",
            (str(tenant_id), phone_e164),
        )


def mark_status(phone_e164: str, status: str) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE whatsapp_signup_sessions SET status = %s WHERE phone_e164 = %s",
            (status, phone_e164),
        )


def purge_stale(*, retention_days: int = RETENTION_DAYS) -> int:
    """Module-owned retention: DELETE non-converted sessions older than the bound (pre-tenant
    PII outside the tenant DSR purge — the waitlist-data policy shape). Converted
    ('consented') rows keep their audit link and age out with the tenant instead."""
    with _pool().connection() as conn:
        cur = conn.execute(
            "DELETE FROM whatsapp_signup_sessions "
            "WHERE status <> 'consented' "
            "  AND created_at < now() - make_interval(days => %s)",
            (int(retention_days),),
        )
        return cur.rowcount if cur.rowcount is not None else 0


# --- consent classification (LLM-primary; deterministic veto in the safe direction only) --------


def _normalize_exact(body: str) -> str:
    normalized = (
        unicodedata.normalize("NFC", (body or "").strip().casefold())
        .replace("'", "")
        .replace("’", "")
    )
    return re.sub(r"[\s]+", " ", normalized).strip(".!। ")


def consent_hard_stop(body: str) -> str | None:
    """Deterministic veto, SAFE direction only ('declined' — never a consent). Opt-out / DSR
    phrasing from an un-onboarded number is a person telling us to go away — respect it."""
    from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

    if matches_opt_out_or_dsr(body or ""):
        return "declined"
    return None


def classify_consent_reply(body: str) -> str:
    """'consent' | 'declined' | 'unclear' — the DPDP gate, FULLY DETERMINISTIC (Fazal
    2026-07-22: consent is captured ONLY by the explicit "I agree" button press — its title
    echoes back as the Body; a byte-identical typed reply is the same explicit act).

    Order: hard-stop veto (STOP/DSR → declined) → exact agree-title → consent → exact
    disagree-title → declined → EVERYTHING else (free-text "yes" included) → 'unclear',
    which re-prompts with the buttons. No LLM anywhere in the grant path.
    """
    veto = consent_hard_stop(body)
    if veto is not None:
        return veto
    normalized = _normalize_exact(body)
    if normalized == _normalize_exact(_AGREE_TITLE):
        return "consent"
    if normalized == _normalize_exact(_DISAGREE_TITLE):
        return "declined"
    return "unclear"


# --- the flow driver ----------------------------------------------------------------------------


def _send(phone_e164: str, text: str) -> None:
    """Freeform send to the (tenant-less) number — the inbound just opened the 24h window.
    Dev send-guard + mock mode apply unchanged (twilio_send._client)."""
    from orchestrator.utils.twilio_send import send_freeform_message

    send_freeform_message(text, phone_e164)


def _send_consent_prompt(phone_e164: str) -> None:
    """The consent ask: interactive quick-reply FIRST (the explicit I-agree / I-do-not-agree
    buttons — the Fazal-ruled capture), freeform text with the typed-exact instruction as the
    fallback on any interactive failure. Same in-session window either way."""
    try:
        from orchestrator.templates_registry import content_sid_for
        from orchestrator.utils.twilio_send import send_interactive_message

        content_sid = content_sid_for(INTERACTIVE_CONSENT_TEMPLATE, "en")
        if content_sid:
            send_interactive_message(content_sid, phone_e164)
            return
    except Exception:  # noqa: BLE001 — the buttons are the preferred surface, never the only one
        logger.warning("whatsapp_signup: interactive consent send failed — freeform fallback")
    _send(phone_e164, CONSENT_PROMPT)


def handle_unknown_inbound(phone_e164: str, body: str, message_sid: str | None) -> dict[str, Any]:
    """The VT-691 state machine for one unknown-sender inbound. Returns an outcome dict
    (logged by the workflow; never raises — a signup-path error must never 5xx the ingress
    or crash the workflow into retry-spam)."""
    token = hash_phone(phone_e164)
    try:
        try:
            purged = purge_stale()
            if purged:
                logger.info("whatsapp_signup: retention purge removed %d stale session(s)", purged)
        except Exception:  # noqa: BLE001 — hygiene only
            logger.warning("whatsapp_signup: retention purge failed (fail-soft)")

        session = get_session(phone_e164)

        # First contact. A cold message that is ITSELF a STOP/DSR-shaped refusal gets no
        # solicitation (adversarial-verify hygiene finding): record declined + stay silent —
        # the person told us to go away before we ever asked.
        if session is None:
            if consent_hard_stop(body) is not None:
                upsert_prompted(phone_e164)
                mark_status(phone_e164, "declined")
                logger.info("whatsapp_signup: first contact was a refusal → silent from=%s", token)
                return {"outcome": "declined_silent", "phone_token": token}
            upsert_prompted(phone_e164)
            _send_consent_prompt(phone_e164)
            logger.info("whatsapp_signup: consent prompted (first contact) from=%s", token)
            return {"outcome": "consent_prompted", "phone_token": token}

        status = str(session.get("status"))

        if status == "declined":
            # A refusal is respected — permanent silence (they can still reach us; we never
            # re-prompt). DPDP posture: no processing beyond remembering "don't ask again".
            return {"outcome": "declined_silent", "phone_token": token}

        if status == "expired":
            return {"outcome": "expired_silent", "phone_token": token}

        if status == "consented":
            # Tenant already exists for this number — the ingress tenant-lookup should have
            # routed it. Defensive no-op (a race between conversion and the next inbound).
            return {"outcome": "already_consented_noop", "phone_token": token}

        # status == 'consent_pending' → this reply answers the consent ask.
        decision = classify_consent_reply(body)

        if decision == "consent":
            from orchestrator.onboarding.signup import create_whatsapp_signup_tenant

            res = create_whatsapp_signup_tenant(phone_e164)
            mark_consented(phone_e164, res.tenant_id)
            logger.info(
                "whatsapp_signup: CONSENTED → tenant created tenant=%s created=%s from=%s",
                res.tenant_id, res.created, token,
            )
            _send(phone_e164, WELCOME_AFTER_CONSENT)
            # Start the journey with the SEEDED from-scratch queue (finding A: without a row +
            # a non-empty queue, the draft-gated lazy-fill never asks anything), then kick the
            # first question through the SAME proven path the welcome button uses (the exact
            # "complete setup" kickoff token). Fail-open like the journey itself.
            try:
                from orchestrator.onboarding.journey import (
                    get_journey,
                    maybe_handle_journey_reply,
                    start_journey,
                )

                # created=True → fresh seed. created=False (redelivered consent / crash
                # between create and start) → seed ONLY if no journey row exists yet;
                # start_journey RESETS an existing row, which would wipe real progress.
                if res.created or get_journey(res.tenant_id) is None:
                    start_journey(res.tenant_id, from_scratch_question_queue())
                maybe_handle_journey_reply(
                    res.tenant_id, "complete setup", message_sid, phone_e164
                )
            except Exception:  # noqa: BLE001 — the next owner reply re-enters the journey gate
                logger.warning(
                    "whatsapp_signup: journey start/kickoff failed (next reply re-enters) "
                    "tenant=%s", res.tenant_id,
                )
            return {
                "outcome": "tenant_created",
                "tenant_id": str(res.tenant_id),
                "created": res.created,
                "phone_token": token,
            }

        if decision == "declined":
            mark_status(phone_e164, "declined")
            _send(phone_e164, DECLINED_ACK)
            logger.info("whatsapp_signup: declined from=%s", token)
            return {"outcome": "declined", "phone_token": token}

        # 'unclear' (anything that isn't the agree/disagree title or a hard-stop — free-text
        # "yes" included) → immediate bounded re-prompt with the buttons. Buttons converge a
        # genuine user in one tap; the hard MAX_CONSENT_PROMPTS cap is the spam bound, after
        # which the session expires silent.
        prompts = int(session.get("consent_prompt_count") or 0)
        if prompts >= MAX_CONSENT_PROMPTS:
            mark_status(phone_e164, "expired")
            logger.info("whatsapp_signup: prompts exhausted → expired from=%s", token)
            return {"outcome": "expired", "phone_token": token}
        upsert_prompted(phone_e164)
        _send_consent_prompt(phone_e164)
        logger.info("whatsapp_signup: unclear reply → re-prompted with buttons (%d/%d) from=%s",
                    prompts + 1, MAX_CONSENT_PROMPTS, token)
        return {"outcome": "consent_reprompted", "phone_token": token}
    except Exception:  # noqa: BLE001 — never crash the workflow into retry-spam
        logger.exception("whatsapp_signup: handler failed (fail-soft) from=%s", token)
        return {"outcome": "error", "phone_token": token}


@DBOS.workflow()
def whatsapp_signup_run(phone_e164: str, body: str, message_sid: str) -> dict[str, Any]:
    """Durable unknown-sender signup processing (VT-691). The workflow boundary gives
    idempotency on Twilio redelivery via the ingress's SetWorkflowID (``wa_signup_{sid}``) —
    a redelivered inbound replays to the SAME outcome instead of double-prompting."""
    return handle_unknown_inbound(phone_e164, body, message_sid)


__all__ = [
    "CONSENT_PROMPT",
    "MAX_CONSENT_PROMPTS",
    "RETENTION_DAYS",
    "classify_consent_reply",
    "consent_hard_stop",
    "handle_unknown_inbound",
    "purge_stale",
    "whatsapp_signup_run",
]
