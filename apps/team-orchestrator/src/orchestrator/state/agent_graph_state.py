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

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from orchestrator.types.campaign_plan import CampaignPlan


class AgentGraphState(TypedDict, total=False):
    """State for the parent multi-agent graph in PR 1/3.

    total=False because not all fields are present at graph entry; reducers
    accumulate them as the graph executes.
    """

    messages: Annotated[list[AnyMessage], add_messages]
    active_agent: str | None
    campaign_plan: CampaignPlan | None
