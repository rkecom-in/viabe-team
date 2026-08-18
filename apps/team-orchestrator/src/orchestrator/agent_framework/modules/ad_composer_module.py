"""VT-769 inert ACF adapter for the structurally sendless Ad Composer."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orchestrator.agent_framework.capabilities import AgentRole, Capability
from orchestrator.agent_framework.context import ModuleContext, ModuleResult
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.manifest import AgentBrief, AgentManifest
from orchestrator.agent_framework.retrieval_profiles import specialist_retrieval_profile
from orchestrator.agents.activation_registry import AgentPrerequisites
from orchestrator.agents.ad_composer import (
    AGENT_TOOLS,
    CampaignAssignment,
    CampaignProposal,
    compose_campaign_proposal,
)
from orchestrator.knowledge_contracts import KnowledgeDomain


MODULE_NAME = "ad_composer"
ASSIGNMENT_KEY = "campaign_assignment"
ComposerFn = Callable[[CampaignAssignment], Any]


class AdComposerModule:
    manifest = AgentManifest(
        name=MODULE_NAME,
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description=(
            "Composes complete Meta/Google campaign proposals with approved creatives, a typed "
            "destination request, success metric and kill criterion. It cannot publish or spend."
        ),
        capabilities=frozenset({Capability.PROPOSE_CAMPAIGN}),
        prerequisites=AgentPrerequisites(
            agent=MODULE_NAME,
            requires_journey_complete=True,
            requires_verification=False,
            requires_enabled_data_source=False,
            min_customers=0,
            requires_ownership_verified=True,
        ),
        tools=AGENT_TOOLS,
        required_tools=(),
        category="Marketing",
        tags=frozenset({"ads", "campaigns", "creative", "planning"}),
        retrieval_profile=specialist_retrieval_profile(
            identity=MODULE_NAME,
            domains=frozenset({KnowledgeDomain.MARKETING}),
            top_k=8,
            token_budget=3_000,
        ),
        brief=AgentBrief(
            what_it_does=(
                "Produces complete, grounded Meta/Google campaign proposals for manual owner "
                "publication, including budget, measurement and a stop rule."
            ),
            actions=(
                "compose_campaign_proposal",
                "validate_budget_and_kill_criterion",
                "request_aggregate_tracked_destination",
            ),
            business_activities=(
                "plan paid acquisition campaigns",
                "turn approved creative into an owner-publishable campaign plan",
            ),
            when_to_use=(
                "Use after content artifacts are approved and the owner wants a complete Meta or "
                "Google campaign proposal with a bounded budget and explicit stop condition."
            ),
            limits=(
                "cannot publish, pause, fund or mutate an advertising account",
                "cannot mint a destination until the aggregate tracked-destination seam exists",
                "does not receive customer lists, credentials or sensitive targeting traits",
            ),
        ),
    )

    def __init__(self, *, composer: ComposerFn | None = None) -> None:
        self._composer = composer

    def propose(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        del gate
        assignment = ctx.data.get(ASSIGNMENT_KEY)
        if not isinstance(assignment, CampaignAssignment):
            raise ValueError(
                f"AdComposerModule requires ctx.data[{ASSIGNMENT_KEY!r}] as CampaignAssignment"
            )
        proposal = (
            self._composer(assignment)
            if self._composer is not None
            else compose_campaign_proposal(assignment)
        )
        if not isinstance(proposal, CampaignProposal):
            raise TypeError("ad composer must return CampaignProposal")
        result = proposal.as_proposal()
        if result["publication_mode"] != "manual_owner_only" or result["effect_authorized"] is not False:
            raise RuntimeError("Ad Composer returned effect-capable publication state")
        return ModuleResult(role=AgentRole.PROPOSER, status="completed", proposal=result)


__all__ = ["ASSIGNMENT_KEY", "AdComposerModule", "MODULE_NAME"]
