"""The context contract — ``ModuleContext`` (input) + ``ModuleResult`` (output).

Generalizes the two existing input shapes:
  - PROPOSER input  — ``SpecialistHandoff`` (situation / desired_outcome / context_slice / data).
  - EXECUTOR input  — ``AgentItemContext`` (tenant_id / item_id / work_item_id / run_id — IDs only).

…and the two existing output shapes:
  - PROPOSER output — ``AgentResult`` (a typed proposal envelope; orchestrator owns side effects).
  - EXECUTOR output — ``ItemExecutionResult`` (a work-item status + IDs + counters, no PII).

TENANT AUTHORITY (the map's flagged trust concern — IDOR):
  - A PROPOSER runs inside a conversational graph where a MODEL might supply a tenant_id. So
    ``for_proposer`` resolves the tenant through ``lane_tenant.resolve_lane_tenant`` — the ambient
    dispatch ``ObservabilityContext`` WINS; a model-supplied value that disagrees is logged +
    ignored (the VT-293/294/599 IDOR guard). The module never gets to pick its own tenant.
  - An EXECUTOR is dispatched by the deterministic coordinator with a SERVER-DERIVED tenant_id
    (from the work item). So ``for_executor`` trusts it directly — there is no model in that path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from orchestrator.agent_framework.capabilities import AgentRole


class TenantResolutionError(RuntimeError):
    """Raised when a module context cannot resolve an authoritative tenant.

    Fail-closed: a module must NEVER run against an unknown/ambiguous tenant. (The conversational
    lane tools return a structured error dict instead of raising — but a module dispatch is a
    coarser boundary than a single tool call, so here we fail loud rather than run tenant-less.)
    """


@dataclass(frozen=True)
class ModuleContext:
    """Everything a module receives. Frozen value object.

    Construct via ``for_proposer`` / ``for_executor`` (never the raw ctor) so the tenant is always
    resolved through the correct authority path. The two roles populate different fields; the
    unused ones stay at their neutral defaults.

    Common:
      - ``tenant_id`` — the AUTHORITATIVE tenant (a ``UUID``; resolved, never model-trusted).
      - ``role``      — which role this context is for.
      - ``run_id``    — the dispatch run id (observability lineage), if any.

    PROPOSER framing (generalizes ``SpecialistHandoff``):
      - ``situation`` / ``desired_outcome`` — the manager's framing (WHAT outcome, not the action).
      - ``context_slice`` — the lane-scoped slice of business context.
      - ``data``          — the per-lane structured bundle (e.g. a serialized SalesRecoveryContext).

    EXECUTOR identity (generalizes ``AgentItemContext``):
      - ``item_id`` / ``work_item_id`` — the roadmap item + the claimed work-item row (IDs only).
    """

    tenant_id: UUID
    role: AgentRole
    run_id: str | None = None
    # proposer framing
    situation: str = ""
    desired_outcome: str = ""
    context_slice: Mapping[str, Any] = field(default_factory=dict)
    data: Mapping[str, Any] = field(default_factory=dict)
    # executor identity
    item_id: str | None = None
    work_item_id: str | None = None

    @classmethod
    def for_proposer(
        cls,
        *,
        tenant_model_value: str | UUID | None,
        module_name: str,
        run_id: str | None = None,
        situation: str = "",
        desired_outcome: str = "",
        context_slice: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> ModuleContext:
        """Build a PROPOSER context, resolving the tenant through the IDOR guard.

        ``tenant_model_value`` is treated as UNTRUSTED (a model may have supplied it): it is passed
        to ``resolve_lane_tenant``, which returns the ambient dispatch tenant when present
        (authoritative) and ignores a disagreeing model value. Only when there is NO ambient
        context does it parse ``tenant_model_value`` as a UUID (a direct/unit-test call). Raises
        ``TenantResolutionError`` if neither resolves.
        """
        from orchestrator.agent.lane_tenant import resolve_lane_tenant

        resolved = resolve_lane_tenant(
            str(tenant_model_value) if tenant_model_value is not None else None,
            tool_name=f"agent_framework:{module_name}",
        )
        if resolved is None:
            raise TenantResolutionError(
                f"module {module_name!r}: no resolvable tenant (no ambient dispatch context and "
                "no parseable tenant value) — refusing to run tenant-less (fail-closed)"
            )
        return cls(
            tenant_id=resolved,
            role=AgentRole.PROPOSER,
            run_id=run_id,
            situation=situation,
            desired_outcome=desired_outcome,
            context_slice=dict(context_slice or {}),
            data=dict(data or {}),
        )

    @classmethod
    def for_executor(
        cls,
        *,
        tenant_id: str | UUID,
        item_id: str,
        work_item_id: str,
        run_id: str,
    ) -> ModuleContext:
        """Build an EXECUTOR context. The tenant is SERVER-DERIVED (from the coordinator work item),
        so it is trusted directly — parsed as a UUID, no model-value reconciliation. Raises
        ``TenantResolutionError`` if it does not parse (a coordinator bug, fail-loud)."""
        try:
            resolved = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise TenantResolutionError(
                f"executor context: tenant_id={tenant_id!r} is not a valid UUID"
            ) from exc
        return cls(
            tenant_id=resolved,
            role=AgentRole.EXECUTOR,
            run_id=run_id,
            item_id=item_id,
            work_item_id=work_item_id,
        )


@dataclass(frozen=True)
class ModuleResult:
    """Everything a module returns. Frozen value object spanning both roles.

    PROPOSER result — ``proposal`` carries the structured output (the intent/draft/recommendation);
    ``status`` is a proposer terminal state (e.g. ``completed`` / ``refused``). NO side effect
    happened. Adapts to the existing ``AgentResult`` via ``to_agent_result``.

    EXECUTOR result — ``work_item_status`` carries a coordinator work-item status; ``batch_id`` /
    ``counters`` carry the IDs-only outcome. Adapts to the existing ``ItemExecutionResult`` via
    ``to_item_execution_result``.

    The adapters are what makes the generalization CONCRETE: a framework module's output flows into
    the EXISTING dispatch/return seams unchanged.
    """

    role: AgentRole
    status: str
    proposal: Mapping[str, Any] | None = None
    work_item_status: str | None = None
    batch_id: str | None = None
    counters: Mapping[str, int] = field(default_factory=dict)
    reason: str = ""

    def to_agent_result(self) -> Any:
        """Adapt a PROPOSER result to the existing ``agent.types.AgentResult`` envelope.

        Lazy import: ``AgentResult`` pulls ``orchestrator.failures`` — kept out of the framework's
        import surface so the dep-less smoke suite collects this module.
        """
        from orchestrator.agent.types import AgentResult

        status = self.status if self.status in _AGENT_RESULT_STATUSES else "completed"
        return AgentResult(status=status, output=dict(self.proposal or {}))

    def to_item_execution_result(self) -> Any:
        """Adapt an EXECUTOR result to the existing ``coordinator.ItemExecutionResult``.

        Lazy import: the coordinator module imports ``dbos`` at top level — kept out of the
        framework's import surface (dep-less smoke). ``work_item_status`` falls back to ``status``
        when not separately set, so an executor may populate either field.
        """
        from orchestrator.agents.coordinator import ItemExecutionResult

        return ItemExecutionResult(
            work_item_status=self.work_item_status or self.status,
            batch_id=self.batch_id,
            counters=dict(self.counters),
        )


# The AgentResult status literals (mirror agent/types.py:AgentStatus) — kept here as a plain set so
# to_agent_result can normalize without importing the heavy module just to read the Literal.
_AGENT_RESULT_STATUSES = frozenset(
    {"completed", "terminated", "refused", "invalid", "placeholder", "rejected"}
)


__all__ = ["ModuleContext", "ModuleResult", "TenantResolutionError"]
