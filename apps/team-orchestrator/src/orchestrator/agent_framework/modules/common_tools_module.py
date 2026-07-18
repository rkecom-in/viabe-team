"""The COMMON READ-tools surface as an ``agent_framework`` MODULE.

WHAT THIS IS
------------
ARCHITECTURE.md §1.3 ("READ tools … scoped to the Manager's resolved tenant") + §1.1 ("the
specialist pulls operational data itself via Manager-scoped READ tools"), expressed on the
framework contract, ADDITIVELY. A pure ``{PROPOSER}`` module whose ``manifest.tools`` ARE the three
common READ ``@tool`` objects (``agent_framework.tools_common.COMMON_READ_TOOLS``): the
customer-ledger summary, the business-context read, and the integration-state read. It re-expresses
the common read surface as a conforming module WITHOUT re-authoring any read — the tools delegate to
the existing readers verbatim (see ``tools_common``).

  - ``propose`` is a THIN read entry (the reads are Manager/specialist-driven tool-calls, not a
    single ``propose()`` computation): it reports the common READ-tool surface the Manager can drive
    for this tenant. The TOOLS are the point; ``propose()`` is the honest "here are the common read
    tools available" entry.

CAPABILITY MODELING (truthful, minimal, NON-GATED)
--------------------------------------------------
The manifest declares exactly the three NON-GATED read capabilities the tools exercise —
``{READ_BUSINESS_CONTEXT, READ_CUSTOMER_LEDGER, READ_INTEGRATION_STATE}`` — and NO gated
(``REQUEST_*``) capability. There is no customer send and no money action anywhere on this surface:
the three tools are pure reads (counts / owner-business-fields / integration phase), so the module
is a pure ``{PROPOSER}`` and the ``gate`` argument to ``propose`` is intentionally UNUSED (the
proposer-lane facade is empty and would raise on any gated call). The tool surface is deny-list
clean by construction (``tools_common`` runs ``assert_agent_tools_safe`` at its own import); the
conformance ``tool_surface_safe`` check re-proves it over all three here.

IMPORT-LIGHT (mirrors ``integration_tools_module`` exactly)
-----------------------------------------------------------
Building the manifest LAZY-imports ``COMMON_READ_TOOLS`` (which pulls ``langchain_core`` +
``orchestrator.agent``) so the tuple is resolved at INSTANCE construction, not as a class attribute:
``import orchestrator.agent_framework.modules.common_tools_module`` pulls NO langchain — only
``CommonToolsModule()`` does. That keeps this file dep-less-smoke safe, matching the sibling
integration-tools module. A ``tools_provider=`` hook is injectable (the repo's transport-injection
convention) so the module unit-tests without importing the real langchain tools.

INERT / ADDITIVE
----------------
Importing this module wires NOTHING: it is NOT imported by ``agent_framework/__init__`` and does NOT
register itself. Wiring the Manager / specialist live paths to this surface is a deliberate
follow-on, not done here. This file only makes the module EXIST + CONFORM.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from orchestrator.agent_framework.capabilities import AgentRole, Capability
from orchestrator.agent_framework.context import ModuleContext, ModuleResult
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.manifest import AgentBrief, AgentManifest

logger = logging.getLogger("orchestrator.agent_framework.modules.common_tools")

#: The module's stable registry key.
MODULE_NAME = "common_tools"

#: Injectable tools-provider signature: ``() -> tuple[tool, ...]``. Default ``None`` -> lazy-import
#: the real ``COMMON_READ_TOOLS``. Mirrors the sibling module's ``_connector_tools`` /
#: transport-injection convention so the module unit-tests without the real langchain tools.
ToolsProvider = Callable[[], tuple[Any, ...]]


def _common_read_tools() -> tuple[Any, ...]:
    """The three common READ tools, LAZY-imported (defining them pulls ``langchain_core`` +
    ``orchestrator.agent`` — kept OUT of this module's import surface)."""
    from orchestrator.agent_framework.tools_common import COMMON_READ_TOOLS

    return tuple(COMMON_READ_TOOLS)


class CommonToolsModule:
    """The common READ-tools surface as a pure ``{PROPOSER}`` framework module carrying the three
    common read tools on ``manifest.tools``.

    Declares NO gated capability (the surface is pure reads — see the module docstring). Its
    ``propose`` is a thin, side-effect-free read entry; the READ TOOLS are the point, driven by the
    Manager/specialist. The manifest (with its tool surface) is built at construction so the module
    FILE stays import-light.
    """

    def __init__(self, *, tools_provider: ToolsProvider | None = None) -> None:
        tools = tuple(tools_provider()) if tools_provider is not None else _common_read_tools()
        #: Instance-level (not class-level) so importing this file pulls no langchain — only
        #: constructing the module resolves the common read tools (see the module docstring).
        self.manifest = AgentManifest(
            name=MODULE_NAME,
            version="1.0.0",
            roles=frozenset({AgentRole.PROPOSER}),
            description=(
                "Common READ-tools surface. PROPOSER: reports the Manager-scoped read tools a "
                "specialist drives to pull operational data for itself — the customer-ledger "
                "counts summary, the business-context read, and the integration-state read. No "
                "customer send, no money action, no customer PII (counts / owner-business-fields / "
                "integration phase only)."
            ),
            # Truthful, minimal, NON-GATED set — one read capability per tool.
            capabilities=frozenset(
                {
                    Capability.READ_BUSINESS_CONTEXT,
                    Capability.READ_CUSTOMER_LEDGER,
                    Capability.READ_INTEGRATION_STATE,
                }
            ),
            # A read is always safe: no activation bar carried in this additive stage.
            prerequisites=None,
            # The three common READ @tool objects — the whole point of the module. Deny-list clean
            # (tools_common asserts it at import); the conformance ``tool_surface_safe`` re-proves it.
            tools=tools,
            entitlement_key=None,
            # VT-686 — the agent taxonomy: category/tags/brief, written from this module's own
            # docstring above (accurate, no invention). "Tech" — this is cross-cutting platform
            # infrastructure (the shared read surface every specialist reaches through), not a
            # domain specialist in its own right; see ``when_to_use`` below.
            category="Tech",
            tags=frozenset(
                {"reads", "common", "ledger", "business-context", "integration-state"}
            ),
            brief=AgentBrief(
                what_it_does=(
                    "Exposes the Manager-scoped common READ tools every specialist pulls "
                    "operational data through — customer-ledger counts, business-context, "
                    "integration-state, active-plan, agent-memory, recent campaigns, attribution, "
                    "and per-customer ledger query."
                ),
                actions=(
                    "read_customer_ledger_summary",
                    "read_business_context",
                    "read_integration_state",
                    "read_active_plan",
                    "read_agent_memory",
                    "get_recent_campaigns",
                    "get_attribution_data",
                    "query_customer_ledger",
                ),
                business_activities=(
                    "give every specialist a shared, safe way to read tenant-scoped operational "
                    "data",
                ),
                when_to_use=(
                    "Not a standalone delegation target — this is the shared read surface other "
                    "specialists (Sales Recovery, Integration, Compliance) reach through to pull "
                    "their own operational context. The Manager does not spawn this as its own "
                    "specialist for an owner-facing turn."
                ),
                limits=(
                    "pure reads only — no write, no send, no money action",
                    "counts / owner-business-fields / integration phase only — no customer PII "
                    "(CL-390)",
                ),
            ),
        )

    # --- PROPOSER lane -------------------------------------------------------------------------

    def propose(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        """Report the common READ-tool surface for ``ctx.tenant_id`` as a proposal.

        Modeled as a THIN read (the reads are Manager/specialist-driven tool-calls, so there is no
        single ``propose()`` computation to run): it lists the common read tools the Manager can
        drive for this tenant. ``gate`` is intentionally UNUSED — a proposer has no side effects and
        this facade is empty (would raise on any gated call). No DB is touched here (the per-tool
        reads open their own RLS scope when actually driven); ``propose`` only reports the surface.
        """
        tool_names = [getattr(t, "name", repr(t)) for t in self.manifest.tools]
        return ModuleResult(
            role=AgentRole.PROPOSER,
            status="completed",
            proposal={"common_read_tools": tool_names},
        )


__all__ = [
    "MODULE_NAME",
    "CommonToolsModule",
    "ToolsProvider",
]
