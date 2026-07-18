"""First-party agent-framework MODULES (the migrated in-tree agents).

Each file here is a concrete ``agent_framework`` module (an ``AgentManifest`` + the role method(s)
its manifest declares) that ADAPTS an existing in-tree agent onto the framework contract WITHOUT
editing the agent it wraps. Importing this subpackage — or an individual module in it — wires NOTHING
live: a module graduates into a live seam (supervisor node / coordinator registry / activation
registry) only through a deliberate, Fazal-authorized cutover step, never at import (mirrors
``agent_framework``'s "importing the framework changes no routing" invariant). This subpackage is
intentionally NOT imported by ``orchestrator.agent_framework.__init__`` so the framework's import
surface stays inert + dep-less-smoke safe.
"""

from __future__ import annotations

__all__: list[str] = []


def register_all_modules() -> list[str]:
    """VT-686 live wiring (CC-owned) — register EVERY first-party module into the process-global
    default registry, idempotently, at BOOT (called from main.py's startup, register-before-launch
    like ``register_scheduled_triggers``). Before this, the registry started empty and only Sales
    Recovery self-registered lazily DURING supervisor-graph build — later in the dispatch than the
    Manager's agent-directory block reads it, so the Manager's first turn saw no directory and
    never saw the other four briefs at all (Codex live-wiring review, 2026-07-19).

    Import stays inert (this function is explicitly CALLED at boot, never run at import — the
    subpackage's no-routing-at-import invariant holds). Registration is the fail-closed manifest
    validation point: a bad brief/category crashes boot loudly. Idempotent per module (duplicate →
    re-enter via ``get_registered``, the supervisor's own guard pattern). Returns registered names.

    NOTE: registering ≠ routing. The supervisor/coordinator still decide what EXECUTES; this only
    makes every module's manifest (identity card, VT-686) VISIBLE to the Manager's directory.
    """
    from orchestrator.agent_framework import (
        ModuleRegistrationError,
        get_registered,
        register_agent,
    )

    names: list[str] = []
    for _load in (
        lambda: __import__(
            "orchestrator.agent_framework.modules.sales_recovery_module", fromlist=["*"]
        ).SalesRecoveryModule(),
        lambda: __import__(
            "orchestrator.agent_framework.modules.onboarding_conductor_module", fromlist=["*"]
        ).OnboardingConductorModule(),
        lambda: __import__(
            "orchestrator.agent_framework.modules.integration_tools_module", fromlist=["*"]
        ).IntegrationToolsModule(),
        lambda: __import__(
            "orchestrator.agent_framework.modules.common_tools_module", fromlist=["*"]
        ).CommonToolsModule(),
        lambda: __import__(
            "orchestrator.agent_framework.modules.compliance_tools_module", fromlist=["*"]
        ).ComplianceToolsModule(),
    ):
        impl = _load()
        try:
            registered = register_agent(impl)
        except ModuleRegistrationError:
            registered = get_registered(impl.manifest.name)
        names.append(registered.manifest.name)
    return names
