"""The module PROTOCOLS — the exact seam a third-party (or migrated first-party) agent implements.

Generalizes the two existing "real" interfaces into role-specific Protocols:
  - ``ProposerModule``  generalizes the conversational specialist (``run_sales_recovery_agent`` /
                        the ``integration_agent`` sub-graph) — it PROPOSES.
  - ``ExecutorModule``  generalizes the coordinator ``SpecialistAgent`` Protocol
                        (``coordinator.py:118``) — it EXECUTES a dispatched work item.

Both are ``runtime_checkable`` (like the coordinator's own ``SpecialistAgent``) so registration can
assert conformance structurally. Both receive a ``GateFacade`` — a proposer's is empty (no gated
capability), an executor's is scoped to its declared gated capabilities. The uniform signature is
deliberate: the ENFORCEMENT is the facade's capability scope, not "remember not to pass a facade to
a proposer."

THE THIRD-PARTY CONTRACT — a new module is exactly:
    class MyModule:
        manifest = AgentManifest(name=..., version=..., role=..., description=..., capabilities=...)
        def propose(self, ctx, gate) -> ModuleResult: ...     # PROPOSER
        # OR
        def execute(self, ctx, gate) -> ModuleResult: ...     # EXECUTOR
    register_agent(MyModule())
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from orchestrator.agent_framework.context import ModuleContext, ModuleResult
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.manifest import AgentManifest


@runtime_checkable
class ProposerModule(Protocol):
    """A conversational-lane module that PROPOSES. No side effects.

    ``manifest.role`` MUST be ``PROPOSER``. ``propose`` reads ``ctx`` (the IDOR-resolved tenant +
    the manager's situation/desired_outcome framing) and returns a ``ModuleResult`` carrying a
    ``proposal``. The ``gate`` it receives is EMPTY (a proposer declares no gated capability), so
    any attempt to use it raises ``CapabilityNotDeclared`` — a proposer is structurally read/propose
    only.
    """

    manifest: AgentManifest

    def propose(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult: ...


@runtime_checkable
class ExecutorModule(Protocol):
    """A coordinator-dispatched module that EXECUTES a work item.

    ``manifest.role`` MUST be ``EXECUTOR``. ``execute`` reads ``ctx`` (server-derived tenant + the
    work-item IDs), does its work (re-reading PII from RLS tables itself, IDs-in-state), ARMS any
    consequential action through ``gate`` (scoped to its declared gated capabilities), and returns
    a ``ModuleResult`` carrying the work-item status. Adapts to the coordinator's ``SpecialistAgent``
    via ``registration.CoordinatorAgentAdapter``.
    """

    manifest: AgentManifest

    def execute(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult: ...


__all__ = ["ExecutorModule", "ProposerModule"]
