"""VT-Track-B — the modular agent-integration FRAMEWORK (the third-party contract).

WHAT THIS IS
------------
A single, documented CONTRACT that lets specialist agents register as MODULES — where
the module PROPOSES and the platform ENFORCES the trust gates. It GENERALIZES the three
agent shapes that grew independently in this codebase, without replacing or breaking any
of them (it is ADDITIVE — proven by the reference plugin + tests, not by cutting over
production routing):

  - ``agent/roster.py``            SpecialistSpec / ROSTER    — the conversational (PROPOSER) seam.
  - ``agents/coordinator.py``      SpecialistAgent / registry — the autonomous (EXECUTOR) seam.
  - ``agents/activation_registry`` AgentPrerequisites/REGISTRY — the activation bar (REUSED verbatim).
  - ``integrations/connectors``    ConnectorBase / ConnectorSpec — the customer-data seam (shape generalized).

THE TWO ROLES (the map's flagged implication #1)
------------------------------------------------
Every agent "type" is really TWO roles with DIFFERENT contracts, and this framework models
them as distinct ``AgentRole`` values rather than forcing them into one shape:

  - ``PROPOSER``  — a conversational-lane module. Returns a PROPOSAL (``ModuleResult`` with
                    ``proposal``); has NO side effects. Generalizes ``AgentResult`` +
                    ``SpecialistHandoff``. A proposer STRUCTURALLY cannot send/spend: its
                    manifest may declare no gated capability, and its ``GateFacade`` is empty.
  - ``EXECUTOR``  — a coordinator-dispatched module. Touches DB + ARMS the send gate; returns
                    an ``ItemExecutionResult``-shaped ``ModuleResult``. Generalizes
                    ``AgentItemContext`` + ``ItemExecutionResult``. Only an executor may declare
                    a gated capability, and it reaches the gate ONLY through the ``GateFacade``.

THE TRUST BOUNDARY (the map's flagged implication #3)
-----------------------------------------------------
A module is STRUCTURALLY UNABLE to send / spend / write-ledger directly. Two layers:

  1. POSITIVE capability manifest (``AgentManifest.capabilities``) — a module declares exactly
     what it may do. There is NO capability value that means "send directly": the only
     send-shaped capability, ``REQUEST_CUSTOMER_SEND``, is SERVICED by the ``GateFacade``, which
     routes to the EXISTING ``customer_send.agent_send_draft`` Gate 0 stack. You cannot
     manifest your way to a raw transport.
  2. DENY-list tool-surface check (REUSES ``tool_guardrail.assert_agent_tools_safe``) — a module
     that HOLDS a forbidden tool (a ``send_whatsapp_*`` / ``write_ledger`` / accounts-book tool)
     is rejected at registration, exactly as every existing agent surface is.

The ``GateFacade`` is the ONLY door to a gated action. It is capability-scoped from the
manifest: a gated method the manifest did not declare raises ``CapabilityNotDeclared`` — so a
plugin (including a future third-party one) can never touch ``customer_send`` /
``business_impact_choke`` / ``twilio_send`` directly.

WHY CODE MANIFESTS (not a DB table)
-----------------------------------
Same call as ``roster.py`` / ``activation_registry.py`` / ``integrations/registry.py`` (all
cite it): a module's contract is part of the PRODUCT's behavioral contract — version-controlled,
diffable, unit-testable at boot. No migration, no RLS surface, no deploy-vs-data skew.
"""

from __future__ import annotations

from orchestrator.agent_framework.capabilities import (
    GATED_CAPABILITIES,
    AgentRole,
    Capability,
    is_gated,
)
from orchestrator.agent_framework.context import (
    ModuleContext,
    ModuleResult,
    TenantResolutionError,
)
from orchestrator.agent_framework.gate_facade import (
    CapabilityNotDeclared,
    GateFacade,
)
from orchestrator.agent_framework.manifest import AgentManifest
from orchestrator.agent_framework.protocols import ExecutorModule, ProposerModule
from orchestrator.agent_framework.registration import (
    AgentFrameworkRegistry,
    ModuleRegistrationError,
    RegisteredModule,
    default_registry,
    get_registered,
    register_agent,
)

__all__ = [
    "GATED_CAPABILITIES",
    "AgentFrameworkRegistry",
    "AgentManifest",
    "AgentRole",
    "Capability",
    "CapabilityNotDeclared",
    "ExecutorModule",
    "GateFacade",
    "ModuleContext",
    "ModuleRegistrationError",
    "ModuleResult",
    "ProposerModule",
    "RegisteredModule",
    "TenantResolutionError",
    "default_registry",
    "get_registered",
    "is_gated",
    "register_agent",
]
