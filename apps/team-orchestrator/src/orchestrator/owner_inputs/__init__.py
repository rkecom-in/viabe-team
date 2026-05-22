"""Owner-input substrate (VT-146).

Component 2 of the brief: structured-intent extraction writer that
classifies an inbound WhatsApp owner message via Anthropic Haiku and
writes the derived ``intent / segment / occasion`` row to
``owner_inputs``. Raw message body is consumed from the request-scoped
``WebhookEvent`` and is NEVER persisted by this module — see
``writer.write_owner_input``'s contract.
"""

from orchestrator.owner_inputs.writer import (
    OwnerInputClassification,
    classify_message,
    run_extraction_for_event,
    write_owner_input,
)

__all__ = [
    "OwnerInputClassification",
    "classify_message",
    "run_extraction_for_event",
    "write_owner_input",
]
