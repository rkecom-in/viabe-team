"""Collapse path (VT-3.4 PR 3/3).

Consumes the ``CampaignPlan`` PR 2/3 left in ``AgentGraphState`` and writes
it through to durable storage:

  1. INSERT a row into ``campaigns`` (migration 016).
  2. UPSERT the per-tenant ``subscriber_states`` row (migration 017),
     bumping ``last_campaign_at`` and appending the new campaign id to
     ``attribution_close_pending``.

The collapse path NEVER mutates ``phase`` and NEVER calls
``apply_transition``. Proposing a campaign is an activity, not a lifecycle
transition (CL-231). Phase moves later, on a real engagement outcome, via
existing ingress events — that lives in a different subtask.

All writes go through ``tenant_connection`` so RLS is genuinely enforced
(CL-122 / Pillar 3). The function is idempotent at the SQL level only via
the UPSERT — campaigns inserts always produce a new row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator._tenant_guard import TenantIsolationError
from orchestrator.db import tenant_connection
from orchestrator.state.agent_graph_state import AgentGraphState
from orchestrator.types.campaign_plan import CampaignPlan


def collapse_campaign_plan(
    tenant_id: UUID,
    run_id: UUID,
    campaign_plan: CampaignPlan,
) -> UUID:
    """Persist ``campaign_plan`` and update the subscriber's activity row.

    Returns the new ``campaigns.id``. Phase is left untouched — read from
    ``tenants`` so the upserted ``subscriber_states`` row stays consistent
    with the canonical phase mirror.

    Raises ``TenantIsolationError`` if ``tenant_id`` disagrees with
    ``campaign_plan.tenant_id`` — a mismatch means an upstream producer
    crossed a tenant boundary; fail loud (CL-202).
    """
    if campaign_plan.tenant_id != tenant_id:
        raise TenantIsolationError(
            f"collapse: campaign_plan.tenant_id "
            f"{campaign_plan.tenant_id} != state tenant_id {tenant_id}"
        )

    with tenant_connection(tenant_id) as conn, conn.transaction():
        campaign_row = conn.execute(
            """
            INSERT INTO campaigns
                (tenant_id, run_id, subscriber_id, template_id, body_params,
                 status, proposed_at, proposed_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                str(tenant_id),
                str(run_id),
                str(campaign_plan.subscriber_id),
                campaign_plan.template_id,
                Jsonb(campaign_plan.body_params),
                campaign_plan.status,
                campaign_plan.proposed_at,
                campaign_plan.proposed_by,
            ),
        ).fetchone()
        campaign_id = UUID(str(campaign_row["id"]))

        phase_row = conn.execute(
            "SELECT phase FROM tenants WHERE id = %s",
            (str(tenant_id),),
        ).fetchone()
        if phase_row is None:
            raise TenantIsolationError(
                f"collapse: no tenants row for tenant_id {tenant_id} "
                "(RLS hid it or the row does not exist)"
            )
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
