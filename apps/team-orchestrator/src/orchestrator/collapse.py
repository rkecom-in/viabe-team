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
from orchestrator.db.wrappers import CampaignsWrapper, PendingApprovalsWrapper
from orchestrator.observability.log import log_event
from orchestrator.privacy.cohort import (
    CohortRejectedError,
    resolve_cohort_recipients,
)
from orchestrator.state.agent_graph_state import AgentGraphState

logger = logging.getLogger(__name__)

# VT-334 — per-week owner-messaging budget: at most this many campaign_send approval requests
# per owner per 7 days (the owner-fatigue guard). At the cap, the campaign is still persisted as
# 'proposed' (visible next sync) but NO approval prompt is sent this week.
_WEEKLY_APPROVAL_BUDGET = 2


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
        # VT-306: insert through the typed wrapper on the open transaction's conn
        # (atomic with the campaign-proposed emit). status starts at the
        # lifecycle-initial 'proposed' (progression proposed → approved/rejected →
        # sent/failed, owned by VT-6/VT-5). tenant_id is forced by the wrapper.
        campaign_row = CampaignsWrapper().insert(
            tenant_id,
            {
                "run_id": str(run_id),
                "plan_json": Jsonb(plan_dict),
                "status": campaign_plan.status.value,
                "generated_at": campaign_plan.generated_at,
            },
            conn=conn,
        )
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
    persistence patterns. VT-379: written via the shared redacting
    writer (``write_redacted_step_row``) — ``out_of_scope_reason`` /
    ``missing_data`` are model-authored free text and were previously
    INSERTed raw; redaction (patterns + tenant name registry) now runs
    at write.

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
        from orchestrator.observability.pipeline_observability import (
            write_redacted_step_row,
        )

        write_redacted_step_row(
            run_id=run_id,
            tenant_id=tenant_id,
            step_kind="campaign_plan_emitted",
            output_envelope=envelope,
            decision_rationale=f"agent terminal verdict: {variant}",
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
        # VT-334 per-week budget: if the owner has already had _WEEKLY_APPROVAL_BUDGET
        # campaign_send requests in the last 7 days, SKIP a new prompt (owner-fatigue guard).
        # The campaign stays persisted as 'proposed' — the owner sees it next sync; we just
        # don't send a 3rd nudge this week. Silent by design (a "we didn't notify you" message
        # would defeat the guard); logged for observability (no PII).
        if (
            PendingApprovalsWrapper().count_recent_campaign_requests(tenant_id, days=7)
            >= _WEEKLY_APPROVAL_BUDGET
        ):
            log_event(
                event_type="approval_budget_skipped",
                run_id=run_id,
                tenant_id=tenant_id,
                severity="info",
                component="collapse",
                payload={
                    "campaign_id": str(campaign_id),
                    "window_days": 7,
                    "budget": _WEEKLY_APPROVAL_BUDGET,
                },
            )
            return {}
        return {
            "pending_approval_request": _build_approval_request(
                plan=plan, campaign_id=campaign_id, tenant_id=tenant_id,
            )
        }
    # out_of_scope / insufficient_data — terminal-but-valid.
    record_terminal_verdict(
        tenant_id=tenant_id, run_id=run_id, campaign_plan=plan
    )
    return {}


def _redact_agent_label(tenant_id: UUID, text: str) -> str:
    """Redact an agent-authored field before it reaches an owner surface.

    Per-module copy of the dispatch/tm_audit/pipeline_observability pattern
    (each module keeps its own rather than cross-importing a private helper —
    and importing from agent.dispatch here would be a cycle via supervisor).
    ``cohort_label`` is schema-unconstrained free text (min_length=1 only), the
    same class as ``selection_reason`` (delta-review Defect 1): the redaction
    is a no-op on a legitimate categorical label and only fires on real PII.
    Fail-soft to pattern-only redaction; a registry outage never blocks a send.
    """
    try:
        from orchestrator.observability.pii import redact_for_log
        from orchestrator.privacy.customer_registry import make_name_registry

        try:
            registry = make_name_registry(str(tenant_id))
        except Exception:  # noqa: BLE001 — fail-soft by contract
            logger.warning(
                "collapse: name-registry build failed; pattern-only redaction "
                "tenant=%s", tenant_id,
            )
            registry = None
        out = redact_for_log(text, name_registry=registry)
        return str(out) if out else ""
    except Exception:  # noqa: BLE001 — redaction must never block the arm path
        logger.warning("collapse: label redaction failed tenant=%s", tenant_id)
        return ""


def _build_chat_summary_body(
    plan: CampaignPlanProposed, tenant_id: UUID
) -> dict[str, str]:
    """PII-safe plan-summary body (VT-594 change C, post-review restructure).

    Sent by ``request_owner_approval.arm_pause_request`` BEFORE the approval
    template, so the owner sees WHAT they're approving. Built ONLY from
    TYPED plan fields that are structurally safe: cohort size, the cohort
    label (REDACTED — the schema does not actually enforce the "short
    categorical token" convention, so it gets the same treatment as every
    other agent-authored field; delta-review Defect 1), campaign window
    dates, expected recovery ₹ range.

    Deliberately EXCLUDES ``target_cohort.selection_reason`` (review Blocker
    1, CRITICAL): that field is agent-authored free prose, and VT-498's own
    sales_recovery.py docstring documents the SR model baking literal
    customer names into exactly this kind of field. ``_build_approval_request``
    (CL-390) already carries no PII for the same reason — this summary must
    not reintroduce it via a different field.
    """
    cohort = plan.target_cohort
    window = plan.campaign_window
    label = _redact_agent_label(tenant_id, cohort.cohort_label)
    low_rupees = plan.expected_arrr.low_paise // 100
    high_rupees = plan.expected_arrr.high_paise // 100
    start = window.start.strftime("%d %b")
    end = window.end.strftime("%d %b")
    en = (
        f"I've drafted a campaign for {cohort.cohort_size} customers "
        f"({label}), running {start}–{end}, with an expected "
        f"recovery of ₹{low_rupees:,}–₹{high_rupees:,}. I'll send you the "
        "formal approval ask next."
    )
    # Hindi wrapper, English detail set off in its own clause after the colon —
    # brain-composed per-language copy is the program end-state; this
    # deterministic splice is the interim (VT-594 review MINOR note).
    hi = (
        f"मैंने {cohort.cohort_size} ग्राहकों ({label}) के लिए एक "
        f"अभियान तैयार किया है: {start}–{end}, अनुमानित वसूली "
        f"₹{low_rupees:,}–₹{high_rupees:,}। मैं अगली बार आपको औपचारिक "
        "अनुमोदन अनुरोध भेजूंगा।"
    )
    return {"en": en, "hi": hi}


def _build_approval_request(
    *, plan: CampaignPlanProposed, campaign_id: UUID, tenant_id: UUID
) -> dict[str, Any]:
    """Build the ``pending_approval_request`` payload the gate node consumes.

    CL-390: carries NO PII — a short summary + the campaign id + the cohort
    size. Template params for the approval message are looked up by the gate
    against the VT-163 registry; we pass a best-effort recovery figure when
    the plan exposes one, else an empty dict (the gate dry-runs the send in
    CI/canary regardless).

    ``chat_summary`` (VT-594 change C, post-review restructure): the PII-safe
    in-chat plan summary ``request_owner_approval.arm_pause_request`` sends
    BEFORE the approval template. Threaded here (not sent from collapse.py
    itself) because the gate node is the ONE place that knows whether the
    send is actually about to happen (an idempotent resume re-execution or a
    0b queue-busy refusal must NOT re-send it) — see arm_pause_request.
    """
    cohort = plan.target_cohort
    cohort_size = cohort.cohort_size
    low_rupees = plan.expected_arrr.low_paise // 100
    high_rupees = plan.expected_arrr.high_paise // 100
    return {
        "approval_type": "campaign_send",
        "summary": f"Approve sending a recovery campaign to {cohort_size} customers?",
        "campaign_id": campaign_id,
        "details": {"cohort_size": cohort_size},
        # VT-83: populate the team_weekly_approval params — {{1}} cohort segment /
        # {{2}} campaign mode / {{3}} projected recovery ₹ range. Previously empty, which
        # rendered a BLANK approval message to the owner (the actual bug).
        # {{1}} is agent-authored free text like every other label — redacted
        # (delta-review Defect 1; same channel, template variant).
        "template_params": {
            "1": _redact_agent_label(tenant_id, cohort.cohort_label)
            or str(cohort_size),
            "2": "recovery",
            "3": f"{low_rupees:,}–{high_rupees:,}",
        },
        "language": "en",
        "timeout_hours": 48,
        "chat_summary": _build_chat_summary_body(plan, tenant_id),
    }
