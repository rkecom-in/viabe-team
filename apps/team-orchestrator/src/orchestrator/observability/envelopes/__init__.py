"""VT-179 typed-envelope registry for pipeline_steps step_kinds.

Maps step_kind string → Pydantic envelope class. Consumed by:
- VT-180 writer: ``envelope_for(step_kind)`` to validate args at write time
- VT-181 decorator: same lookup at call time
- VT-186 CI gate: ``validate_registry_completeness()`` at boot

Boot-time drift detection runs at import (via orchestrator.__init__.py).
Adding a new step_kind to source code WITHOUT registering it here = boot
failure. Adding here without using it = registry-only entry, harmless but
gets flagged by VT-186's CI gate.

Per CL-417: registry keys (step_kind strings) are CANONICAL — they match
the values written to ``pipeline_steps.step_kind`` on-main. Legacy names
were renamed under VT-179 Option A.
"""

from __future__ import annotations

import re
from pathlib import Path

from .agent_invocation import AgentInvocationEnvelope, AgentInvocationInput
from .agent_reasoning_step import (
    AgentReasoningStepEnvelope,
    AgentReasoningStepInput,
    AgentReasoningStepOutput,
)
from .attribution_match import (
    AttributionMatchEnvelope,
    AttributionMatchInput,
    AttributionMatchOutput,
)
from .base import StepEnvelope, StepStatus
from .context_truncation import (
    ContextTruncationEnvelope,
    ContextTruncationInput,
)
from .campaign_plan_emitted import (
    CampaignPlanEmittedEnvelope,
    CampaignPlanEmittedInput,
    CampaignPlanEmittedOutput,
    CampaignPlanVariant,
)
from .day39_evaluator import (
    Day39EvaluatorEnvelope,
    Day39EvaluatorInput,
    Day39EvaluatorOutput,
)
from .dsr_processed import (
    DsrProcessedEnvelope,
    DsrProcessedInput,
    DsrProcessedOutput,
)
from .error import (
    ErrorEnvelope,
    ErrorInput,
    ErrorOutput,
    FailureType,
    Strategy,
)
from .mcp_tool_call import (
    McpToolCallEnvelope,
    McpToolCallInput,
    McpToolCallOutput,
)
from .message_dispatch import (
    MessageDispatchEnvelope,
    MessageDispatchInput,
    MessageDispatchOutput,
)
from .opt_out_processed import (
    OptOutProcessedEnvelope,
    OptOutProcessedInput,
    OptOutProcessedOutput,
)
from .refund_decision import (
    RefundDecisionEnvelope,
    RefundDecisionInput,
    RefundDecisionOutput,
)
from .self_evaluate_gate import (
    SelfEvaluateGateEnvelope,
    SelfEvaluateGateInput,
    SelfEvaluateGateOutput,
    SelfEvaluateVerdict,
)
from .state_transition import (
    StateTransitionEnvelope,
    StateTransitionInput,
)
from .tenant_isolation_breach import (
    TenantIsolationBreachEnvelope,
    TenantIsolationBreachInput,
)
from .webhook_classified import (
    WebhookClassifiedEnvelope,
    WebhookClassifiedInput,
    WebhookClassifiedOutput,
)
from .webhook_received import (
    WebhookReceivedEnvelope,
    WebhookReceivedInput,
)


STEP_KIND_REGISTRY: dict[str, type[StepEnvelope]] = {
    "webhook_received": WebhookReceivedEnvelope,
    "webhook_classified": WebhookClassifiedEnvelope,
    "state_transition": StateTransitionEnvelope,
    "agent_invocation": AgentInvocationEnvelope,
    "agent_reasoning_step": AgentReasoningStepEnvelope,
    "mcp_tool_call": McpToolCallEnvelope,
    "self_evaluate_gate": SelfEvaluateGateEnvelope,
    "campaign_plan_emitted": CampaignPlanEmittedEnvelope,
    "message_dispatch": MessageDispatchEnvelope,
    "attribution_match": AttributionMatchEnvelope,
    "day39_evaluator": Day39EvaluatorEnvelope,
    "refund_decision": RefundDecisionEnvelope,
    "opt_out_processed": OptOutProcessedEnvelope,
    "dsr_processed": DsrProcessedEnvelope,
    "error": ErrorEnvelope,
    "context_truncation": ContextTruncationEnvelope,
    "tenant_isolation_breach": TenantIsolationBreachEnvelope,
}


