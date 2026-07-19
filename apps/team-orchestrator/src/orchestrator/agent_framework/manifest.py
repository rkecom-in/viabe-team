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


#: VT-686 — the finite category registry every registered agent's ``manifest.category`` must be
#: drawn from (when set). Mirrors the ``CapabilityMode`` discipline elsewhere in this package: a
#: closed, code-reviewed set, not a free string — ``AgentManifest.validate()`` boot-fails on an
#: unrecognized value. Adding a category is a deliberate, reviewed code change (like adding a
#: ``Capability``), never a typo'd string a module author invents inline.
AGENT_CATEGORIES: frozenset[str] = frozenset(
    {
        "Compliance",
        "Sales",
        "Marketing",
        "Finance",
        "Accounting",
        "Onboarding",
        "Integration",
        "Tech",
        "CostOpt",
    }
)


@dataclass(frozen=True)
class AgentBrief:
    """VT-686 — the Manager-facing capability brief for an agent: STRUCTURED, not a prose blob.

    This is what lets the Manager know what an agent DOES and WHEN to delegate to it, instead of
    inferring both from a spawn-tool docstring. ``render_agent_directory`` (``agent_framework.
    directory``) turns a registry of these into the compact per-turn context card; the conformance
    ``brief_complete`` check (``agent_framework.conformance``) makes every field here REQUIRED for a
    module that wants to pass conformance (the dataclass itself stays permissive — see
    ``AgentManifest.brief``'s docstring for why).

    Fields:
      - ``what_it_does``         — 1-2 sentences: the agent's job, in plain language.
      - ``actions``              — tuple of concrete verbs/operations it performs (e.g.
                                   ``("draft_campaign", "read_lapsed_customers")``).
      - ``business_activities``  — tuple of owner-recognizable outcomes it completes (e.g.
                                   ``("win back lapsed customers",)``) — what an OWNER would call
                                   this, not internal jargon.
      - ``when_to_use``          — delegation guidance written FOR THE MANAGER: when to route a
                                   turn to this agent (and, implicitly, when not to).
      - ``limits``                — what it does NOT do — honesty-first, mirrors the capability
                                   registry's disabled/advisory entries (e.g. "does not file GST
                                   returns; readiness/prepare only"). An agent that claims NO limits
                                   fails conformance — every agent has a boundary; state it.
    """

    what_it_does: str
    actions: tuple[str, ...]
    business_activities: tuple[str, ...]
    when_to_use: str
    limits: tuple[str, ...]


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
      - ``required_tools`` — VT-669 SUFFICIENCY: the tool NAMES this module's job REQUIRES to reach
                            to deliver its outcome (the READ tools per ARCHITECTURE §1.1 + the gated
                            effect DOOR it arms). The framework enforces tool SAFETY (deny-list) but
                            never SUFFICIENCY — a specialist could silently lack a tool it needs and
                            still register clean. This POSITIVE spec closes that hole: the
                            ``required_tools_reachable`` conformance check fails-loud at boot if a
                            required tool is not in the ``tool_catalog`` OR is not reachable (neither
                            on this module's own ``tools`` surface NOR in the Manager-scoped common
                            READ set). A required tool is NEVER a raw effect — the strongest is a
                            gated ``REQUEST_*`` door reached through the ``GateFacade``. Default
                            ``()`` (a module with no hard tool dependency, like the reference plugin).
                            NOTE the SR "arm != send" nuance: SR records its required READS here while
                            keeping ``tools=()`` — its reads are Manager-scoped (common set), not
                            tools it holds; its send EFFECT is reached through the deterministic arm
                            path, not a gated tool on its manifest (VT-659 Option A).
      - ``entitlement_key`` — OPEN DESIGN QUESTION (for Fazal): the ₹5000-per-agent enablement key.
                            Left OPTIONAL and UN-enforced by the framework: today per-tenant
                            enable/disable is COMPUTED (``onboarding_gate`` + ``coordinator.is_frozen``
                            + ``usage_meter.budget_status``), not a manifest field. Carried here only
                            as a forward-compat slot should entitlement become a first-class manifest
                            declaration; ``None`` = entitlement stays computed elsewhere.
      - ``category``      — VT-686: ONE of the finite ``AGENT_CATEGORIES`` set (e.g. ``"Compliance"``,
                            ``"Sales"``). Default ``""`` — a SAFE default for back-compat during the
                            taxonomy retrofit (an un-migrated module still constructs + ``validate()``s
                            clean). ``validate()`` enforces it ONLY when non-default: a non-empty
                            category not in ``AGENT_CATEGORIES`` boot-fails. The conformance
                            ``brief_complete`` check makes a valid category REQUIRED for a module that
                            wants to pass conformance — registration is the ratchet, not the dataclass.
      - ``tags``          — VT-686: a ``frozenset[str]`` of free-vocabulary capability identifiers
                            (e.g. ``{"gst", "gstr1", "gstr3b", "returns", "filing-readiness"}``).
                            Default ``frozenset()``. ``validate()`` checks SHAPE ONLY when non-empty
                            (every tag lowercase, non-empty, no whitespace) — it does not enforce a
                            closed vocabulary (tags are free-form, unlike ``category``).
                            ``brief_complete`` requires at least one tag.
      - ``brief``          — VT-686: a structured ``AgentBrief`` (what/actions/business-activities/
                            when/limits) the Manager reads to decide WHEN to delegate here — see
                            ``AgentBrief`` for field-by-field detail. Default ``None`` (back-compat).
                            ``validate()`` only checks it IS an ``AgentBrief`` when supplied (not a
                            dict/string by mistake); ``brief_complete`` requires every field non-empty.
                            ``render_agent_directory`` (``agent_framework.directory``) skips any
                            module whose ``category``/``tags``/``brief`` are still default — only a
                            VT-686-complete manifest gets a Manager-facing directory card.
    """

    name: str
    version: str
    roles: frozenset[AgentRole]
    description: str
    capabilities: frozenset[Capability] = frozenset()
    prerequisites: AgentPrerequisites | None = None
    tools: tuple[Any, ...] = ()
    required_tools: tuple[str, ...] = ()
    entitlement_key: str | None = None
    category: str = ""
    tags: frozenset[str] = frozenset()
    brief: AgentBrief | None = None

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
        bad_required = [t for t in self.required_tools if not isinstance(t, str) or not t.strip()]
        if bad_required:
            raise ManifestError(
                f"manifest {self.name!r}: required_tools must be a tuple of non-empty tool-name "
                f"strings (got {bad_required!r}). It names the tools the module's job requires to "
                "reach (verified by the required_tools_reachable conformance check), not tool objects."
            )
        # VT-686: category/tags/brief are back-compat-DEFAULTED (an un-migrated module validates
        # clean) — but the moment one is SUPPLIED (non-default), its SHAPE is enforced here. The
        # conformance ``brief_complete`` check is the separate, stricter layer that makes all three
        # REQUIRED for a module that wants to pass conformance (see the field docstrings above).
        if self.category and self.category not in AGENT_CATEGORIES:
            raise ManifestError(
                f"manifest {self.name!r}: category={self.category!r} is not a recognized "
                f"AGENT_CATEGORIES value ({sorted(AGENT_CATEGORIES)!r}); adding a new category is a "
                "deliberate, reviewed code change (like adding a Capability), never a free string"
            )
        bad_tags = [
            t
            for t in self.tags
            if not isinstance(t, str) or not t or t != t.lower() or " " in t
        ]
        if bad_tags:
            raise ManifestError(
                f"manifest {self.name!r}: tags must be lowercase, non-empty, space-free strings "
                f"(got {bad_tags!r})"
            )
        if self.brief is not None and not isinstance(self.brief, AgentBrief):
            raise ManifestError(
                f"manifest {self.name!r}: brief must be an AgentBrief instance or None (got "
                f"{type(self.brief).__name__}) — a structured brief, not a prose blob"
            )


__all__ = ["AGENT_CATEGORIES", "AgentBrief", "AgentManifest", "ManifestError"]
