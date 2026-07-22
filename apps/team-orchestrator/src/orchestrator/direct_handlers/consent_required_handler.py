"""consent_required_handler — VT-303 Option-B graceful degrade.

Fires when an inbound owner message would route to the brain but the tenant has
NOT enabled ``owner_inputs`` (the lawful basis for transmitting the owner's
message — which may carry customer PII — to Anthropic, CL-425). Instead of
transmitting, we send a conservative NON-LLM reply pointing the owner to the
enable path. No Anthropic call happens.

Pillar 1: fully deterministic, zero LLM.
Pillar 7 (owner-truth): the prompt send is the honest outcome (send_result),
never a hardcoded claim.
CL-390: the send util hashes the recipient phone in logs; we never log it raw.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS

from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent
from orchestrator.utils.twilio_send import send_freeform_message

# The enable phrase the owner must send back. Kept in sync with the FIRST entry
# of config/data_inputs_enable_keywords.yaml (the canonical, human-facing grant
# phrase). Surfaced here so the prompt tells the owner exactly what to send.
_ENABLE_PHRASE = "ACTIVATE TEAM"

# TODO(VT-272): final consent copy — this is INTERIM draft wording to unblock the
# gate. The consent-bearing language MUST be swapped to the VT-272/legal-validated
# text before real-customer go-live (same posture as consent-text.md). Cowork
# 20260603T144500Z.
# VT-700 (Fazal 2026-07-23): the ask INTRODUCES the Manager (the owner hired this team —
# never a faceless gate) and goes out with a tappable ACTIVATE TEAM button. The
# consent-bearing sentences (what enabling means + STOP) are carried over verbatim.
_CONSENT_PROMPT = (
    "Hi! I'm your Manager — the head of your new Viabe AI team. I run business tasks "
    "for you right here on WhatsApp: tracking sales, winning back lapsed customers, "
    "drafting campaigns, and flagging what needs your attention. You stay in charge — "
    "nothing goes to a customer without your approval.\n\n"
    "Before I can read your business data and start working, I need your go-ahead.\n\n"
    f"Tap *{_ENABLE_PHRASE}* to enable data inputs and activate your team.\n\n"
    "Enabling lets your AI team process your messages and customer data to "
    "recover sales for you. You can pause anytime by replying STOP."
)

# VT-700 — the ask rides this interactive object (button titles: the EXACT grant phrase +
# "Not now"); freeform fallback below keeps delivery unconditional.
_ACTIVATE_TEMPLATE = "team_activate_button"

# full-77 cluster-5 (sr_consent_decline_then_explicit, §2 3/3) — when the owner DECLINES the consent
# ask ("no thanks, not right now"), re-sending _CONSENT_PROMPT verbatim ignores the decline (a
# loop_stall + ignored_speech_act breaker). Prepend a one-line acknowledgment so the reply reflects
# the decline, while KEEPING the full prompt (incl. the ACTIVATE TEAM phrase) intact — the runner's
# consent gate still recognises the ask, and the exact-keyword floor still works on the next turn.
# Zero LLM (classify_consent_intent is deterministic) — Pillar-1 unchanged.
_DECLINE_ACK = (
    "No problem — nothing's changed and your data stays private. Whenever you're ready:\n\n"
)


@DBOS.step()
def consent_required_handler(
    event: WebhookEvent, state: SubscriberState
) -> dict[str, Any]:
    """Send the conservative enable-prompt; never transmit to the brain."""
    from orchestrator.pre_filter_gate import classify_consent_intent

    sid: str | None = None
    error: str | None = None
    recipient = event.sender_phone or None
    # cluster-5 — acknowledge an explicit decline instead of re-pushing the prompt verbatim.
    prompt = _CONSENT_PROMPT
    if classify_consent_intent(getattr(event, "body", "") or "") == "decline":
        prompt = _DECLINE_ACK + _CONSENT_PROMPT
    if recipient is not None:
        try:
            # VT-583 — record this send into the lifetime log (surface='system'; the mig-164 CHECK allows
            # journey|manager|system) so the runner's consent gate can recognise it: the prompt contains
            # the enable phrase (_ENABLE_PHRASE), which uniquely marks the consent ASK. A plain
            # affirmation on the NEXT inbound then routes to the same audited enable path. Pillar-1
            # unchanged: still zero LLM.
            # VT-700 — interactive-first: the full prompt rides {{1}} (the var-1 recording keeps
            # the enable phrase in conversation_log), buttons = ACTIVATE TEAM / Not now. Any
            # failure falls back to the freeform prompt — delivery is unconditional.
            try:
                from orchestrator.templates_registry import content_sid_for
                from orchestrator.utils.twilio_send import send_interactive_message

                content_sid = content_sid_for(_ACTIVATE_TEMPLATE, "en")
                if content_sid:
                    sid = send_interactive_message(
                        content_sid,
                        recipient,
                        content_variables={"1": prompt},
                        tenant_id=state["tenant_id"],
                        surface="system",
                    )
            except Exception:  # noqa: BLE001 — buttons are an enhancement; freeform below
                sid = None
            if sid is None:
                sid = send_freeform_message(
                    prompt,
                    recipient,
                    tenant_id=state["tenant_id"],
                    surface="system",
                )
        except Exception as exc:  # noqa: BLE001 — honest send outcome, never crash the pipeline
            error = repr(exc)
    else:
        error = "no recipient phone on event"

    return {
        "handler": "consent_required_handler",
        "consent_prompt_sent": sid is not None,
        "send_result": {"sid": sid, "error": error},
    }
