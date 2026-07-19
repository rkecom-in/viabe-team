"""VT-287 — inbound-first WhatsApp customer pipeline (deterministic, Pillar 1).

The existing pipeline is owner-centric (subscriber_states PK=tenant = the owner). This
is the SEPARATE customer-inbound path (Cowork ruling 2026-06-02): a customer messages the
business's WABA number, which anchors consent customer→business (fiduciary; Viabe
processor) and opens the 24h session window.

DETERMINISTIC — zero LLM (Pillar 1; cost + guardrail surface). Classification is exact
keyword matching:
- STOP / opt-out keyword  -> consent.opt_out (withdraw). Always honored.
- affirmative (YES/…)     -> consent.record_consent(wa_inbound_optin) — the opt-in.
- first contact (no consent row) -> send the intro ONCE (intro_sent_at guard — Cowork's
  re-send guard: 3 pre-consent messages get the intro once, not thrice).
- established (has consent) -> a templated business-voice reply (v1; a customer-facing
  reasoning agent is DEFERRED to VT-299).

All OUTBOUND sends pass the shared customer-send pre-gate
(`agents.customer_send_choke.assert_customer_send_allowed`, VT-460): the onboarded/activation
bar (`onboarding_gate.is_agent_eligible`) AND the WABA-live gate
(`whatsapp_account.wa_send_allowed` — fail-CLOSED, no send unless the WABA is Meta-verified `live`).
This is the SESSION_OPTIN class (distinct from marketing — opt-in solicitation is NOT folded under
the marketing-consent gate). State (consent, conversation marker) is recorded regardless so a STOP
is never lost, and the dispatch runs inside `customer_send_context()` so the transport's structural
customer-send choke admits it.

Consent COPY is legal-sensitive: the binding text lives in `.viabe/consent-text.md`
(versioned; Cowork drafts + Fazal legal). This module records the version a customer
agreed to and sends a minimal templated body — it does NOT hardcode the legal copy.
Phone tokenised at the boundary (CL-390); raw number never persisted/logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from orchestrator.db import tenant_connection
from orchestrator.keyword_match import boundary_patterns, contains_any
from orchestrator.privacy import consent as consent_service
from orchestrator.utils.phone_token import hash_phone

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent / "config"

# Default consent_text version the inbound opt-in records (see .viabe/consent-text.md).
# The legally-validated copy maps to this version string; Cowork/Fazal own the copy.
_DEFAULT_CONSENT_VERSION = "qr_consent_v0_draft_en"

# SendFn(body, recipient_phone) -> message_sid. Injectable for tests (default = the real
# free-form WhatsApp send; the customer just messaged in, so the 24h window is open).
SendFn = Callable[[str, str], str]


def _load_keywords(filename: str) -> set[str]:
    data = yaml.safe_load((_CONFIG_DIR / filename).read_text())
    return {str(k).strip().casefold() for k in data.get("keywords", [])}


# VT-358: a CUSTOMER opt-out (STOP / बंद करो / band karo) is consent-critical — a missed STOP is a
# DPDP/WhatsApp breach. Match by boundary-safe CONTAINMENT (not whole-body-exact), so "please STOP"
# / "please बंद करो" route. Compiled from the SAME opt_out_keywords.yaml + the SAME shared helper
# the owner gate uses (VT-329), so the two consent surfaces can't drift.
_OPT_OUT_PATTERNS = boundary_patterns(_load_keywords("opt_out_keywords.yaml"))
_OPTIN_KEYWORDS = _load_keywords("wa_optin_keywords.yaml")


@dataclass(frozen=True, slots=True)
class InboundResult:
    action: str   # opted_out | consented | intro_sent | intro_suppressed | reply | gated
    sent: bool
    phone_token: str


def _default_send(body: str, recipient_phone: str) -> str:
    from orchestrator.utils.twilio_send import send_freeform_message

    # VT-460 gap (c)+(d): the VT-287 inbound class is a CUSTOMER session send (intro / opt-in /
    # opt-out acks) — flag it so the transport's structural choke admits it (handle_customer_inbound
    # enters customer_send_context around the dispatch). is_customer_session=True also classes it
    # explicitly as the SESSION_OPTIN audit class, distinct from marketing.
    return send_freeform_message(body, recipient_phone, is_customer_session=True)


def _touch_conversation(tenant_id: str, phone_token: str, *, mark_intro: bool) -> None:
    """Upsert the per-customer marker: always bump last_inbound_at; set intro_sent_at
    once (COALESCE keeps the first value — the re-send guard)."""
    intro_clause = "now()" if mark_intro else "NULL"
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            f"""
            INSERT INTO wa_conversations (tenant_id, phone_token, intro_sent_at, last_inbound_at)
            VALUES (%s, %s, {intro_clause}, now())
            ON CONFLICT (tenant_id, phone_token) DO UPDATE SET
                last_inbound_at = now(),
                intro_sent_at = COALESCE(wa_conversations.intro_sent_at, EXCLUDED.intro_sent_at)
            """,
            (tenant_id, phone_token),
        )


def _intro_already_sent(tenant_id: str, phone_token: str) -> bool:
    with tenant_connection(tenant_id) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT intro_sent_at FROM wa_conversations "
            "WHERE tenant_id = %s AND phone_token = %s",
            (tenant_id, phone_token),
        )
        row = cur.fetchone()
    if row is None:
        return False
    val = row["intro_sent_at"] if isinstance(row, dict) else row[0]
    return val is not None


def handle_customer_inbound(
    tenant_id: UUID | str,
    customer_phone: str,
    body: str,
    *,
    consent_text_version: str = _DEFAULT_CONSENT_VERSION,
    send_fn: SendFn | None = None,
) -> InboundResult:
    """Process one customer inbound message deterministically. Returns the action taken.

    Order: STOP (always honored) → affirmative (opt-in) → established (reply) →
    first-contact (intro-once). Sends gated by the shared onboarded + WABA-live pre-gate
    (VT-460); state always recorded.
    """
    send = send_fn or _default_send
    tid = str(tenant_id)
    token = hash_phone(customer_phone)
    norm = " ".join(body.split()).casefold()

    # VT-460 gaps (b)+(d): the SESSION_OPTIN class shares the universal WABA-live pre-gate with the
    # marketing paths, AND adds the onboarded/activation pre-gate (consistency with agent +
    # campaign). This is the SESSION class — first-contact intro + opt-in/opt-out acks to a
    # not-yet-consented customer is lawful opt-in solicitation, so it is NOT folded under the
    # marketing-CONSENT gate (gap d). It STILL passes the onboarded + WABA bound. STATE (consent
    # withdrawal / opt-in / conversation marker) is recorded REGARDLESS — a STOP is never lost even
    # when the send is suppressed (the original VT-287 contract, preserved).
    from orchestrator.agents.customer_send_choke import (
        CustomerSendClass,  # noqa: F401 — names the audited class for this path (gap d)
        assert_customer_send_allowed,
    )
    from orchestrator.utils.twilio_send import customer_send_context

    with tenant_connection(tid) as _pre_conn:
        _pregate = assert_customer_send_allowed(tid, agent="sales_recovery", conn=_pre_conn)
    can_send = _pregate.allowed

    def _maybe_send(text: str) -> bool:
        if not can_send:
            logger.info(
                "VT-287 send suppressed tenant=%s token=%s class=%s reason=%s",
                tid, token, CustomerSendClass.SESSION_OPTIN.value, _pregate.reason,
            )
            return False
        # gap (c): the gated transport extent for the customer session send.
        with customer_send_context():
            send(text, customer_phone)
        return True

    # 1. STOP — withdraw consent. Always recorded (never lost), ack best-effort. VT-358:
    # boundary-safe containment on the raw body so "please STOP"/"please बंद करो" route.
    if contains_any(body, _OPT_OUT_PATTERNS):
        consent_service.opt_out(tid, token)
        _touch_conversation(tid, token, mark_intro=False)
        sent = _maybe_send("You've been opted out. Reply START to opt back in.")
        return InboundResult(action="opted_out", sent=sent, phone_token=token)

    # 2. affirmative — record the inbound opt-in (consent). Version-tracked.
    if norm in _OPTIN_KEYWORDS:
        consent_service.record_consent(
            tid, customer_phone,
            consent_text_version=consent_text_version,
            consent_method="wa_inbound_optin",
        )
        _touch_conversation(tid, token, mark_intro=False)
        sent = _maybe_send("Thanks — you're opted in. Reply STOP any time to opt out.")
        return InboundResult(action="consented", sent=sent, phone_token=token)

    # 3. established (already consented) — templated business-voice reply (v1).
    #    A customer-facing reasoning agent is DEFERRED (VT-299).
    if consent_service.has_consent(tid, token):
        _touch_conversation(tid, token, mark_intro=False)
        sent = _maybe_send("Thanks for your message — the team will get back to you shortly.")
        return InboundResult(action="reply", sent=sent, phone_token=token)

    # 4. first contact (no consent) — send the intro ONCE (re-send guard).
    if _intro_already_sent(tid, token):
        _touch_conversation(tid, token, mark_intro=False)
        return InboundResult(action="intro_suppressed", sent=False, phone_token=token)
    # Viabe disclosed as operator + automated-assistant disclosed (consent-bearing intro;
    # exact legal copy = .viabe/consent-text.md version `consent_text_version`).
    intro = (
        "Hi! This business uses Viabe (an automated assistant) to stay in touch on "
        "WhatsApp. Reply YES to get updates, or STOP to opt out."
    )
    sent = _maybe_send(intro)
    # only mark intro_sent if we actually sent it (so a not-live WABA retries the intro
    # once it goes live, rather than silently suppressing forever).
    _touch_conversation(tid, token, mark_intro=sent)
    return InboundResult(
        action="intro_sent" if sent else "gated", sent=sent, phone_token=token
    )


# --- durable entry (DBOS) ---------------------------------------------------
# The ingress starts this with SetWorkflowID(f"wa_customer_{sid}") so a Twilio
# redelivery of the same MessageSid is idempotent (DBOS skips a completed workflow).

from dbos import DBOS  # noqa: E402 — after the plain logic so tests can import it dbos-free at call sites


@DBOS.step()
def _customer_inbound_step(tenant_id: str, customer_phone: str, body: str) -> dict[str, Any]:
    res = handle_customer_inbound(tenant_id, customer_phone, body)
    return {"action": res.action, "sent": res.sent, "phone_token": res.phone_token}


@DBOS.workflow()
def customer_inbound_run(tenant_id: str, customer_phone: str, body: str) -> dict[str, Any]:
    """Durable customer-inbound processing (VT-287). One deterministic step; the
    workflow boundary gives idempotency on Twilio redelivery via the ingress's
    SetWorkflowID."""
    return _customer_inbound_step(tenant_id, customer_phone, body)


__all__ = ["InboundResult", "handle_customer_inbound", "customer_inbound_run"]
