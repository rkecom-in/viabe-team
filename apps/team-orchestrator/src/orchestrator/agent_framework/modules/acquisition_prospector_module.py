"""Inert ACF adapter for the sendless Acquisition Prospector.

Importing this module registers nothing. A caller supplies already-acquired public signals in the
context; the adapter normalises and scores them without a network request, database write or effect.
"""

from __future__ import annotations

from orchestrator.agent_framework.capabilities import AgentRole, Capability
from orchestrator.agent_framework.context import ModuleContext, ModuleResult
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.manifest import AgentBrief, AgentManifest
from orchestrator.agent_framework.retrieval_profiles import specialist_retrieval_profile
from orchestrator.knowledge_contracts import KnowledgeDomain, NoResultBehavior

MODULE_NAME = "acquisition_prospector"
SIGNALS_KEY = "public_launch_signals"


class AcquisitionProspectorModule:
    manifest = AgentManifest(
        name=MODULE_NAME,
        version="0.1.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description=(
            "Research-only Indian F&B acquisition specialist. Normalises and scores public "
            "pre-launch evidence into an inert prospect-list proposal; it cannot contact anyone."
        ),
        capabilities=frozenset({Capability.PROPOSE_DRAFT}),
        prerequisites=None,
        tools=(),
        required_tools=(),
        category="Marketing",
        tags=frozenset({"acquisition", "prospecting", "research", "fnb", "prelaunch"}),
        retrieval_profile=specialist_retrieval_profile(
            identity=MODULE_NAME,
            domains=frozenset({KnowledgeDomain.MARKETING, KnowledgeDomain.SALES}),
            top_k=8,
            token_budget=3_000,
            no_result_behavior=NoResultBehavior.CONTINUE_WITHOUT_KNOWLEDGE,
            allow_disputed=False,
        ),
        brief=AgentBrief(
            what_it_does=(
                "Builds a source-bound, scored list of pre-launch Indian F&B prospects from "
                "public launch signals and marks stale evidence for revalidation."
            ),
            actions=("normalise_public_signal", "score_prospect", "deduplicate_prospect_list"),
            business_activities=("research potential buyers for a Feasibility report",),
            when_to_use=(
                "Use when the Manager needs a research list of pre-launch Indian F&B founders or "
                "operators, with evidence and why-now context, before any outreach decision."
            ),
            limits=(
                "research artifacts only — no email, WhatsApp, ad publication or customer send",
                "a public phone number is discarded and never treated as a consent basis",
                "does not acquire web pages itself; a governed read-only adapter supplies evidence",
            ),
        ),
    )

    def propose(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        del gate  # proposer facade is intentionally unused; this module has no gated capability
        raw_signals = ctx.data.get(SIGNALS_KEY, ())
        if not isinstance(raw_signals, (list, tuple)):
            return ModuleResult(
                role=AgentRole.PROPOSER,
                status="refused",
                reason=f"{SIGNALS_KEY} must be a list or tuple",
            )
        from orchestrator.agents.acquisition_prospector import (
            build_prospect_list,
            signal_from_mapping,
        )

        try:
            signals = tuple(signal_from_mapping(raw) for raw in raw_signals)
            prospects = build_prospect_list(signals, limit=min(50, max(1, len(signals))))
        except (KeyError, TypeError, ValueError) as exc:
            return ModuleResult(
                role=AgentRole.PROPOSER,
                status="refused",
                reason=f"invalid public launch signal: {exc}",
            )
        return ModuleResult(
            role=AgentRole.PROPOSER,
            status="completed",
            proposal={
                "artifact_type": "acquisition_prospect_list",
                "outreach_authorized": False,
                "prospects": [prospect.as_dict() for prospect in prospects],
            },
        )


__all__ = ["MODULE_NAME", "SIGNALS_KEY", "AcquisitionProspectorModule"]

