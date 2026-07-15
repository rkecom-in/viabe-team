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

from orchestrator.agent_framework.capabilities import AgentRole
from orchestrator.agent_framework.context import ModuleContext, ModuleResult
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.manifest import AgentManifest, ManifestError

logger = logging.getLogger("orchestrator.agent_framework.registration")


class ModuleRegistrationError(RuntimeError):
    """Raised when a module fails any registration check (validation / deny-list / conformance)."""


@dataclass(frozen=True)
class RegisteredModule:
    """A validated module: its manifest + its impl (a ``ProposerModule`` or ``ExecutorModule``)."""

    manifest: AgentManifest
    impl: Any

    def new_gate(self, ctx: ModuleContext) -> GateFacade:
        """Build the capability-scoped ``GateFacade`` for a run of this module against ``ctx``.

        The facade's tenant is the context's IDOR-resolved tenant; its capabilities are the
        manifest's declared set. A proposer's manifest has no gated capability, so its facade
        refuses every gated method — the structural read/propose-only guarantee.
        """
        return GateFacade(
            tenant_id=ctx.tenant_id,
            capabilities=self.manifest.capabilities,
            run_id=ctx.run_id,
        )

    def run(self, ctx: ModuleContext) -> ModuleResult:
        """Invoke the module for ``ctx`` (dispatching to ``propose`` or ``execute`` by role).

        This is the framework's uniform driver: build the scoped facade, call the role method,
        return the module's ``ModuleResult``. The reference plugin + tests prove the full seam
        (registration -> capability scope -> context in -> gate-mediated result out) through here.
        """
        gate = self.new_gate(ctx)
        if self.manifest.role is AgentRole.PROPOSER:
            return self.impl.propose(ctx, gate)
        return self.impl.execute(ctx, gate)


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

        # 3. role/impl conformance — the code must match the declared role.
        required_method = "propose" if manifest.role is AgentRole.PROPOSER else "execute"
        if not callable(getattr(impl, required_method, None)):
            raise ModuleRegistrationError(
                f"module {manifest.name!r}: role={manifest.role.value} requires a callable "
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
            "agent_framework: registered module name=%s role=%s capabilities=%s tools=%d",
            manifest.name,
            manifest.role.value,
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
        if registered.manifest.role is not AgentRole.EXECUTOR:
            raise ModuleRegistrationError(
                f"CoordinatorAgentAdapter requires an EXECUTOR module; "
                f"{registered.manifest.name!r} is {registered.manifest.role.value}"
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
    "ModuleRegistrationError",
    "RegisteredModule",
    "default_registry",
    "get_registered",
    "register_agent",
]
