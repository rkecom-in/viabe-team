"""Agent-graph state for the supervisor multi-agent StateGraph (VT-3.4 PR 1/3).

Distinct from SubscriberState (the lifecycle TypedDict in this package's
``__init__``). This state's lifetime is one orchestrator->specialist
execution; it is collapsed back into SubscriberState updates on completion
(collapse logic is OUT of scope for PR 1/3 — lands in a later PR).

Fields planned for later PRs:
- PR 2/3: task_description, run_id, tenant_id (for Context Composer)
- PR 3/3: token_count, tool_call_count, depth, started_at, cost_paise
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from orchestrator.context_builder import SalesRecoveryContext
from orchestrator.types.campaign_plan import CampaignPlan
from orchestrator.types.trigger_reason import TriggerReason


class AgentGraphState(TypedDict, total=False):
    """State for the parent multi-agent supervisor graph.

    total=False because not all fields are present at graph entry; reducers
    accumulate them as the graph executes.

    tenant_id / run_id / trigger_reason are populated at graph entry by the
    real upstream producers (VT-3.3 / VT-3.5 / VT-3.8) — wiring lands in a
    later PR (PR #26 deferral pattern). VT-3.4 PR 2/3 tests supply them
    directly in the invoke() initial state. trigger_reason defaults to None at
    the schema (CL-195): the 'weekly_cadence' fallback lives only at the spawn
    tool's read site, so a missing upstream source stays observable.

    terminated_without_spawn is absent (-> None on .get()) until the terminal
    node runs and sets it True. Downstream readers MUST treat absent/None and
    False equivalently — both mean "has not terminated without spawning".
    """

    messages: Annotated[list[AnyMessage], add_messages]
    active_agent: str | None
    campaign_plan: CampaignPlan | None
    # VT-3.4 PR 2/3 additions (CL-188 / CL-195):
    tenant_id: UUID | None
    run_id: UUID | None
    trigger_reason: TriggerReason | None
    sales_recovery_context: SalesRecoveryContext | None
    terminated_without_spawn: bool
