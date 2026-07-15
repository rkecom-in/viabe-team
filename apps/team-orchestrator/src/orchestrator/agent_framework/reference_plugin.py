"""The REFERENCE plugin — a read-only PROPOSER, proving the seam end-to-end.

WHY THIS ONE: it is the lowest-risk real path — a read-only proposer re-expressing an existing
advisory read (the ``tech_lane.read_tech_context`` capability: the manager-held business objective +
identity slice) through the new contract. It demonstrates the full framework seam —

    registration -> capability declaration -> context in (IDOR-resolved tenant) -> proposal out

— WITHOUT rewiring any live money path, and without migrating SR or Integration (a later
who-does-it call). It is the canonical example a third-party module copies.

WHAT IT PROVES (see tests/agent/test_agent_framework.py):
  - a manifest declaring only a READ capability registers cleanly (deny-list + positive checks pass);
  - the ``ModuleContext`` it receives carries the IDOR-resolved tenant (a model-supplied foreign
    tenant is ignored when an ambient dispatch context is present);
  - its ``GateFacade`` is EMPTY — ``gate.request_customer_send(...)`` raises ``CapabilityNotDeclared``,
    i.e. a proposer is structurally unable to send;
  - it returns a PROPOSAL (``ModuleResult.proposal``), no side effect.

The business-context reader is INJECTABLE (``reader=``) — the repo's transport-injection convention
(cf. the connectors' ``FetchFn``) — so the plugin unit-tests with no DB. Default ``None`` lazy-imports
the real ``knowledge.business_context.read_business_context``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from orchestrator.agent_framework.capabilities import AgentRole, Capability
from orchestrator.agent_framework.context import ModuleContext, ModuleResult
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.manifest import AgentManifest

logger = logging.getLogger("orchestrator.agent_framework.reference_plugin")

#: Injectable reader signature: ``(tenant_id: str) -> business_context object`` (has ``.objective``
#: and optionally ``.identity``). Mirrors the connector ``FetchFn`` transport-injection convention.
BusinessContextReaderFn = Callable[[str], Any]


class BusinessContextReader:
    """A read-only PROPOSER: reads the tenant's business objective + identity and returns it as a
    proposal. Holds NO tool surface and NO gated capability — the minimal safe module shape."""

    manifest = AgentManifest(
        name="business_context_reader",
        version="1.0.0",
        role=AgentRole.PROPOSER,
        description=(
            "Read-only reference module: reads the tenant's manager-held business objective + "
            "identity slice and returns it as a proposal for the manager to frame a finding "
            "against the owner's goal. No side effects, no send, no write."
        ),
        capabilities=frozenset({Capability.READ_BUSINESS_CONTEXT}),
        prerequisites=None,  # like the advisory lanes: no activation bar (a read is always safe).
        tools=(),  # works purely through the context contract; holds no callable tool.
    )

    def __init__(self, *, reader: BusinessContextReaderFn | None = None) -> None:
        self._reader = reader

    def _read(self, tenant_id: str) -> Any:
        if self._reader is not None:
            return self._reader(tenant_id)
        from orchestrator.knowledge.business_context import read_business_context

        return read_business_context(tenant_id)

    def propose(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        """Read the business context for ``ctx.tenant_id`` and return it as a proposal.

        ``gate`` is intentionally UNUSED — a proposer has no side effects, and this facade is empty
        (would raise on any gated call). Best-effort read: a miss yields an empty proposal (context
        is enrichment), mirroring ``read_tech_context``'s own miss handling.
        """
        tenant_id = str(ctx.tenant_id)
        try:
            bc = self._read(tenant_id)
            proposal = {
                "objective": dict(getattr(bc, "objective", {}) or {}),
                "identity": dict(getattr(bc, "identity", {}) or {}),
            }
            return ModuleResult(
                role=AgentRole.PROPOSER, status="completed", proposal=proposal
            )
        except Exception:  # noqa: BLE001 — context read is enrichment; a miss is not a failure.
            logger.warning(
                "business_context_reader: read miss tenant=%s (returning empty proposal)",
                tenant_id,
            )
            return ModuleResult(
                role=AgentRole.PROPOSER,
                status="completed",
                proposal={"objective": {}, "identity": {}},
                reason="context_read_miss",
            )


__all__ = ["BusinessContextReader", "BusinessContextReaderFn"]
