"""``AgentManifest`` — the capability manifest a module declares to register.

Generalizes the declarative-registration idea that already exists in three shapes
(``SpecialistSpec`` / ``AgentPrerequisites`` / ``ConnectorSpec``) into ONE manifest that spans
both agent roles. It REUSES ``AgentPrerequisites`` verbatim (does not reinvent the activation
bar): a manifest simply CARRIES the existing prereq value, so the existing
``onboarding_gate.is_agent_eligible`` can read it unchanged once a manifest is wired into the
activation registry (a deliberate later step — the framework is additive).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.agent_framework.capabilities import (
    GATED_CAPABILITIES,
    AgentRole,
    Capability,
)
from orchestrator.agents.activation_registry import AgentPrerequisites


class ManifestError(ValueError):
    """Raised by ``AgentManifest.validate`` for a structurally invalid manifest."""


@dataclass(frozen=True)
class AgentManifest:
    """The declarative contract for ONE module. Frozen value object (a code manifest, like the
    existing registries — see the package docstring for the "why code, not a DB table" call).

    Fields:
      - ``name``          — the module's stable key (its registry key AND, for an executor, must
                            equal the coordinator ``SpecialistAgent.name`` it adapts to).
      - ``version``       — the module contract version (semver-ish string). A third-party module
                            bumps this on a breaking change to its manifest.
      - ``roles``         — the SET of ``AgentRole`` this module fulfils (min 1): ``{PROPOSER}``,
                            ``{EXECUTOR}``, or BOTH ``{PROPOSER, EXECUTOR}`` (the Sales-Recovery
                            shape — one module that proposes in the conversational lane AND executes
                            a coordinator work item). Decides which impl method(s) registration
                            requires (``propose`` for PROPOSER, ``execute`` for EXECUTOR) AND bounds
                            the declarable capabilities (a gated capability requires ``EXECUTOR``).
      - ``description``   — human/LLM-readable summary (the PROPOSER's maps to a spawn-tool
                            ``description``; the EXECUTOR's is diagnostics only).
      - ``capabilities``  — the POSITIVE set of ``Capability`` this module exercises. A gated
                            (``REQUEST_*``) capability is legal ONLY when ``EXECUTOR`` is a declared
                            role — the ``GateFacade`` then services it (and ONLY it), and even for a
                            dual-role module the PROPOSER lane's facade STRIPS gated capabilities
                            (a proposal has no side effects by contract; see ``capabilities_for_role``).
      - ``prerequisites`` — the module's activation bar, REUSING ``AgentPrerequisites`` (``None`` =
                            no bar, like the advisory lanes). If set, ``prerequisites.agent`` must
                            equal ``name``.
      - ``tools``         — the module's tool surface (langchain tools / any object with ``.name``).
                            Validated against the deny-list at registration via
                            ``assert_agent_tools_safe`` — a module holding a forbidden tool is
                            rejected. Default ``()`` (a module that works purely through the context
                            contract + facade, like the reference plugin, holds no tools).
      - ``entitlement_key`` — OPEN DESIGN QUESTION (for Fazal): the ₹5000-per-agent enablement key.
                            Left OPTIONAL and UN-enforced by the framework: today per-tenant
                            enable/disable is COMPUTED (``onboarding_gate`` + ``coordinator.is_frozen``
                            + ``usage_meter.budget_status``), not a manifest field. Carried here only
                            as a forward-compat slot should entitlement become a first-class manifest
                            declaration; ``None`` = entitlement stays computed elsewhere.
    """

    name: str
    version: str
    roles: frozenset[AgentRole]
    description: str
    capabilities: frozenset[Capability] = frozenset()
    prerequisites: AgentPrerequisites | None = None
    tools: tuple[Any, ...] = ()
    entitlement_key: str | None = None

    @property
    def gated_capabilities(self) -> frozenset[Capability]:
        """The subset of this manifest's capabilities that require the ``GateFacade``."""
        return frozenset(self.capabilities) & GATED_CAPABILITIES

    def has_role(self, role: AgentRole) -> bool:
        """True iff this manifest declares ``role``."""
        return role in self.roles

    def capabilities_for_role(self, role: AgentRole) -> frozenset[Capability]:
        """The capabilities a ``GateFacade`` scoped to ``role`` will service.

        The PROPOSER lane is side-effect-free BY CONTRACT: even a DUAL-role module's proposer
        facade STRIPS the gated (``REQUEST_*``) capabilities — a proposal never sends/spends, so the
        proposer lane cannot reach a gated door regardless of what the executor lane declares. The
        EXECUTOR lane services the full declared set. This is what keeps "a proposer is structurally
        read/propose-only" true for a module that is ALSO an executor.
        """
        if role is AgentRole.PROPOSER:
            return frozenset(self.capabilities) - GATED_CAPABILITIES
        return frozenset(self.capabilities)

    def declares(self, capability: Capability) -> bool:
        """True iff this manifest positively declares ``capability``."""
        return capability in self.capabilities

    def as_prerequisites(self) -> AgentPrerequisites | None:
        """The module's activation bar for wiring into ``activation_registry.REGISTRY`` (later step).

        Returns the carried ``AgentPrerequisites`` unchanged (or ``None``). Kept as an explicit
        accessor so a future ``register_agent`` variant can populate the existing activation
        registry from the manifest WITHOUT the manifest owning the registry mutation — additive,
        so this framework never mutates the global registry at import time.
        """
        return self.prerequisites

    def validate(self) -> None:
        """Structural validation (fail-loud). The DENY-list tool check lives in ``registration``
        (it needs ``assert_agent_tools_safe``); this covers everything intrinsic to the manifest.

        Enforces the POSITIVE-capability trust rule, now ROLE-SET aware: a gated (``REQUEST_*``)
        capability is legal ONLY when ``EXECUTOR`` is among the declared roles. A pure ``{PROPOSER}``
        declaring a gated capability is rejected before it can ever be handed a facade (the
        manifest-level analogue of the deny-list). A dual ``{PROPOSER, EXECUTOR}`` module MAY declare
        a gated capability — the executor lane services it while the proposer lane strips it
        (``capabilities_for_role``).
        """
        if not self.name or not self.name.strip():
            raise ManifestError("manifest.name must be a non-empty string")
        if not self.version or not self.version.strip():
            raise ManifestError(f"manifest {self.name!r}: version must be non-empty")
        if not isinstance(self.roles, frozenset) or not self.roles:
            raise ManifestError(
                f"manifest {self.name!r}: roles must be a non-empty frozenset[AgentRole] "
                "(declare at least one of {PROPOSER, EXECUTOR})"
            )
        bad_roles = [r for r in self.roles if not isinstance(r, AgentRole)]
        if bad_roles:
            raise ManifestError(
                f"manifest {self.name!r}: roles must all be AgentRole values (got {bad_roles!r})"
            )
        bad = [c for c in self.capabilities if not isinstance(c, Capability)]
        if bad:
            raise ManifestError(
                f"manifest {self.name!r}: capabilities must all be Capability values (got {bad!r})"
            )
        if self.gated_capabilities and AgentRole.EXECUTOR not in self.roles:
            raise ManifestError(
                f"manifest {self.name!r}: gated capabilities "
                f"{sorted(c.value for c in self.gated_capabilities)!r} require the EXECUTOR role, "
                f"but roles={sorted(r.value for r in self.roles)!r}. A gated (REQUEST_*) capability "
                "is EXECUTOR-only; a pure PROPOSER returns a PROPOSAL with no side effects."
            )
        if self.prerequisites is not None and self.prerequisites.agent != self.name:
            raise ManifestError(
                f"manifest {self.name!r}: prerequisites.agent={self.prerequisites.agent!r} must "
                f"equal manifest.name={self.name!r} (the activation bar keys on the agent name)"
            )


__all__ = ["AgentManifest", "ManifestError"]
