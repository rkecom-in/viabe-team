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
      - ``role``          — PROPOSER or EXECUTOR (``AgentRole``). Decides which impl method the
                            registration expects AND bounds the declarable capabilities.
      - ``description``   — human/LLM-readable summary (the PROPOSER's maps to a spawn-tool
                            ``description``; the EXECUTOR's is diagnostics only).
      - ``capabilities``  — the POSITIVE set of ``Capability`` this module exercises. A PROPOSER
                            MUST declare no gated capability. An EXECUTOR may declare a gated one,
                            which the ``GateFacade`` then services (and ONLY that one).
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
    role: AgentRole
    description: str
    capabilities: frozenset[Capability] = frozenset()
    prerequisites: AgentPrerequisites | None = None
    tools: tuple[Any, ...] = ()
    entitlement_key: str | None = None

    @property
    def gated_capabilities(self) -> frozenset[Capability]:
        """The subset of this manifest's capabilities that require the ``GateFacade``."""
        return frozenset(self.capabilities) & GATED_CAPABILITIES

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

        Enforces the POSITIVE-capability trust rule: a PROPOSER may declare NO gated capability —
        a proposer has no side effects BY CONTRACT, so a manifest that says otherwise is rejected
        before it can ever be handed a facade. (This is the manifest-level analogue of the
        deny-list: "declaring a forbidden capability is rejected at registration.")
        """
        if not self.name or not self.name.strip():
            raise ManifestError("manifest.name must be a non-empty string")
        if not self.version or not self.version.strip():
            raise ManifestError(f"manifest {self.name!r}: version must be non-empty")
        if not isinstance(self.role, AgentRole):
            raise ManifestError(f"manifest {self.name!r}: role must be an AgentRole")
        bad = [c for c in self.capabilities if not isinstance(c, Capability)]
        if bad:
            raise ManifestError(
                f"manifest {self.name!r}: capabilities must all be Capability values (got {bad!r})"
            )
        if self.role is AgentRole.PROPOSER and self.gated_capabilities:
            raise ManifestError(
                f"manifest {self.name!r}: a PROPOSER may declare NO gated capability, but declared "
                f"{sorted(c.value for c in self.gated_capabilities)!r}. A proposer returns a "
                "PROPOSAL with no side effects; a gated (REQUEST_*) capability is EXECUTOR-only."
            )
        if self.prerequisites is not None and self.prerequisites.agent != self.name:
            raise ManifestError(
                f"manifest {self.name!r}: prerequisites.agent={self.prerequisites.agent!r} must "
                f"equal manifest.name={self.name!r} (the activation bar keys on the agent name)"
            )


__all__ = ["AgentManifest", "ManifestError"]
