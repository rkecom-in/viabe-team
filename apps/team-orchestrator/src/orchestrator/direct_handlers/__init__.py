"""Direct-handler registry for the Pre-Filter Gate (VT-3.8).

Maps ``handler_name`` (as returned in ``RouteToDirectHandler``) to its handler.
Pillar 8: one registry, one set of handlers — no shadow filtering elsewhere.
Pillar 1: every handler is fully deterministic — zero LLM.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orchestrator.direct_handlers.dsr_handler import dsr_handler
from orchestrator.direct_handlers.dupe_handler import dupe_handler
from orchestrator.direct_handlers.opt_out_handler import opt_out_handler
from orchestrator.direct_handlers.status_ping_handler import status_ping_handler
from orchestrator.direct_handlers.template_error_handler import template_error_handler
from orchestrator.types import Tenant, WebhookEvent

Handler = Callable[[WebhookEvent, Tenant], dict[str, Any]]

HANDLERS: dict[str, Handler] = {
    "opt_out_handler": opt_out_handler,
    "dsr_handler": dsr_handler,
    "dupe_handler": dupe_handler,
    "status_ping_handler": status_ping_handler,
    "template_error_handler": template_error_handler,
}
