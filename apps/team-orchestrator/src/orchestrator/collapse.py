"""Collapse path (VT-3.4 PR 3/3, migrated to CampaignPlan v1.0 by VT-122).

Consumes the ``CampaignPlan`` the specialist left in ``AgentGraphState``
and writes it through to durable storage:

  1. INSERT a row into ``campaigns`` (migration 016 + 018).
  2. UPSERT the per-tenant ``subscriber_states`` row (migration 017),
     bumping ``last_campaign_at`` and appending the new campaign id to
     ``attribution_close_pending``.

Only the ``proposed`` variant produces a ``campaigns`` row. The
``out_of_scope`` and ``insufficient_data`` variants are refusals /
defers — they do not create a campaign and so do not collapse. If
either reaches this function, fail loud: it indicates the caller did
not gate on ``plan.status`` before invoking.

The collapse path NEVER mutates ``phase`` and NEVER calls
``apply_transition``. Proposing a campaign is an activity, not a
lifecycle transition (CL-231). Phase moves later, on a real engagement
outcome, via existing ingress events — that lives in a different
subtask.

All writes go through ``tenant_connection`` so RLS is genuinely
enforced (CL-122 / Pillar 3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator._tenant_guard import TenantIsolationError
from orchestrator.agent.schemas.campaign_plan import (
    CampaignPlan,
    CampaignPlanProposed,
)
from orchestrator.db import tenant_connection
from orchestrator.state.agent_graph_state import AgentGraphState


def collapse_campaign_plan(
    tenant_id: UUID,
    run_id: UUID,
    campaign_plan: CampaignPlan,
) -> UUID:
    """Persist ``campaign_plan`` and update the subscriber's activity row.

    Returns the new ``campaigns.id``. Phase is left untouched — read
    from ``tenants`` so the upserted ``subscriber_states`` row stays
    consistent with the canonical phase mirror.

    Raises ``RuntimeError`` if ``campaign_plan`` is not the
    ``proposed`` variant — only proposed plans produce campaign rows.
    Raises ``TenantIsolationError`` if ``tenant_id`` disagrees with
    ``campaign_plan.tenant_id`` (CL-202).
    """
    if not isinstance(campaign_plan, CampaignPlanProposed):
        raise RuntimeError(
            f"collapse_campaign_plan only handles the proposed variant; "
            f"got status={campaign_plan.status.value}"
        )

    if campaign_plan.tenant_id != tenant_id:
        raise TenantIsolationError(
            f"collapse: campaign_plan.tenant_id "
            f"{campaign_plan.tenant_id} != state tenant_id {tenant_id}"
        )

    with tenant_connection(tenant_id) as conn, conn.transaction():
        # The full v1.0 plan lands in plan_json; downstream consumers
        # read structured fields via JSONB operators.
        plan_dict = campaign_plan.model_dump(mode="json")
        # dict_row factory is configured on the pool (graph.py); mypy
        # can't see it through psycopg's generic Row type, so cast at
        # the seam.
        raw_campaign = conn.execute(
            """
            INSERT INTO campaigns
                (tenant_id, run_id, plan_json, status, generated_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                str(tenant_id),
                str(run_id),
                Jsonb(plan_dict),
                # status starts at the lifecycle-initial value
                # 'proposed' (which happens to share the name with
                # the agent-terminal state — the lifecycle progression
                # is proposed → approved/rejected → sent/failed,
                # owned by VT-6 / VT-5).
                campaign_plan.status.value,
                campaign_plan.generated_at,
            ),
        ).fetchone()
        campaign_row = cast("dict[str, Any]", raw_campaign)
        campaign_id = UUID(str(campaign_row["id"]))

        raw_phase = conn.execute(
            "SELECT phase FROM tenants WHERE id = %s",
            (str(tenant_id),),
        ).fetchone()
        if raw_phase is None:
            raise TenantIsolationError(
                f"collapse: no tenants row for tenant_id {tenant_id} "
                "(RLS hid it or the row does not exist)"
            )
        phase_row = cast("dict[str, Any]", raw_phase)
        current_phase = phase_row["phase"]

        now = datetime.now(UTC)
        conn.execute(
            """
            INSERT INTO subscriber_states
                (tenant_id, phase, last_campaign_at, attribution_close_pending)
            VALUES (%s, %s, %s, ARRAY[%s]::uuid[])
            ON CONFLICT (tenant_id) DO UPDATE SET
                last_campaign_at = EXCLUDED.last_campaign_at,
                attribution_close_pending =
                    subscriber_states.attribution_close_pending
                    || EXCLUDED.attribution_close_pending
            """,
            (str(tenant_id), current_phase, now, str(campaign_id)),
        )

    return campaign_id


def collapse_node(state: AgentGraphState) -> dict[str, Any]:
    """StateGraph node wrapper. Reads run identity + plan from state, calls
    ``collapse_campaign_plan``, and returns an empty state update.

    Fail-loud on missing identifiers: tenant_id / run_id must be present by
    the time the specialist has produced a CampaignPlan; absence means an
    upstream wiring bug, not a tolerable edge case (CL-195).
    """
    tenant_id = state.get("tenant_id")
    if tenant_id is None:
        raise TenantIsolationError("collapse_node: tenant_id missing from state")
    run_id = state.get("run_id")
    if run_id is None:
        raise TenantIsolationError("collapse_node: run_id missing from state")
    plan = state.get("campaign_plan")
    if plan is None:
        raise RuntimeError(
            "collapse_node: campaign_plan missing from state — the specialist "
            "did not produce one"
        )

    collapse_campaign_plan(tenant_id=tenant_id, run_id=run_id, campaign_plan=plan)
    return {}
