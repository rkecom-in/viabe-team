"""The POSITIVE capability model + the two agent ROLES.

The existing codebase enforces the trust boundary with a DENY-list only
(``tool_guardrail.FORBIDDEN_CAPABILITY_SUBSTRINGS`` — "you may hold no send / ledger / accounts
tool"). This module adds the missing POSITIVE half: a module declares exactly WHICH capabilities
it exercises. The two halves are complementary — a module must (a) positively declare every
capability it uses AND (b) hold no forbidden tool.

THE DESIGN INVARIANT that makes the positive model safe by construction: **no ``Capability``
value means "send/spend directly."** The only side-effecting capabilities are the ``REQUEST_*``
family, and each is SERVICED exclusively by the ``GateFacade`` (which calls the existing
deterministic gates). A module cannot manifest its way to a raw transport — the strongest
capability it can declare is "ask the platform, through the facade, to run a GATED action," and
the platform still decides autonomous-vs-approval. This is what makes a future third-party agent
safe: its manifest is an upper bound the facade enforces, not a grant of raw power.
"""

from __future__ import annotations

from enum import Enum


class AgentRole(str, Enum):
    """The two distinct contracts every agent "type" splits into (map finding #1).

    A ``str`` enum so a manifest value serializes to a stable machine token (logs / tests pin it).
    """

    #: Conversational-lane module. Returns a PROPOSAL, NO side effects. Generalizes the
    #: ``run_sales_recovery_agent`` -> ``AgentResult`` path + the ``SpecialistHandoff`` input.
    PROPOSER = "proposer"
    #: Coordinator-dispatched module. Touches DB + ARMS the send gate through the facade; returns
    #: an ``ItemExecutionResult``-shaped result. Generalizes the ``SalesRecoveryAgent.execute_item``
    #: (``AgentItemContext`` -> ``ItemExecutionResult``) path.
    EXECUTOR = "executor"


class Capability(str, Enum):
    """The positively-declarable capabilities. A ``str`` enum for stable serialization.

    Two families:

      * NON-GATED (``READ_*`` / ``PROPOSE_*``): safe capabilities with no irreversible side effect
        — reads, and PROPOSALS (an intent/draft handed back, never executed). A PROPOSER lives
        entirely here.
      * GATED (``REQUEST_*``, listed in ``GATED_CAPABILITIES``): a request to run a consequential
        action. NEVER performs the action itself — it is serviced by the ``GateFacade``, which
        routes to the existing deterministic gate (``customer_send.agent_send_draft`` /
        ``business_impact_choke.assert_or_gate_business_action``). Only an EXECUTOR may declare one.

    Adding a capability is a deliberate, reviewed code change (like adding a roster entry). A NEW
    gated capability MUST also be added to ``GATED_CAPABILITIES`` AND given a ``GateFacade`` method
    that routes to a real gate — never a direct transport.
    """

    # --- NON-GATED: reads (no effect) ---
    READ_BUSINESS_CONTEXT = "read_business_context"
    READ_CUSTOMER_LEDGER = "read_customer_ledger"
    READ_INTEGRATION_STATE = "read_integration_state"

    # --- NON-GATED: proposals (an intent/draft; no effect, no persist, no send) ---
    PROPOSE_DRAFT = "propose_draft"
    PROPOSE_CAMPAIGN = "propose_campaign"
    PROPOSE_CONFIG_CHANGE = "propose_config_change"
    PROPOSE_BUSINESS_ACTION = "propose_business_action"

    # --- GATED: serviced ONLY by the GateFacade (EXECUTOR-only) ---
    #: Ask the platform to send an already-persisted draft. Routes to
    #: ``customer_send.agent_send_draft`` — the full fail-closed Gate 0..5 stack. The module never
    #: touches Twilio / ``customer_send_context``; it names a draft_id, the platform gates + sends.
    REQUEST_CUSTOMER_SEND = "request_customer_send"
    #: Ask the platform to gate a consequential business action (SPEND / COMMITMENT / CONFIG).
    #: Routes to ``business_impact_choke.assert_or_gate_business_action`` — the deterministic
    #: autonomous-vs-owner-approval decision + the ``business_action_context`` transport choke.
    REQUEST_BUSINESS_ACTION = "request_business_action"


#: The gated capabilities — declarable only by an EXECUTOR, serviced only by the ``GateFacade``.
#: The single source of truth for "which capabilities need a gate"; the facade + the manifest
#: validator both read it, so a new gated capability is enforced in ONE place.
GATED_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.REQUEST_CUSTOMER_SEND,
        Capability.REQUEST_BUSINESS_ACTION,
    }
)


def is_gated(capability: Capability) -> bool:
    """True iff ``capability`` is a gated (``REQUEST_*``) capability the facade must service."""
    return capability in GATED_CAPABILITIES


__all__ = [
    "GATED_CAPABILITIES",
    "AgentRole",
    "Capability",
    "is_gated",
]