class EnvelopeNotRegistered(Exception):
    """Raised when ``envelope_for(step_kind)`` is called with an unknown kind."""


class EnvelopeRegistryDrift(Exception):
    """Raised at boot when source-code step_kind literals diverge from
    STEP_KIND_REGISTRY keys (VT-186 CI gate substrate)."""


def envelope_for(step_kind: str) -> type[StepEnvelope]:
    """Look up the Pydantic envelope class for a step_kind string."""
    if step_kind not in STEP_KIND_REGISTRY:
        raise EnvelopeNotRegistered(step_kind)
    return STEP_KIND_REGISTRY[step_kind]


# step_kind=<literal> in single OR double quotes; captures the literal.
_STEP_KIND_LITERAL_RE = re.compile(
    r"""step_kind\s*=\s*['"]([a-z][a-z0-9_]*)['"]"""
)


def _collect_step_kind_literals(source_root: Path) -> set[str]:
    """Walk source_root, return every step_kind=<literal> value found.

    Skips the envelopes/ package itself (the registry IS the source of
    truth for class-level ``step_kind`` ClassVars).
    """
    envelopes_pkg = source_root / "orchestrator" / "observability" / "envelopes"
    found: set[str] = set()
    for path in source_root.rglob("*.py"):
        if envelopes_pkg in path.parents or path == envelopes_pkg / "__init__.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _STEP_KIND_LITERAL_RE.finditer(text):
            found.add(match.group(1))
    return found


def validate_registry_completeness(source_root: Path | None = None) -> None:
    """Boot-time check: every step_kind=<literal> in source has a registry entry.

    Raises EnvelopeRegistryDrift listing the unregistered literals.
    """
    if source_root is None:
        source_root = Path(__file__).resolve().parents[3]
    literals = _collect_step_kind_literals(source_root)
    missing = sorted(literals - STEP_KIND_REGISTRY.keys())
    if missing:
        raise EnvelopeRegistryDrift(
            f"unregistered step_kind literals in source: {missing}; "
            f"either register them in STEP_KIND_REGISTRY or remove the literals"
        )


__all__ = [
    # Registry + errors + helpers
    "STEP_KIND_REGISTRY",
    "EnvelopeNotRegistered",
    "EnvelopeRegistryDrift",
    "envelope_for",
    "validate_registry_completeness",
    # Base
    "StepEnvelope",
    "StepStatus",
    # Envelope classes
    "WebhookReceivedEnvelope",
    "WebhookClassifiedEnvelope",
    "StateTransitionEnvelope",
    "AgentInvocationEnvelope",
    "AgentReasoningStepEnvelope",
    "McpToolCallEnvelope",
    "SelfEvaluateGateEnvelope",
    "CampaignPlanEmittedEnvelope",
    "MessageDispatchEnvelope",
    "AttributionMatchEnvelope",
    "Day39EvaluatorEnvelope",
    "RefundDecisionEnvelope",
    "OptOutProcessedEnvelope",
    "DsrProcessedEnvelope",
    "ErrorEnvelope",
    "ContextTruncationEnvelope",
    "TenantIsolationBreachEnvelope",
    # Input/Output sub-models
    "WebhookReceivedInput",
    "WebhookClassifiedInput",
    "WebhookClassifiedOutput",
    "StateTransitionInput",
    "AgentInvocationInput",
    "AgentReasoningStepInput",
    "AgentReasoningStepOutput",
    "McpToolCallInput",
    "McpToolCallOutput",
    "SelfEvaluateGateInput",
    "SelfEvaluateGateOutput",
    "SelfEvaluateVerdict",
    "CampaignPlanEmittedInput",
    "CampaignPlanEmittedOutput",
    "CampaignPlanVariant",
    "MessageDispatchInput",
    "MessageDispatchOutput",
    "AttributionMatchInput",
    "AttributionMatchOutput",
    "Day39EvaluatorInput",
    "Day39EvaluatorOutput",
    "RefundDecisionInput",
    "RefundDecisionOutput",
    "OptOutProcessedInput",
    "OptOutProcessedOutput",
    "DsrProcessedInput",
    "DsrProcessedOutput",
    "ErrorInput",
    "ErrorOutput",
    "FailureType",
    "Strategy",
    "ContextTruncationInput",
    "TenantIsolationBreachInput",
]
