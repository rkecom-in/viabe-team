"""template_error_handler — Pre-Filter direct handler for failed template
sends (VT-3.8).

Pillar 1: fully deterministic, zero LLM.

VT-3.6 (error-handling + retry framework) is not built yet. For VT-3.8 this
handler records the failure and its retry-eligibility, then ends the workflow.
Persisting to pipeline_steps needs a workflow run_id (VT-3.3 ingress / VT-122
observability); for now the failure is recorded via structured logging.
"""

from __future__ import annotations

import logging
from typing import Any

from dbos import DBOS

from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent

logger = logging.getLogger(__name__)


@DBOS.step()
def template_error_handler(event: WebhookEvent, state: SubscriberState) -> dict[str, Any]:
    """Record a failed Twilio template send and its retry-eligibility."""
    # Template send failures are transient and retry-eligible by default;
    # VT-3.6 will replace this flag with real retry / escalation logic.
    retry_eligible = True

    logger.warning(
        "template send failed (sid=%s, tenant=%s) — retry-eligible: %s",
        event.twilio_message_sid,
        state["tenant_id"],
        retry_eligible,
    )

    return {"handler": "template_error_handler", "retry_eligible": retry_eligible}
