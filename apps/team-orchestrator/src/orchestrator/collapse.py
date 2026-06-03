"""Collapse path (VT-3.4 PR 3/3, migrated to CampaignPlan v1.0 by VT-122,
variant-correctness fix CL-294).

Consumes the ``CampaignPlan`` the specialist left in ``AgentGraphState``
and writes it through to durable storage. The three v1.0 variants land
on DIFFERENT durable surfaces (CL-294):

  - ``proposed`` → ``collapse_campaign_plan``: INSERT a ``campaigns``
    row (migration 016 + 018), UPSERT ``subscriber_states`` (017).
  - ``out_of_scope`` / ``insufficient_data`` → ``record_terminal_verdict``:
    one ``pipeline_steps`` row (migration 006) with
    ``step_kind='campaign_plan_emitted'``, variant + reason fields in
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
from orchestrator.privacy.cohort import (
    CohortRejectedError,
    resolve_cohort_recipients,
)
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

        # VT-65 PR-2: emit campaign_created to the KG outbox IN THIS TXN (atomic
        # with the campaign INSERT — a cohort rejection below rolls both back).
        from orchestrator.knowledge.kg_emit import emit_kg_event
        from orchestrator.knowledge.kg_vocab import KgEventType

        emit_kg_event(conn, KgEventType.CAMPAIGN_CREATED, tenant_id, {
            "campaign_id": str(campaign_id),
            "status": campaign_plan.status.value,
        })

        # VT-309: L2 episodic campaign_proposed, IN THIS TXN (atomic with the
        # campaign INSERT — a cohort rejection below rolls it back too). Direct
        # in-txn emit (no outbox) + deterministic event_id → idempotent.
        from orchestrator.knowledge.l2_types import L2EventType
        from orchestrator.knowledge.l2_writer import (
            deterministic_event_id,
            record_episodic_event,
        )

        cohort_size = len(campaign_plan.target_cohort.customer_ids or [])
        record_episodic_event(
            tenant_id,
            L2EventType.CAMPAIGN_PROPOSED,
            payload={"campaign_id": str(campaign_id), "cohort_size": cohort_size},
            referenced_entity_type="campaign",
            referenced_entity_id=campaign_id,
            event_id=deterministic_event_id(
                tenant_id, L2EventType.CAMPAIGN_PROPOSED, campaign_id
            ),
            conn=conn,
        )

        # VT-241: link the cohort to campaign_recipients IN THIS TRANSACTION
        # (cur-injected, same tenant_connection). FAIL-CLOSED — if any id is
        # unresolvable / cross-tenant, raise CohortRejectedError; the
        # transaction unwinds → the campaign INSERT + any recipients roll
        # back → nothing persisted. The caller (collapse_node) surfaces the
        # structured rejection to the owner (count only) + audit log (full).
        with conn.cursor() as cohort_cur:
            cohort = resolve_cohort_recipients(
                tenant_id=str(tenant_id),
                campaign_id=str(campaign_id),
                customer_ids=[str(c) for c in campaign_plan.target_cohort.customer_ids],
                cur=cohort_cur,
            )
        if cohort.rejected:
            raise CohortRejectedError(cohort)

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

    # Post-commit: drain the outbox (immediate, best-effort; VT-307 sweep is the
    # backstop). Idempotent — never double-applies, never raises.
    from orchestrator.knowledge.kg_emit import drain_kg_events

    drain_kg_events(tenant_id)
    return campaign_id


def record_terminal_verdict(
    tenant_id: UUID,
    run_id: UUID,
    campaign_plan: CampaignPlan,
) -> None:
    """Record a non-proposed terminal verdict to ``pipeline_steps`` (CL-294).

    Writes one ``pipeline_steps`` row with
    ``step_kind='campaign_plan_emitted'``. The variant
    (``out_of_scope`` / ``insufficient_data``) lives in
    ``output_envelope['variant']``; per-variant detail fields
    (``out_of_scope_reason`` / ``suggested_specialist`` /
    ``missing_data``) also land in ``output_envelope`` so downstream
    observability can read them without a second lookup.

    Best-effort: routing failure logs but does NOT re-raise.
    Observability must not break the graph. Matches the existing
    ``error_router._log_decision`` + ``_emit_self_evaluate_gate``
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
                VALUES (%s, %s, %s, 'campaign_plan_emitted', %s, %s, 'completed')
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
            "collapse: failed to record campaign_plan_emitted verdict"
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
        try:
            campaign_id = collapse_campaign_plan(
                tenant_id=tenant_id, run_id=run_id, campaign_plan=plan
            )
        except CohortRejectedError as exc:
            # VT-241 FAIL-CLOSED: the campaign was NOT persisted (the whole
            # transaction rolled back). Audit the full rejected-id list to
            # the log layer (operator-visible — a cross-tenant cohort id is
            # a real anomaly) but surface only a COUNT to the owner: the
            # owner message must not become a cross-tenant existence oracle
            # (Cowork privacy guard). Under tenant-scoped RLS the resolver
            # can't distinguish cross-tenant from non-existent ids (both are
            # "not found"); the logged ids let operators investigate.
            res = exc.resolution
            logger.warning(
                "collapse: campaign REJECTED (fail-closed) tenant=%s run=%s "
                "rejected_count=%d rejected_ids=%s — campaign NOT persisted",
                tenant_id, run_id, len(res.rejected), res.rejected,
            )
            return {
                "campaign_rejected": {
                    "reason": "unresolved_cohort",
                    "rejected_count": len(res.rejected),
                }
            }
        # VT-47 Pillar-7: a PERSISTED proposed campaign is a sensitive action
        # that requires the owner's AUTHORITATIVE approval before any send.
        # Attach the approval request to state; route_after_collapse keys on
        # its presence to send the run to the request_owner_approval gate node
        # (which pauses via interrupt() until the owner decides). The campaign
        # is persisted as 'proposed' — it does NOT advance to 'sent' until the
        # resume path resolves an 'approved' decision (the send path is a
        # separate VT, structurally downstream of this gate).
        return {
            "pending_approval_request": _build_approval_request(
                plan=plan, campaign_id=campaign_id,
            )
        }
    # out_of_scope / insufficient_data — terminal-but-valid.
    record_terminal_verdict(
        tenant_id=tenant_id, run_id=run_id, campaign_plan=plan
    )
    return {}


def _build_approval_request(
    *, plan: CampaignPlanProposed, campaign_id: UUID
) -> dict[str, Any]:
    """Build the ``pending_approval_request`` payload the gate node consumes.

    CL-390: carries NO PII — a short summary + the campaign id + the cohort
    size. Template params for the approval message are looked up by the gate
    against the VT-163 registry; we pass a best-effort recovery figure when
    the plan exposes one, else an empty dict (the gate dry-runs the send in
    CI/canary regardless).
    """
    cohort_size = len(getattr(plan.target_cohort, "customer_ids", []) or [])
    return {
        "approval_type": "campaign_send",
        "summary": f"Approve sending a recovery campaign to {cohort_size} customers?",
        "campaign_id": campaign_id,
        "details": {"cohort_size": cohort_size},
        "template_params": {},
        "language": "en",
        "timeout_hours": 48,
    }
