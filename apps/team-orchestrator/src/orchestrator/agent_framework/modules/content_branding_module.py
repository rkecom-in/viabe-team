"""VT-768 inert ACF adapter for the Content/Branding specialist.

Importing this module registers and activates nothing.  It returns an unpersisted artifact proposal;
the Manager/owner remains the publication boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orchestrator.agent_framework.capabilities import AgentRole, Capability
from orchestrator.agent_framework.context import ModuleContext, ModuleResult
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.manifest import AgentBrief, AgentManifest
from orchestrator.agent_framework.retrieval_profiles import specialist_retrieval_profile
from orchestrator.agents.activation_registry import AgentPrerequisites
from orchestrator.agents.content_branding import (
    AGENT_TOOLS,
    ContentAssignment,
    compose_content_artifact,
)
from orchestrator.knowledge_contracts import KnowledgeDomain


MODULE_NAME = "content_branding"
ASSIGNMENT_KEY = "content_assignment"
ComposerFn = Callable[[ContentAssignment], Any]


class ContentBrandingModule:
    manifest = AgentManifest(
        name=MODULE_NAME,
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description=(
            "Composes grounded social, launch, landing, report-promotion and WhatsApp Status copy "
            "as owner-reviewable artifacts. It cannot send, publish, persist or address customers."
        ),
        capabilities=frozenset({Capability.PROPOSE_DRAFT}),
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
        tags=frozenset({"branding", "content", "copy", "creative-brief"}),
        retrieval_profile=specialist_retrieval_profile(
            identity=MODULE_NAME,
            domains=frozenset({KnowledgeDomain.MARKETING}),
            top_k=8,
            token_budget=3_000,
        ),
        brief=AgentBrief(
            what_it_does=(
                "Produces grounded owner-reviewable content drafts in English, Hindi or Hinglish "
                "using the owner's supplied brand voice and facts."
            ),
            actions=("compose_content_artifact", "validate_supplied_fact_binding"),
            business_activities=(
                "prepare social and WhatsApp Status content",
                "prepare Viabe Market Intelligence launch and report-promotion copy",
            ),
            when_to_use=(
                "Use when the owner needs reviewable brand, launch, landing, social, report-promotion "
                "or WhatsApp Status copy; not when an effect or individual outreach is requested."
            ),
            limits=(
                "does not publish, send, persist or authorize an effect",
                "does not receive customer records or infer missing performance facts",
                "does not talk to the owner directly; the Manager renders the result",
            ),
        ),
    )

    def __init__(self, *, composer: ComposerFn | None = None) -> None:
        self._composer = composer

    def propose(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        del gate  # proposer facade is intentionally unused; no effect door exists.
        assignment = ctx.data.get(ASSIGNMENT_KEY)
        if not isinstance(assignment, ContentAssignment):
            raise ValueError(
                f"ContentBrandingModule requires ctx.data[{ASSIGNMENT_KEY!r}] as ContentAssignment"
            )
        artifact = (
            self._composer(assignment)
            if self._composer is not None
            else compose_content_artifact(assignment)
        )
        if not hasattr(artifact, "as_proposal"):
            raise TypeError("content composer must return an UnpersistedArtifact")
        proposal = artifact.as_proposal()
        if proposal.get("stored") is not False:
            raise RuntimeError("content proposer may not claim persistence")
        return ModuleResult(role=AgentRole.PROPOSER, status="completed", proposal=proposal)


__all__ = ["ASSIGNMENT_KEY", "ContentBrandingModule", "MODULE_NAME"]
