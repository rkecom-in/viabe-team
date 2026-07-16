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
Every agent "type" is really TWO roles with DIFFERENT contracts, modelled as ``AgentRole`` values. A
module declares a SET of them (``AgentManifest.roles``, min 1): a pure ``{PROPOSER}``, a pure
``{EXECUTOR}``, or BOTH ``{PROPOSER, EXECUTOR}`` — the Sales-Recovery shape (Ruling #1): ONE module
that proposes in the conversational lane AND executes a coordinator work item, registered once and
dispatched by ``ctx.role``.

  - ``PROPOSER``  — the conversational LANE. Returns a PROPOSAL (``ModuleResult`` with ``proposal``);
                    has NO side effects. Generalizes ``AgentResult`` + ``SpecialistHandoff``. The
                    proposer lane is STRUCTURALLY side-effect-free: its ``GateFacade`` STRIPS gated
                    capabilities (``capabilities_for_role``), so even a dual-role module cannot
                    send/spend while proposing — a gated method raises ``CapabilityNotDeclared``.
  - ``EXECUTOR``  — a coordinator-dispatched LANE. Touches DB + ARMS the send gate; returns an
                    ``ItemExecutionResult``-shaped ``ModuleResult``. Generalizes ``AgentItemContext``
                    + ``ItemExecutionResult``. A gated capability is legal ONLY when ``EXECUTOR`` is a
                    declared role, and it reaches the gate ONLY through the ``GateFacade``.

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
from orchestrator.agent_framework.conformance import (
    CHECK_NAMES,
    CheckResult,
    ConformanceReport,
    assert_conforms,
    check_module_conformance,
)
from orchestrator.agent_framework.context import (
    ModuleContext,
    ModuleResult,
    TenantResolutionError,
)
from orchestrator.agent_framework.entitlement import check_entitlement
from orchestrator.agent_framework.gate_facade import (
    BusinessActionOutcome,
    CapabilityNotDeclared,
    GateFacade,
)
from orchestrator.agent_framework.manifest import AgentManifest, ManifestError
from orchestrator.agent_framework.protocols import ExecutorModule, ProposerModule
from orchestrator.agent_framework.registration import (
    AgentFrameworkRegistry,
    ModuleDispatchError,
    ModuleRegistrationError,
    RegisteredModule,
    default_registry,
    get_registered,
    register_activation_prereqs,
    register_agent,
)

# The COMPLETE public SDK surface. A module author imports EVERYTHING it needs from
# ``orchestrator.agent_framework`` — never a submodule-deep path. This list IS the contract: the
# manifest + roles + capabilities, the context/result value objects, the GateFacade (the only door
# to a gated action), the two role Protocols, ``register_agent``, and the conformance kit
# (``check_module_conformance`` / ``assert_conforms``) that verifies a module against all of it.
__all__ = [
    "CHECK_NAMES",
    "GATED_CAPABILITIES",
    "AgentFrameworkRegistry",
    "AgentManifest",
    "AgentRole",
    "BusinessActionOutcome",
    "Capability",
    "CapabilityNotDeclared",
    "CheckResult",
    "ConformanceReport",
    "ExecutorModule",
    "GateFacade",
    "ManifestError",
    "ModuleContext",
    "ModuleDispatchError",
    "ModuleRegistrationError",
    "ModuleResult",
    "ProposerModule",
    "RegisteredModule",
    "TenantResolutionError",
    "assert_conforms",
    "check_entitlement",
    "check_module_conformance",
    "default_registry",
    "get_registered",
    "is_gated",
    "register_activation_prereqs",
    "register_agent",
]
