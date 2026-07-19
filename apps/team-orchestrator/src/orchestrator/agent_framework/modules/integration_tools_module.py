"""VT-664 Stage 1 — the Integration surface as an ``agent_framework`` MODULE (connector TOOLS).

WHAT THIS IS
------------
ARCHITECTURE.md §5 ("Integration dissolves into connector Tools ... the Manager driving the
connector Tools") + §7.1, expressed on the framework contract, ADDITIVELY. This is a pure
``{PROPOSER}`` module whose ``manifest.tools`` ARE the eleven VT-608 connector ``@tool`` objects
(``agent/integration_agent.INTEGRATION_AGENT_TOOLS``). It re-expresses the Integration Agent's tool
surface as a conforming module WITHOUT touching the integration brain: it EDITS ZERO existing files
and delegates to the EXISTING tools verbatim.

  - ``propose`` is a THIN read entry (the connector flow is Manager-driven tool-calling, not a
    single ``propose()`` computation): it best-effort reads the tenant's current integration state
    and reports the connector tool surface the Manager can drive. The TOOLS are the point; the
    ``propose()`` is the honest entry that says "here is the integration state + the connector tools
    available for this tenant."

INERT / ADDITIVE (this stage makes the tools EXIST + CONFORM — nothing live)
---------------------------------------------------------------------------
Importing this module wires NOTHING: it is NOT imported by ``agent_framework/__init__`` and it does
NOT register itself. The LIVE "dissolution" — the Manager driving these connector Tools and the
integration brain (``build_integration_agent`` / the roster ``integration`` SpecialistSpec) being
removed — is a deliberate, Fazal-gated LATER step. This file only makes the module EXIST + CONFORM.

IMPORT-LIGHT (deliberate divergence from the tool-less SR / reference modules)
------------------------------------------------------------------------------
This is the FIRST framework module to carry a real ``manifest.tools`` surface. Importing
``agent/integration_agent`` is HEAVY — its module body eager-builds the langchain agent
(``build_integration_agent(_MODEL)``) and resolves a chat model. So the tool tuple is LAZY-imported
(``_connector_tools()``) and the manifest is built at INSTANCE construction rather than as a
class attribute: ``import orchestrator.agent_framework.modules.integration_tools_module`` pulls NO
langchain / model / dbos — only ``IntegrationToolsModule()`` does. That keeps this module file
dep-less-smoke safe, exactly like the lazy-delegate discipline the SR module and reference plugin
follow (they simply had no tools to carry). The state reader is likewise INJECTABLE (``state_reader=``,
the repo's transport-injection convention) so the module unit-tests with no DB.

CAPABILITY MODELING (truthful, minimal, NON-GATED — FLAGGED for review)
-----------------------------------------------------------------------
The manifest declares exactly the two NON-GATED capabilities the connector tools exercise, and NO
gated (``REQUEST_*``) capability — consistent with the PROPOSER-only role:

  * ``READ_INTEGRATION_STATE`` — the READS (``read_integration_state`` / ``check_oauth_status`` /
    ``verify_connector`` / ``list_supported_connectors`` / the counts-only ``pull_sample``).
  * ``PROPOSE_CONFIG_CHANGE`` — the mapping / cadence / commit proposals + the OAuth link-out
    (``propose_mapping`` / ``confirm_mapping`` / ``commit_ingestion`` [proposal-only, VT-268] /
    ``schedule_recurring_pull`` [cadence CONFIG, VT-210 accepted precedent] / ``start_oauth``).

There is NO customer send and NO money action anywhere on this surface — the eleven tools are
VT-268-safe (they already pass ``assert_agent_tools_safe`` as the integration agent's surface;
``commit_ingestion`` is PROPOSAL-ONLY, the real ingest WRITE is the module-EXTERNAL deterministic
executor ``integrations/commit.execute_pending_ingestion_commit``, NOT wrapped here). So no gated
capability is truthful, and the module is a pure ``{PROPOSER}``. The config-staging writes the tools
DO perform (cadence row / confirmed-mapping) are non-gated by the framework's own model (only
send/spend are ``REQUEST_*``) and VT-268-safe — hence ``PROPOSE_CONFIG_CHANGE`` (non-gated), never a
``REQUEST_*`` capability. The ``gate`` argument to ``propose`` is therefore intentionally UNUSED (the
proposer-lane facade is empty and would raise on any gated call).

REGISTRATION SHAPE (FLAGGED for review)
---------------------------------------
The framework registry registers MODULES (``register_agent``) carrying a ``tools`` surface — there is
NO separate standalone-Tool registry (``registration.py``). So the faithful additive shape for
"connector Tools" is a MODULE whose ``manifest.tools`` == the connector tools, registerable +
deny-list-clean. That is the shape here.

  * NAME — ``"integration_tools"``, deliberately DISTINCT from the still-live conversational brain
    (roster ``SpecialistSpec(name="integration", agent_name="integration_agent")``) so the two
    coexist unambiguously during the additive phase. At the live cutover the reviewer may prefer to
    align the name to ``"integration"`` (roster name) or ``"integration_agent"`` (activation-registry
    key) depending on the graduation path chosen — a NAMING call for that gated step, not this one.
  * ``prerequisites=None`` — like the reference plugin and the advisory lanes ("a read is always
    safe"): the PROPOSER lane here is a READ, and the activation bar governs EXECUTION, which this
    additive proposer stage does not perform. NOTE FOR REVIEW: an activation bar for
    ``"integration_agent"`` ALREADY EXISTS in ``activation_registry.REGISTRY`` (journey + verification
    + ownership). If the live dissolution has the Manager drive these tools to actually mutate a
    tenant's config, the reviewer should decide whether to carry that bar here (which would require
    ``manifest.name == "integration_agent"`` for ``manifest.validate()`` — the ``prerequisites.agent
    == name`` invariant) or keep activation gating upstream. Left OUT of this additive stage on
    purpose.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from orchestrator.agent_framework.capabilities import AgentRole, Capability
from orchestrator.agent_framework.context import ModuleContext, ModuleResult
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.manifest import AgentBrief, AgentManifest

logger = logging.getLogger("orchestrator.agent_framework.modules.integration_tools")

#: The module's stable registry key. DISTINCT from the live roster ``integration`` brain (see the
#: module docstring, "REGISTRATION SHAPE"); a naming call is deferred to the gated cutover step.
MODULE_NAME = "integration_tools"

#: VT-101 Stage 3(c) — the SINGLE SOURCE OF TRUTH for whether the Integration surface routes through
#: the agent_framework contract: the Manager holds the connector Tools directly (advisory-tool
#: demotion) and the ``integration`` sub-graph specialist is dissolved from the spawnable roster.
#: Both the roster (``spawnable_roster``) and the supervisor (Manager tool set) read it. Default OFF:
#: the pre-VT-101 spawn-the-integration-specialist path runs byte-identically. Dev sets it to validate
#: the dissolution; prod stays unset until Fazal promotes. Mirrors the SR module's
#: ``FRAMEWORK_ROUTING_FLAG`` / ``sr_via_framework`` pattern exactly.
FRAMEWORK_ROUTING_FLAG = "TEAM_INTEGRATION_VIA_FRAMEWORK"


def integration_via_framework() -> bool:
    """True iff the Integration surface should route through the agent_framework contract — the
    Manager drives the connector Tools directly and the ``integration`` specialist is excluded from
    the spawnable roster (default OFF).

    Read at CALL TIME (never cached at import) so dev can flip it per-process; prod stays unset until
    Fazal promotes. Mirrors ``sales_recovery_module.sr_via_framework`` exactly."""
    return os.environ.get(FRAMEWORK_ROUTING_FLAG, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

#: Injectable state-reader signature: ``(tenant_id: str) -> integration-state dict | None`` (the
#: ``{"phase", "current_connector_id", "pending_owner_input"}`` shape ``read_integration_state``
#: returns). Default ``None`` -> lazy-import the real reader. Mirrors the connector ``FetchFn`` /
#: reference-plugin ``reader=`` transport-injection convention so the module unit-tests with no DB.
StateReaderFn = Callable[[str], Any]


def _connector_tools() -> tuple[Any, ...]:
    """The eleven VT-608 connector tools, LAZY-imported (importing the integration agent eager-builds
    the langchain agent + resolves a model — kept OUT of this module's import surface)."""
    from orchestrator.agent.integration_agent import INTEGRATION_AGENT_TOOLS

    return tuple(INTEGRATION_AGENT_TOOLS)


class IntegrationToolsModule:
    """The Integration surface as a pure ``{PROPOSER}`` framework module carrying the eleven
    connector tools on ``manifest.tools``.

    Declares NO gated capability (there is no send / money action on the connector surface — see the
    module docstring). Its ``propose`` is a thin, side-effect-free read entry; the connector TOOLS are
    the point, driven by the Manager at the deferred live-dissolution step. The manifest (with its
    tool surface) is built at construction so the module FILE stays import-light.
    """

    def __init__(self, *, state_reader: StateReaderFn | None = None) -> None:
        self._state_reader = state_reader
        #: Instance-level (not class-level) so importing this file pulls no langchain/model — only
        #: constructing the module resolves the connector tools (see the module docstring).
        self.manifest = AgentManifest(
            name=MODULE_NAME,
            version="1.0.0",
            roles=frozenset({AgentRole.PROPOSER}),
            description=(
                "Integration connector surface. PROPOSER: reads the tenant's current integration "
                "phase and reports the connector tools (list/read/OAuth/pull-sample/propose+confirm-"
                "mapping/commit-proposal/schedule/verify) the Manager drives to set up and maintain a "
                "customer-data source. No customer send, no money action — reads + config proposals + "
                "an OAuth link-out; the ingest WRITE is a deterministic module-external executor."
            ),
            # Truthful, minimal, NON-GATED set (see the module docstring, "CAPABILITY MODELING").
            capabilities=frozenset(
                {Capability.READ_INTEGRATION_STATE, Capability.PROPOSE_CONFIG_CHANGE}
            ),
            # No activation bar carried in this additive stage (FLAGGED — see the module docstring).
            prerequisites=None,
            # The eleven VT-608 connector @tool objects — the whole point of the dissolution. Already
            # deny-list-clean (they are the integration agent's own surface); the conformance
            # ``tool_surface_safe`` check re-proves it over all eleven here.
            tools=_connector_tools(),
            entitlement_key=None,
            # VT-686 — the agent taxonomy: category/tags/brief, written from this module's own
            # docstring above (accurate, no invention).
            category="Integration",
            tags=frozenset({"integration", "connectors", "oauth", "data-source", "ingestion"}),
            brief=AgentBrief(
                what_it_does=(
                    "Reads the tenant's current integration/connector phase and exposes the "
                    "connector tools (list/read/OAuth/pull-sample/propose+confirm-mapping/"
                    "commit-proposal/schedule/verify) for setting up and maintaining a "
                    "customer-data source."
                ),
                actions=(
                    "read_integration_state",
                    "check_oauth_status",
                    "verify_connector",
                    "list_supported_connectors",
                    "pull_sample",
                    "propose_mapping",
                    "confirm_mapping",
                    "commit_ingestion",
                    "schedule_recurring_pull",
                    "start_oauth",
                ),
                business_activities=(
                    "connect a sales-data source (Google Sheets / Shopify)",
                    "set up and maintain automated customer-data ingestion",
                ),
                when_to_use=(
                    "Route here when the owner wants to connect, check, or fix a data source / "
                    "integration (Sheets, Shopify, etc.), or asks about the status of an already-"
                    "connected source."
                ),
                limits=(
                    "no customer send, no money action",
                    "commit_ingestion is PROPOSAL-ONLY — the real ingest WRITE is a deterministic "
                    "module-external executor, never performed here",
                    "does not talk to the owner directly — the Manager renders every word the "
                    "owner reads",
                ),
            ),
        )

    # --- PROPOSER lane -------------------------------------------------------------------------

    def _read_state(self, tenant_id: str) -> Any:
        if self._state_reader is not None:
            return self._state_reader(tenant_id)
        # Lazy: pulls the DB-backed onboarding read — kept out of the import surface. This is the
        # SAME underlying read the ``read_integration_state`` @tool delegates to; the module holds
        # the ALREADY-resolved authoritative tenant, so it calls the reader directly (no re-run of
        # the tool's lane-tenant resolution, which needs an ambient dispatch context).
        from orchestrator.onboarding.shopify_onboarding import (
            read_integration_state as _read_state,
        )

        return _read_state(tenant_id)

    def propose(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        """Report the tenant's integration state + the connector tool surface as a proposal.

        Modeled as a THIN read (the connector flow is Manager-driven tool-calling, so there is no
        single ``propose()`` computation to run): best-effort read the current integration state for
        ``ctx.tenant_id`` and list the connector tools the Manager can drive. ``gate`` is intentionally
        UNUSED — a proposer has no side effects and this facade is empty (would raise on any gated
        call). A read miss yields a ``None`` state (context is enrichment, not a failure), mirroring
        the reference plugin's best-effort read; the tool surface is ALWAYS reported.
        """
        tenant_id = str(ctx.tenant_id)
        tool_names = [getattr(t, "name", repr(t)) for t in self.manifest.tools]
        try:
            state = self._read_state(tenant_id)
        except Exception:  # noqa: BLE001 — integration-state read is enrichment; a miss is not a failure.
            logger.warning(
                "integration_tools: integration-state read miss tenant=%s (reporting tools only)",
                tenant_id,
            )
            return ModuleResult(
                role=AgentRole.PROPOSER,
                status="completed",
                proposal={"integration_state": None, "connector_tools": tool_names},
                reason="integration_state_read_miss",
            )
        return ModuleResult(
            role=AgentRole.PROPOSER,
            status="completed",
            proposal={
                "integration_state": dict(state) if state is not None else None,
                "connector_tools": tool_names,
            },
        )


__all__ = [
    "FRAMEWORK_ROUTING_FLAG",
    "MODULE_NAME",
    "IntegrationToolsModule",
    "StateReaderFn",
    "integration_via_framework",
]
