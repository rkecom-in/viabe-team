"""Direct-handler registry for the Pre-Filter Gate (VT-3.8).

Maps ``handler_name`` (as returned in ``RouteToDirectHandler``) to its handler.
Pillar 8: one registry, one set of handlers — no shadow filtering elsewhere.
Pillar 1: every handler is fully deterministic — zero LLM.

Return contract (VT-3.3c)
-------------------------
Every handler returns a ``dict`` of the shape::

    {"handler": <name>, "<side_effect_flag>": <value>, "send_result": {...}}

``send_result`` is ``SendResult.model_dump()`` from ``utils.twilio_send`` — the
honest outcome of the Twilio template send (Pillar 7). It replaces the old
hardcoded ``confirmation_sent``/``acknowledgment_sent``/``reply_sent``: True
booleans, which lied about a send that had not happened (audit C4, CL-74).
``dupe_handler`` is the one exception — it sends nothing and has no
``send_result``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orchestrator.direct_handlers.autonomy_enable_handler import autonomy_enable_handler
from orchestrator.direct_handlers.autonomy_kill_handler import autonomy_kill_handler
from orchestrator.direct_handlers.consent_required_handler import (
    consent_required_handler,
)
from orchestrator.direct_handlers.customer_send_delivery_handler import (
    customer_send_delivery_handler,
)
from orchestrator.direct_handlers.data_inputs_enable_handler import (
    data_inputs_enable_handler,
)
from orchestrator.direct_handlers.dsr_handler import dsr_handler
from orchestrator.direct_handlers.dupe_handler import dupe_handler
from orchestrator.direct_handlers.opt_out_handler import opt_out_handler
from orchestrator.direct_handlers.status_ping_handler import status_ping_handler
from orchestrator.direct_handlers.template_error_handler import template_error_handler
from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent

Handler = Callable[[WebhookEvent, SubscriberState], dict[str, Any]]

HANDLERS: dict[str, Handler] = {
    "opt_out_handler": opt_out_handler,
    "dsr_handler": dsr_handler,
    "dupe_handler": dupe_handler,
    "status_ping_handler": status_ping_handler,
    "template_error_handler": template_error_handler,
    # VT-303 — owner_inputs consent gate (Option B) + enable path.
    "consent_required_handler": consent_required_handler,
    "data_inputs_enable_handler": data_inputs_enable_handler,
    # VT-564 — customer-send delivery reconciliation (delivered/read/undelivered callbacks).
    "customer_send_delivery_handler": customer_send_delivery_handler,
    # VT-384 — L3 autonomy keyword paths (pre_filter rules b2/b3, AFTER opt-out + DSR).
    "autonomy_kill_handler": autonomy_kill_handler,
    "autonomy_enable_handler": autonomy_enable_handler,
}
