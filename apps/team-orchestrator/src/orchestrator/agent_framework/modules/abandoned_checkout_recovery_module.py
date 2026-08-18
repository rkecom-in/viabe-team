"""S2 abandoned-checkout recovery on the ACF contract (inert until explicit registration).

The module is dual-role but structurally sendless. Its proposer returns a recovery-plan artifact;
its executor delegates to an injected draft-building implementation that may persist an
``awaiting_approval`` batch but may not send it. No import here reaches ``customer_send`` or a
transport. The production executor remains unwired until the Reports bridge and agent-general send
rails land.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from orchestrator.agent_framework.capabilities import AgentRole, Capability
from orchestrator.agent_framework.context import ModuleContext, ModuleResult
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.manifest import AgentBrief, AgentManifest
from orchestrator.agent_framework.retrieval_profiles import specialist_retrieval_profile
from orchestrator.agents.abandoned_checkout_recovery import AGENT_NAME, AGENT_TOOLS
from orchestrator.agents.activation_registry import AgentPrerequisites
from orchestrator.knowledge_contracts import KnowledgeDomain


COHORT_KEY = "abandoned_checkout_recovery_candidates"
ExecutorFn = Callable[[ModuleContext], ModuleResult]


class S2IntegrationNotReady(RuntimeError):
    """The inert module was dispatched before its reviewed persistence/rail seams were wired."""


class AbandonedCheckoutRecoveryModule:
    """Source-neutral S2 module. Registration is proposed, never performed at import."""

    manifest = AgentManifest(
        name=AGENT_NAME,
        version="1.0.0-proposal",
        roles=frozenset({AgentRole.PROPOSER, AgentRole.EXECUTOR}),
        description=(
            "Finds consent-eligible incomplete checkouts from a source-neutral checkout adapter, "
            "builds grounded recovery drafts, and arms the existing L2 approval flow. It never "
            "sends a customer message."
        ),
        capabilities=frozenset(
            {Capability.READ_CUSTOMER_LEDGER, Capability.READ_INTEGRATION_STATE, Capability.PROPOSE_DRAFT}
        ),
        prerequisites=AgentPrerequisites(
            agent=AGENT_NAME,
            requires_journey_complete=True,
            requires_verification=True,
            requires_enabled_data_source=True,
            min_customers=1,
            requires_ownership_verified=True,
        ),
        tools=AGENT_TOOLS,
        required_tools=(
            "read_customer_ledger_summary",
            "read_business_context",
            "read_integration_state",
        ),
        category="Sales",
        tags=frozenset(
            {"abandoned-checkout", "checkout-recovery", "reports-funnel", "shopify"}
        ),
        retrieval_profile=specialist_retrieval_profile(
            identity=AGENT_NAME,
            domains=frozenset({KnowledgeDomain.SALES, KnowledgeDomain.MARKETING}),
            top_k=8,
            token_budget=3_000,
        ),
        brief=AgentBrief(
            what_it_does=(
                "Builds grounded, consent-eligible recovery drafts for incomplete Shopify or "
                "Viabe Reports checkouts and places them into the existing L2 approval flow."
            ),
            actions=(
                "read_incomplete_checkout_attempts",
                "apply_checkout_recovery_eligibility",
                "draft_grounded_recovery_copy",
                "arm_owner_approval",
            ),
            business_activities=(
                "recover incomplete purchases",
                "measure checkout recovery",
            ),
            when_to_use=(
                "Route here when an incomplete checkout is old enough to evaluate, or when the "
                "owner asks for a checkout-recovery plan or outcome."
            ),
            limits=(
                "does not infer consent from a checkout, click, phone field or prior purchase",
                "does not invent discounts, urgency, availability or delivery promises",
                "does not send; it may only build and arm an awaiting-approval draft",
                "does not talk to the owner directly; it returns to the Manager",
            ),
        ),
    )

    def __init__(self, *, executor: ExecutorFn | None = None) -> None:
        self._executor = executor

    def propose(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        del gate  # proposal lane is effect-free
        raw = ctx.data.get(COHORT_KEY, ())
        if not isinstance(raw, (tuple, list)):
            raise ValueError(f"ctx.data[{COHORT_KEY!r}] must be a list/tuple of candidate summaries")
        candidates: list[Mapping[str, Any]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError("candidate summaries must be mappings")
            # Only source-neutral, non-PII summary fields may cross the Manager/module seam.
            candidates.append(
                {
                    key: item[key]
                    for key in ("source", "attempt_id", "total_paise", "item_count", "age_minutes")
                    if key in item
                }
            )
        return ModuleResult(
            role=AgentRole.PROPOSER,
            status="completed",
            proposal={
                "agent": AGENT_NAME,
                "candidate_count": len(candidates),
                "candidates": tuple(candidates),
                "effect_authorized": False,
            },
        )

    def execute(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        del gate  # arm != send; no gated effect is exercised inside this module
        if self._executor is None:
            raise S2IntegrationNotReady(
                "S2 executor is intentionally unwired: the Reports bridge and generalized "
                "agent_send_draft policy seam must land before integration"
            )
        result = self._executor(ctx)
        if result.role is not AgentRole.EXECUTOR:
            raise ValueError("S2 executor returned a non-EXECUTOR ModuleResult")
        return result


__all__ = [
    "COHORT_KEY",
    "AbandonedCheckoutRecoveryModule",
    "ExecutorFn",
    "S2IntegrationNotReady",
]
