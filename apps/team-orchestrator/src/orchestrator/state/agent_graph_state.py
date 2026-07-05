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

from typing import Annotated, Any
from uuid import UUID

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from orchestrator.agent.schemas.campaign_plan import CampaignPlan
from orchestrator.context_builder import SalesRecoveryContext
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
    # VT-47 — Pillar-7 owner-approval gate (additive, total=False):
    #   pending_approval_request: the approval payload the collapse path
    #     attaches when a proposed campaign needs owner sign-off. Routing
    #     to the approval-gate node keys on its presence.
    #   owner_decision: the resolved decision the gate node returns after
    #     resume ('approved'|'rejected'|'needs_changes'|'timeout') or
    #     'send_failed' when the template send failed (no pause fired).
    #   approval_id / approval_error: the durable pending_approvals row id +
    #     (on send failure) the structured error envelope.
    pending_approval_request: dict[str, Any] | None
    owner_decision: str | None
    approval_id: UUID | None
    approval_error: dict[str, Any] | None
    # VT-251 — campaign execution seam (additive, total=False):
    #   campaign_execution_summary: count-only summary from execute_approved_campaign
    #     {sent, skipped_opt_out, failed}. Set when owner_decision='approved' and
    #     the fan-out runs. CL-390: counts only, no PII.
    #   campaign_execution_error: exception type name if the seam errors
    #     (e.g. RuntimeError from a missing campaign row). Absent on success.
    campaign_execution_summary: dict[str, int] | None
    campaign_execution_error: str | None
    # VT-606 (Loop Package 3, enforce-mode ONLY — absent/None in legacy/shadow) — the plan-store
    # identifiers + step framing manager_task_workflow populates before invoking the graph for ONE
    # specialist-dispatch attempt, and manager_review_node reads to run the review + persist its
    # decision. additive + total=False: every pre-VT-606 caller (legacy/shadow mode) never sets
    # these, so the graph shape and behavior for those modes is unaffected by their presence here.
    manager_task_id: UUID | None
    manager_step_id: UUID | None
    manager_step_situation: str | None
    manager_step_desired_outcome: str | None
    manager_step_acceptance_criteria: list[str] | None
    manager_has_next_step: bool | None
    # VT-607 fix round (adversarial review) — manager_review_node's PRIMARY output. Declared here
    # (it was NOT — only manager_review_revised_outcome, below, was ever added) so LangGraph
    # actually merges it as a real channel; every reader (_dispatch_specialist_step's
    # terminal_state.get("manager_review_outcome"), the "escalate" fallback) was silently reading
    # None back for ANY clean (non-interrupted) terminal, since an undeclared TypedDict key is
    # dropped rather than merged — masked everywhere else because every dispatch this loop has
    # actually exercised end-to-end so far paused on the approval gate first (paused_approval is
    # computed from is_paused, never from this key) or was reached via a mocked
    # _dispatch_specialist_step (bypassing the real graph merge entirely). Caught by a MINOR fix-
    # round test asserting the pipeline_runs close-status for a CLEAN 'complete' terminal — the
    # SAME string coincidentally being the "escalate" fallback's own value is what let this hide
    # even in the sibling 'escalate' test.
    manager_review_outcome: str | None
    # VT-606 round-3 (adversarial-review fix, MAJOR #4) — manager_review_node's OUTPUT: the
    # reframed desired_outcome to re-dispatch with on a revise_step decision (ManagerDecision.
    # revised_outcome). None for every other outcome. workflow.py reads this to actually APPLY the
    # revision (build a replacement PlanStep) instead of silently re-claiming the stale original.
    manager_review_revised_outcome: str | None
