"""VT-349 — free-form owner acks (in-window session replies, NOT templates).

Fazal ruling 2026-06-06: a WhatsApp template is only required to message OUTSIDE the 24h
customer-service window. Three owner-facing messages are DIRECT REPLIES to a message the owner
just sent → in-window → they send as free-form session messages (send_freeform_message / VT-44),
not Meta templates. This module holds the bilingual copy + the owner-locale resolution + the
single best-effort send that NEVER crashes the handler (a window-closed / failed ack must not
unwind the owner-action that already landed).

- team_edge_case_ack: NO fixed copy here — the handler computes the locale-aware reply text and
  passes it straight to send_freeform_ack.
- support_handoff / refund_processing: fixed copy, bilingual, in ACK_COPY (a dict so VT-329 can
  extend cleanly — not inline if/else). Latin tokens (Viabe / ₹ / numbers) kept in both langs.
"""

from __future__ import annotations

import logging
from uuid import UUID

logger = logging.getLogger(__name__)

# 24h-window-closed Twilio error: a free-form send to a number whose inbound window lapsed.
_WINDOW_CLOSED_CODE = 63016

# Fazal-approved copy (2026-06-06). {ref} = run id; {amt} = ₹ amount (Indian-grouped, no symbol).
ACK_COPY: dict[str, dict[str, str]] = {
    "support_handoff": {
        "en": (
            "Thanks for your message. This one needs a human, so I've flagged it to a customer "
            "service representative, who will follow up with you personally. Your reference is "
            "{ref} if you need to mention it."
        ),
        "hi": (
            "आपके संदेश के लिए धन्यवाद। इसके लिए किसी व्यक्ति की ज़रूरत है, इसलिए मैंने इसे हमारे ग्राहक सेवा "
            "प्रतिनिधि को भेज दिया है, जो आपसे व्यक्तिगत रूप से संपर्क करेंगे। ज़रूरत होने पर आपका रेफरेंस {ref} है।"
        ),
        # VT-677 D1: the hi-Latn register for hinglish-preference owners — SAME approved content as
        # 'hi', romanized (free-form is in-window: no Meta constraint).
        "hinglish": (
            "Aapke message ke liye dhanyavaad. Iske liye ek insaan ki zaroorat hai, isliye maine "
            "ise hamare customer service representative ko bhej diya hai — woh aapse personally "
            "follow up karenge. Zaroorat ho toh aapka reference {ref} hai."
        ),
    },
    "refund_processing": {
        "en": (
            "Your refund of ₹{amt} is being processed. It should reach your original payment "
            "method within 5 business days. I'll confirm once it's done."
        ),
        "hi": (
            "आपका ₹{amt} का रिफंड प्रोसेस किया जा रहा है। यह 5 कार्य-दिवसों के भीतर आपके मूल पेमेंट मेथड में "
            "पहुँच जाना चाहिए। पूरा होने पर मैं पुष्टि कर दूँगा।"
        ),
        "hinglish": (
            "Aapka ₹{amt} ka refund process ho raha hai. Yeh 5 business days ke andar aapke "
            "original payment method mein pahunch jaana chahiye. Poora hote hi main confirm kar "
            "dunga."
        ),
    },
}

# VT-677: the locale resolver is now CANONICAL in owner_surface.owner_locale (en|hinglish|hi value
# space, explicit→observed→en precedence). Re-exported here so the 8 pre-VT-677 call sites keep
# working unchanged; new code imports from owner_locale directly.
from orchestrator.owner_surface.owner_locale import (  # noqa: E402
    resolve_owner_locale as resolve_owner_locale,
)


def ack_body(kind: str, locale: str, **fmt: str) -> str:
    """Resolve + format a fixed-copy ack body. `locale` must be supported; falls back to en."""
    variants = ACK_COPY[kind]
    template = variants.get(locale) or variants["en"]
    return template.format(**fmt)


def send_freeform_ack(
    tenant_id: UUID | str, recipient_phone: str | None, body: str
) -> bool:
    """Send a free-form in-window owner ack. Best-effort + fail-safe: a window-closed (63016)
    or any other send error is logged and SWALLOWED — the owner-action (exclusion / escalation
    / refund) already landed and must not be unwound by an ack send. Returns True on a sent
    message, False otherwise (no raise)."""
    if not recipient_phone:
        logger.info("VT-349 ack: no recipient tenant=%s — skipping", tenant_id)
        return False
    try:
        from orchestrator.utils.twilio_send import send_freeform_message

        # VT-579: this IS an owner-facing send (an in-window ack/reply from the manager surface). Pass
        # tenant_id so the transport records it into the lifetime conversation log (the 'assistant' leg);
        # the recording itself lives at the transport chokepoint (twilio_send), we only supply the scope.
        send_freeform_message(body, recipient_phone, tenant_id=tenant_id, surface="manager")
        return True
    except Exception as exc:  # noqa: BLE001 — the ack must never crash the handler
        code = getattr(exc, "code", None)
        if code == _WINDOW_CLOSED_CODE:
            logger.info(
                "VT-349 ack: 24h window closed (63016) tenant=%s — owner-action still applied",
                tenant_id,
            )
        else:
            logger.exception("VT-349 ack send failed tenant=%s code=%s", tenant_id, code)
        return False
