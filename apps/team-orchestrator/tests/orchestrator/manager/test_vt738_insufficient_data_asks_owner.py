"""VT-738 RV-2 — `insufficient_data` must ask the owner, never trigger a revise loop.

Observed on deployed dev 2026-08-10: the Manager delegated, `sales_recovery` returned
`insufficient_data`, `manager_review` chose `revise_step`, the step was reframed and re-dispatched
IDENTICALLY, three times, until `max_revisions_per_step_seq` blocked the task and triage cancelled
the plan. The owner got nothing useful.

The loop could never converge: re-framing a step cannot create data that does not exist. So this is
not a retry-budget tuning question — the REVISE branch was simply the wrong one, and these tests
pin the branch rather than the budget.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from orchestrator.manager.decision import ManagerDecisionKind, decide_next_action  # noqa: E402
from orchestrator.manager.review import (  # noqa: E402
    adapt_campaign_plan_to_specialist_return,
    to_legacy_specialist_return,
)


_TENANT = "11111111-1111-1111-1111-111111111111"


def _plan_insufficient(*, remediation: str | None):
    """A minimal `insufficient_data` CampaignPlan double.

    A stub rather than the real pydantic model: the branch under test reads exactly `status` and
    `missing_data[*]`, and building a full valid CampaignPlan here would test the schema, not the
    decision.
    """
    from types import SimpleNamespace

    from orchestrator.agent.schemas.campaign_plan import CampaignStatus

    return SimpleNamespace(
        status=CampaignStatus.INSUFFICIENT_DATA,
        missing_data=[SimpleNamespace(
            category="customer_history",
            description="no purchase dates on file",
            suggested_remediation=remediation,
        )],
    )


def test_owner_actionable_gap_asks_the_owner_instead_of_revising() -> None:
    """VT-755 / ruling D-A UPDATED THIS ASSERTION. It used to require the model's own
    `suggested_remediation` to appear in the owner's question:

        assert "connect your sales sheet" in ret.owner_question

    D-A (Fazal 2026-08-15) rules the opposite — raw model remediation NEVER reaches an owner, because
    `suggested_remediation` is free text written for an ENGINEERING audience ("backfill the customer
    table"). The branch still ASKS rather than revising, which is what this row was about; what
    changed is where the words come from.
    """
    ret = adapt_campaign_plan_to_specialist_return(
        _TENANT, _plan_insufficient(remediation="connect your sales sheet")
    )
    assert ret.status == "needs_owner_input"
    assert ret.owner_question, "needs_owner_input REQUIRES a question (plan_models enforces it)"
    assert "connect your sales sheet" not in ret.owner_question, (
        "the model's remediation prose reached the owner — VT-755/D-A forbids it"
    )
    assert "I can't build this yet" in ret.owner_question, (
        "the question is no longer coming from the closed vocabulary in manager.owner_ask"
    )
    # The model's words are still recorded — INTERNALLY, where they belong.
    assert "connect your sales sheet" in (ret.outcome_summary or "")
    assert ret.proposed_outcome is None, (
        "a proposed_outcome is what routed this to REVISE — it must not come back"
    )


def test_the_decision_is_clarify_not_revise() -> None:
    """The end-to-end branch, through the same adapter chain the loop uses. This is the assertion
    that actually prevents the loop: CLARIFY parks and asks; REVISE re-dispatches forever."""
    ret = adapt_campaign_plan_to_specialist_return(
        _TENANT, _plan_insufficient(remediation="connect your sales sheet")
    )
    decision = decide_next_action(to_legacy_specialist_return(ret), has_next_step=False)
    assert decision.kind is ManagerDecisionKind.CLARIFY
    assert decision.kind is not ManagerDecisionKind.REVISE


def test_no_actionable_remediation_escalates_rather_than_looping() -> None:
    """When there is nothing the owner could do, there is genuinely no path. Escalating is an
    honest closure the owner sees; looping is not. Either way it must not be REVISE."""
    ret = adapt_campaign_plan_to_specialist_return(_TENANT, _plan_insufficient(remediation=""))
    assert ret.status == "blocked"
    assert not ret.proposed_outcome, "an empty proposal is what makes decide_next_action escalate"

    decision = decide_next_action(to_legacy_specialist_return(ret), has_next_step=False)
    assert decision.kind is ManagerDecisionKind.ESCALATE


def test_the_gap_detail_survives_for_operators() -> None:
    """The owner-facing question is deliberately short; the diagnostic detail must still be on the
    return so an operator can see WHAT was missing without re-running anything."""
    ret = adapt_campaign_plan_to_specialist_return(
        _TENANT, _plan_insufficient(remediation="connect your sales sheet")
    )
    assert "customer_history" in ret.outcome_summary
    assert "no purchase dates on file" in ret.outcome_summary
    assert ret.reason_code == "insufficient_data"
