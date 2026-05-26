"""Collapse path (VT-3.4 PR 3/3, migrated to CampaignPlan v1.0 by VT-122,
variant-correctness fix CL-294).

Consumes the ``CampaignPlan`` the specialist left in ``AgentGraphState``
and writes it through to durable storage. The three v1.0 variants land
on DIFFERENT durable surfaces (CL-294):

  - ``proposed`` → ``collapse_campaign_plan``: INSERT a ``campaigns``
    row (migration 016 + 018), UPSERT ``subscriber_states`` (017).
  - ``out_of_scope`` / ``insufficient_data`` → ``record_terminal_verdict``:
    one ``pipeline_steps`` row (migration 006) with
    ``step_kind='campaign_plan_terminal'``, variant + reason fields in
    ``output_envelope``. No campaign row — the agent declined to act.

Both terminal paths complete the graph cleanly. The dispatch lives in
``collapse_node``: ``collapse_campaign_plan`` itself remains strictly
proposed-only — its non-proposed RuntimeError is the fail-loud guard
against the dispatch ever routing a non-proposed plan to the campaign-
write path (defence in depth).

The collapse path NEVER mutates ``phase`` and NEVER calls
``apply_transition``. Proposing a campaign is an activity, not a
lifecycle transition (CL-231). Phase moves later, on a real engagement
outcome, via existing ingress events — that lives in a different
subtask.

All writes go through ``tenant_connection`` so RLS is genuinely
enforced (CL-122 / Pillar 3).
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


def collapse_campaign_plan(
    tenant_id: UUID,
    run_id: UUID,
    campaign_plan: CampaignPlan,
) -> UUID:
    """Persist a PROPOSED ``campaign_plan`` and update the subscriber's
    activity row. Returns the new ``campaigns.id``.

    Strictly proposed-only by design (CL-294 Disposition B). The
    non-proposed RuntimeError below is a fail-loud guard, not a path
    consumers should hit — ``collapse_node`` dispatches non-proposed
    variants to ``record_terminal_verdict`` BEFORE reaching this
    function. A non-proposed plan arriving here means a routing bug
    upstream (the dispatch broke or a future consumer called this
    function directly without gating on variant first).

    Phase is left untouched — read from ``tenants`` so the upserted
    ``subscriber_states`` row stays consistent with the canonical
    phase mirror.

    Raises ``RuntimeError`` if ``campaign_plan`` is not the proposed
    variant (defence-in-depth guard; see above).
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


def record_terminal_verdict(
    tenant_id: UUID,
    run_id: UUID,
    campaign_plan: CampaignPlan,
) -> None:
    """Record a non-proposed terminal verdict to ``pipeline_steps`` (CL-294).

    Writes one ``pipeline_steps`` row with
    ``step_kind='campaign_plan_terminal'``. The variant
    (``out_of_scope`` / ``insufficient_data``) lives in
    ``output_envelope['variant']``; per-variant detail fields
    (``out_of_scope_reason`` / ``suggested_specialist`` /
    ``missing_data``) also land in ``output_envelope`` so downstream
    observability can read them without a second lookup.

    Best-effort: routing failure logs but does NOT re-raise.
    Observability must not break the graph. Matches the existing
    ``error_router._log_decision`` + ``_emit_self_evaluate_attempt``
    persistence patterns.

    Raises ``TenantIsolationError`` if ``tenant_id`` disagrees with
    ``campaign_plan.tenant_id`` (CL-202 — kept for all three variants).
    """
    if campaign_plan.tenant_id != tenant_id:
        raise TenantIsolationError(
            f"record_terminal_verdict: campaign_plan.tenant_id "
            f"{campaign_plan.tenant_id} != state tenant_id {tenant_id}"
        )

    variant = campaign_plan.status.value
    envelope: dict[str, Any] = {
        "variant": variant,
        "version": campaign_plan.version,
        "generated_at": campaign_plan.generated_at.isoformat(),
    }
    # Per-variant detail fields. Read via getattr so this helper does
    # not import the leaf variant classes — keeps the dispatch in
    # collapse_node, the only place that branches on variant identity.
    for key in (
        "out_of_scope_reason",
        "suggested_specialist",
        "missing_data",
    ):
        if hasattr(campaign_plan, key):
            value = getattr(campaign_plan, key)
            if hasattr(value, "value"):  # enum
                envelope[key] = value.value
            elif isinstance(value, list):
                envelope[key] = [
                    item.model_dump(mode="json")
                    if hasattr(item, "model_dump")
                    else item
                    for item in value
                ]
            else:
                envelope[key] = value

    try:
        with tenant_connection(tenant_id) as conn, conn.transaction():
            raw = conn.execute(
                "SELECT COALESCE(MAX(step_seq), 0) + 1 AS next "
                "FROM pipeline_steps WHERE run_id = %s",
                (str(run_id),),
            ).fetchone()
            row = cast("dict[str, Any]", raw)
            next_index = int(row["next"])
            conn.execute(
                """
                INSERT INTO pipeline_steps
                    (run_id, tenant_id, step_seq, step_kind,
                     output_envelope, decision_rationale, status)
                VALUES (%s, %s, %s, 'campaign_plan_terminal', %s, %s, 'completed')
                """,
                (
                    str(run_id),
                    str(tenant_id),
                    next_index,
                    Jsonb(envelope),
                    f"agent terminal verdict: {variant}",
                ),
            )
    except Exception:
        # Observability must not break the graph. Log + continue.
        logger.exception(
            "collapse: failed to record campaign_plan_terminal verdict"
            " (variant=%s, run_id=%s)",
            variant,
            run_id,
        )


def collapse_node(state: AgentGraphState) -> dict[str, Any]:
    """StateGraph node wrapper. Reads run identity + plan from state and
    dispatches by CampaignPlan variant (CL-294):

      - ``CampaignPlanProposed`` → ``collapse_campaign_plan`` writes the
        ``campaigns`` row + ``subscriber_states`` upsert.
      - ``out_of_scope`` / ``insufficient_data`` → ``record_terminal_verdict``
        writes one ``pipeline_steps`` row. No campaign row.

    Both paths complete the graph cleanly. The graph reaches END
    regardless of variant — refusals and defers are legitimate terminal
    states, not failures.

    Fail-loud on missing identifiers: tenant_id / run_id must be present
    by the time the specialist has produced a CampaignPlan; absence
    means an upstream wiring bug, not a tolerable edge case (CL-195).
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

    if isinstance(plan, CampaignPlanProposed):
        collapse_campaign_plan(
            tenant_id=tenant_id, run_id=run_id, campaign_plan=plan
        )
    else:
        # out_of_scope / insufficient_data — terminal-but-valid.
        record_terminal_verdict(
            tenant_id=tenant_id, run_id=run_id, campaign_plan=plan
        )
    return {}
