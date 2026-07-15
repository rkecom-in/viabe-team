"""Registration — the ONE surface a module registers through, + the role adapters.

Generalizes ``SpecialistSpec``/``ROSTER`` (proposer registration) and the coordinator
``SpecialistAgent`` registry (executor registration) into ONE ``AgentFrameworkRegistry`` that
holds both roles. It is ADDITIVE: this registry is SEPARATE from ``ROSTER`` and the coordinator's
``_REGISTRY_SPEC`` — registering here does NOT wire a module into the live supervisor graph or the
daily sweep. The adapters (``as_specialist_spec`` doc-mapping + ``CoordinatorAgentAdapter``) are how
a registered module GRADUATES into a live seam, on a deliberate, reviewed later step (a Fazal
who-does-it call), never implicitly at import.

REGISTRATION VALIDATION (fail-loud, both layers of the trust boundary):
  1. ``manifest.validate()``            — structural + POSITIVE-capability rule (a proposer may
                                          declare no gated capability).
  2. ``assert_agent_tools_safe(tools)`` — the EXISTING deny-list: a module holding a forbidden
                                          send/ledger/accounts tool is REJECTED (reused verbatim).
  3. role/impl conformance              — a PROPOSER impl must expose ``propose``; an EXECUTOR impl
                                          ``execute``. (The manifest.role and the code must agree.)
  4. name uniqueness                    — one module per name in a registry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from orchestrator.agent_framework.capabilities import ROLE_METHOD, AgentRole
from orchestrator.agent_framework.context import ModuleContext, ModuleResult
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.manifest import AgentManifest, ManifestError

logger = logging.getLogger("orchestrator.agent_framework.registration")


class ModuleRegistrationError(RuntimeError):
    """Raised when a module fails any registration check (validation / deny-list / conformance)."""


class ModuleDispatchError(RuntimeError):
    """Raised by ``RegisteredModule.run`` when a context's role is not one the module declares.

    A dual-role module dispatched with either role runs; a pure PROPOSER handed an EXECUTOR context
    (or vice-versa) is a caller/wiring bug — fail loud rather than silently mis-dispatch."""


@dataclass(frozen=True)
class RegisteredModule:
    """A validated module: its manifest + its impl (a ``ProposerModule`` or ``ExecutorModule``)."""

    manifest: AgentManifest
    impl: Any

    def new_gate(self, ctx: ModuleContext) -> GateFacade:
        """Build the capability-scoped ``GateFacade`` for a run of this module against ``ctx``.

        The facade's tenant is the context's IDOR-resolved tenant; its capabilities are the
        manifest's declared set SCOPED TO ``ctx.role`` (``capabilities_for_role``). The PROPOSER lane
        strips gated capabilities even for a dual-role module, so a proposer's facade refuses every
        gated method — the structural read/propose-only guarantee holds regardless of the module's
        executor lane. The EXECUTOR lane services the full declared set.
        """
        return GateFacade(
            tenant_id=ctx.tenant_id,
            capabilities=self.manifest.capabilities_for_role(ctx.role),
            run_id=ctx.run_id,
        )

    def run(self, ctx: ModuleContext) -> ModuleResult:
        """Invoke the module for ``ctx`` (dispatching by ``ctx.role`` to ``propose`` / ``execute``).

        This is the framework's uniform driver: assert the context's role is one the module
        declares, build the role-scoped facade, call the matching role method, return the module's
        ``ModuleResult``. A dual-role module dispatches to ``propose`` under a PROPOSER context and
        ``execute`` under an EXECUTOR context — the SAME registered instance, selected by the lane.
        Raises ``ModuleDispatchError`` if ``ctx.role`` is not among the module's declared roles.
        """
        if ctx.role not in self.manifest.roles:
            raise ModuleDispatchError(
                f"module {self.manifest.name!r} dispatched with role={ctx.role.value!r}, but "
                f"declares roles={sorted(r.value for r in self.manifest.roles)!r} — refusing to run."
            )
        gate = self.new_gate(ctx)
        return getattr(self.impl, ROLE_METHOD[ctx.role])(ctx, gate)


class AgentFrameworkRegistry:
    """An in-memory registry of framework modules (keyed by manifest name). Additive — separate
    from ``ROSTER`` + the coordinator registry (see the module docstring)."""

    def __init__(self) -> None:
        self._modules: dict[str, RegisteredModule] = {}

    def register(self, impl: Any) -> RegisteredModule:
        """Validate + register a module instance. Raises ``ModuleRegistrationError`` on any failure.

        ``impl`` must carry a ``.manifest`` (``AgentManifest``) and the role method its manifest
        declares (``propose`` for a PROPOSER, ``execute`` for an EXECUTOR).
        """
        manifest = getattr(impl, "manifest", None)
        if not isinstance(manifest, AgentManifest):
            raise ModuleRegistrationError(
                f"module {impl!r} has no AgentManifest 'manifest' attribute"
            )

        # 1. structural + positive-capability validation.
        try:
            manifest.validate()
        except ManifestError as exc:
            raise ModuleRegistrationError(str(exc)) from exc

        # 2. deny-list: the module's tool surface holds no forbidden send/ledger/accounts tool.
        #    REUSES the existing graph-build guard verbatim — a module is held to the EXACT same
        #    capability boundary as every hand-wired agent surface. Imported LAZILY here (not at
        #    module top): ``orchestrator.agent.__init__`` eager-imports the langchain orchestrator
        #    agent, so a top-level import would pull langchain into the framework's import surface and
        #    break the dep-less smoke. Registration is a runtime path where the full deps are present.
        from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

        try:
            assert_agent_tools_safe(
                manifest.tools, surface=f"agent_framework:{manifest.name}"
            )
        except Exception as exc:  # ToolGuardrailViolation (+ any tool-introspection failure)
            raise ModuleRegistrationError(
                f"module {manifest.name!r}: tool surface rejected by the deny-list guard: {exc}"
            ) from exc

        # 3. role/impl conformance — the code must expose the method for EACH declared role
        #    (a dual {PROPOSER, EXECUTOR} module must expose BOTH ``propose`` and ``execute``).
        for role in manifest.roles:
            required_method = ROLE_METHOD[role]
            if not callable(getattr(impl, required_method, None)):
                raise ModuleRegistrationError(
                    f"module {manifest.name!r}: declared role={role.value!r} requires a callable "
                    f"{required_method!r} method"
                )

        # 4. name uniqueness.
        if manifest.name in self._modules:
            raise ModuleRegistrationError(
                f"module {manifest.name!r} is already registered"
            )

        registered = RegisteredModule(manifest=manifest, impl=impl)
        self._modules[manifest.name] = registered
        logger.info(
            "agent_framework: registered module name=%s roles=%s capabilities=%s tools=%d",
            manifest.name,
            sorted(r.value for r in manifest.roles),
            sorted(c.value for c in manifest.capabilities),
            len(manifest.tools),
        )
        return registered

    def get(self, name: str) -> RegisteredModule:
        """Look up a registered module by name. Raises ``KeyError`` if absent (fail-closed)."""
        if name not in self._modules:
            raise KeyError(
                f"module {name!r} not registered; available: {sorted(self._modules)}"
            )
        return self._modules[name]

    def names(self) -> list[str]:
        """The registered module names (sorted)."""
        return sorted(self._modules)

    def __contains__(self, name: object) -> bool:
        return name in self._modules


# --- The process-global default registry -----------------------------------------------------
#
# Empty by default. Unlike ROSTER / the coordinator registry, the framework does NOT auto-populate
# this with production agents — a module is registered explicitly (by a wiring step, or a test).
# This keeps the framework strictly additive: importing it changes NO live routing.
_DEFAULT_REGISTRY = AgentFrameworkRegistry()


def default_registry() -> AgentFrameworkRegistry:
    """The process-global default registry."""
    return _DEFAULT_REGISTRY


def register_agent(impl: Any) -> RegisteredModule:
    """Register a module into the default registry (convenience wrapper)."""
    return _DEFAULT_REGISTRY.register(impl)


def get_registered(name: str) -> RegisteredModule:
    """Look up a module in the default registry."""
    return _DEFAULT_REGISTRY.get(name)


# --- D-REG: manifest -> activation_registry (single source, EXPLICIT wiring) ------------------
#
# CL-2026-07-15-manifest-single-source-activation. The manifest is the SINGLE SOURCE of a module's
# activation bar; ``register_activation_prereqs`` is the deliberate, EXPLICIT step that PUBLISHES
# that bar into the existing ``activation_registry.REGISTRY`` the ``onboarding_gate`` reads. It is
# NOT called at import — importing the framework mutates zero live routing; the bar stays inert
# until a wiring step invokes this. Kept SEPARATE from ``register()`` on purpose: registering a
# module into the framework registry does NOT touch the live activation gate.


def register_activation_prereqs(module_or_manifest: Any) -> None:
    """Wire a module's declared activation prerequisites into ``activation_registry.REGISTRY``.

    ``module_or_manifest`` is a module instance (carrying ``.manifest``) OR an ``AgentManifest``.
    Reads the manifest's ``as_prerequisites()`` (the carried ``AgentPrerequisites``, or ``None``) and
    publishes it, keyed by the prereq's agent name, into the EXISTING activation registry.

    Guarantees:
      - NOT import-time — call this deliberately (a wiring step), never at module load.
      - No declared bar (``as_prerequisites()`` is ``None``, e.g. a read-only advisory lane) → no-op.
      - IDEMPOTENT — calling twice with the same manifest is a no-op (equal declaration).
      - DRIFT-GUARDED — a CONFLICTING re-declaration for the same agent name raises
        ``ModuleRegistrationError`` (the manifest is the single source; refuse to silently clobber).
      - LAZY-imports ``activation_registry`` (keeps the package dep-less-smoke safe).
    """
    manifest = (
        module_or_manifest
        if isinstance(module_or_manifest, AgentManifest)
        else getattr(module_or_manifest, "manifest", None)
    )
    if not isinstance(manifest, AgentManifest):
        raise ModuleRegistrationError(
            f"register_activation_prereqs: {module_or_manifest!r} is neither an AgentManifest nor a "
            "module carrying a 'manifest' attribute"
        )
    prereqs = manifest.as_prerequisites()
    if prereqs is None:
        return  # no declared activation bar — nothing to wire.

    # Lazy import: keep the mutation (and activation_registry) OUT of the framework's import surface.
    from orchestrator.agents import activation_registry

    existing = activation_registry.REGISTRY.get(prereqs.agent)
    if existing is not None and existing != prereqs:
        raise ModuleRegistrationError(
            f"register_activation_prereqs: activation registry already holds a DIFFERENT bar for "
            f"agent {prereqs.agent!r}; refusing to clobber it. The manifest is the single source — "
            "reconcile the declaration rather than double-wiring."
        )
    activation_registry.REGISTRY[prereqs.agent] = prereqs
    logger.info(
        "agent_framework: wired activation prereqs agent=%s journey=%s verify=%s data_source=%s "
        "min_customers=%d ownership=%s",
        prereqs.agent,
        prereqs.requires_journey_complete,
        prereqs.requires_verification,
        prereqs.requires_enabled_data_source,
        prereqs.min_customers,
        prereqs.requires_ownership_verified,
    )


# --- Role adapter: EXECUTOR module -> coordinator SpecialistAgent Protocol --------------------
#
# Concrete proof that the framework GENERALIZES the coordinator seam without replacing it: this
# adapter makes a framework ExecutorModule conform to the coordinator's ``SpecialistAgent`` Protocol
# (``name`` + ``execute_item(ctx) -> ItemExecutionResult``). A future cutover would register the
# adapter into ``coordinator._REGISTRY_SPEC`` — but that is a deliberate later step; nothing here
# wires it in. Kept in a class (not a live registration) so importing the framework never pulls the
# coordinator's ``dbos`` import.


class CoordinatorAgentAdapter:
    """Adapts a registered EXECUTOR module to the coordinator's ``SpecialistAgent`` Protocol.

    ``name`` mirrors the module manifest name (the coordinator requires key == name). ``execute_item``
    translates the coordinator's ``AgentItemContext`` into a framework ``ModuleContext`` (server-
    derived tenant, trusted directly), runs the module through its scoped facade, and translates the
    ``ModuleResult`` back into an ``ItemExecutionResult``. All PII stays inside the module (IDs-only
    across this boundary), exactly like the existing ``SalesRecoveryAgent``.
    """

    def __init__(self, registered: RegisteredModule) -> None:
        if AgentRole.EXECUTOR not in registered.manifest.roles:
            raise ModuleRegistrationError(
                f"CoordinatorAgentAdapter requires a module declaring the EXECUTOR role; "
                f"{registered.manifest.name!r} declares "
                f"{sorted(r.value for r in registered.manifest.roles)!r}"
            )
        self._registered = registered
        self.name = registered.manifest.name

    def execute_item(self, ctx: Any) -> Any:
        """Coordinator entrypoint. ``ctx`` is a ``coordinator.AgentItemContext`` (duck-typed to keep
        the coordinator's ``dbos`` import lazy)."""
        module_ctx = ModuleContext.for_executor(
            tenant_id=ctx.tenant_id,
            item_id=ctx.item_id,
            work_item_id=ctx.work_item_id,
            run_id=ctx.run_id,
        )
        result = self._registered.run(module_ctx)
        return result.to_item_execution_result()


__all__ = [
    "AgentFrameworkRegistry",
    "CoordinatorAgentAdapter",
    "ModuleDispatchError",
    "ModuleRegistrationError",
    "RegisteredModule",
    "default_registry",
    "get_registered",
    "register_activation_prereqs",
    "register_agent",
]
