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
from typing import Any, NamedTuple, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator._tenant_guard import TenantIsolationError
from orchestrator.agent.schemas.campaign_plan import (
    CampaignPlan,
    CampaignPlanProposed,
)
from orchestrator.db import tenant_connection
from orchestrator.db.wrappers import (
    LAPSED_WINDOW_DAYS,
    CampaignsWrapper,
    CustomersWrapper,
    PendingApprovalsWrapper,
)
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

# Build R4 — expected-recovery clamp constants + basis copy.
# Pillar-7: band constants await Fazal tuning (plan "contested": expected_arrr derivation
# constants). The owner-facing recovery ₹ range is grounded by clamping the LLM's expected_arrr
# DOWN to a conservative response band of the cohort's OWN past order sizes (per-customer average
# 'sale' value). The clamp can only SHRINK an inflated model figure, never inflate it. Fazal
# confirms the 10-30% band + the basis phrasing before the dev->main promotion; retuning these
# constants does NOT change the mechanism (grounding the number in the cohort's own spend is
# trust-floor, not a product call).
_ARRR_RESPONSE_BAND_LOW_PCT = 10
_ARRR_RESPONSE_BAND_HIGH_PCT = 30
_ARRR_BASIS_NOTE_EN = "based on their past order sizes"
_ARRR_BASIS_NOTE_HI = "उनके पिछले ऑर्डर के आधार पर"


def _complete_message_plan(conn: Any, tenant_id: UUID, plan_dict: dict[str, Any]) -> None:
    """VT-633 — make ``plan_dict['message_plan']`` satisfy its template's registry signature
    IN PLACE, deterministically, before the plan is persisted/armed/approved.

    Repairs (all deterministic, no LLM):
      - a param whose value is a literal angle-bracket placeholder ("<customer_name>") is
        treated as MISSING (the send path substitutes nothing — it would render verbatim);
      - ``business_name``  ← the tenant's real ``tenants.business_name``;
      - ``offer_description`` ← the plan's own ``personalization`` copy (the specialist's
        approved text), length-capped;
      - ``customer_name`` ← the register-neutral "ji" (V1 — real per-recipient personalization
        needs VT-45 per-recipient params; a follow-up row);
      - any OTHER missing registry var ← "" (rare; logged);
      - params the registry does NOT accept are dropped (extra_template_params is as fatal at
        execution as missing).

    Fail-soft end to end: an unknown template/language or ANY internal error leaves the plan
    untouched (the execution envelope still guards) — repairing must never break a proposal."""
    try:
        from orchestrator.templates_registry import resolve

        mp = plan_dict.get("message_plan")
        if not isinstance(mp, dict) or not mp.get("template_id"):
            return
        entry = resolve(str(mp["template_id"]), str(mp.get("language") or "en"))
        expected = tuple(entry.variables)
        params = {k: str(v) for k, v in (mp.get("template_params") or {}).items()}
        # Placeholder values are as unsendable as absent ones.
        params = {
            k: v for k, v in params.items()
            if not (v.startswith("<") and v.endswith(">"))
        }
        business_name = ""
        if "business_name" in expected and not params.get("business_name"):
            row = conn.execute(
                "SELECT business_name FROM tenants WHERE id = %s", (str(tenant_id),)
            ).fetchone()
            business_name = str(
                (row.get("business_name") if isinstance(row, dict) else row[0]) or ""
            ) if row is not None else ""
        fills = {
            "business_name": business_name,
            "offer_description": str(mp.get("personalization") or "")[:512],
            "customer_name": "ji",
        }
        repaired = {}
        for var in expected:
            if params.get(var):
                repaired[var] = params[var]
            else:
                repaired[var] = fills.get(var, "")
                if var not in fills:
                    logger.info(
                        "collapse: message_plan var %r had no deterministic fill (tenant=%s)",
                        var, tenant_id,
                    )
        dropped = sorted(set(params) - set(expected))
        if dropped:
            logger.info(
                "collapse: dropped non-registry message_plan params %s (tenant=%s)",
                dropped, tenant_id,
            )
        mp["template_params"] = repaired
    except Exception:  # noqa: BLE001 — repair must never break a proposal
        logger.warning(
            "collapse: message_plan repair failed (fail-soft, plan unchanged) tenant=%s",
            tenant_id, exc_info=True,
        )


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
        # VT-633 — deterministic message_plan REPAIR before persist: the owner must only ever
        # approve a SENDABLE plan. The specialist's LLM-authored message_plan routinely under-
        # fills the template's registry signature (live: team_winback_offer requires
        # (customer_name, business_name, offer_description); the plan carried only customer_name
        # — and as the literal placeholder "<customer_name>") — every recipient then failed at
        # execution with missing_template_params AFTER the owner had already approved. Repair is
        # registry-driven and fail-soft (see _complete_message_plan).
        _complete_message_plan(conn, tenant_id, plan_dict)
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


class _SummaryMoney(NamedTuple):
    """Money facts for the owner-facing plan summary (build R4), all fail-soft.

    ``low_rupees`` / ``high_rupees`` are the recovery ₹ range the owner actually
    sees — the LLM's ``expected_arrr`` CLAMPED DOWN to what the cohort's OWN past
    spend supports (never inflated). ``arrr_grounded`` is True only when a real
    per-customer spend figure was read and the cap computed (so the "based on
    their past order sizes" basis is honest). ``total_lapsed`` is the tenant's
    FULL lapsed count for the N-of-M scope clause, or None when it could not be
    read (→ no clause; summary stays byte-identical to today).
    """

    low_rupees: int
    high_rupees: int
    arrr_grounded: bool
    total_lapsed: int | None


def _derive_summary_money(
    plan: CampaignPlanProposed, tenant_id: UUID
) -> _SummaryMoney:
    """Ground the plan-summary money in the cohort's OWN data (build R4), fail-soft.

    Two trust-floor reads (fixes routing_db_proof turn-1 *unanchored*
    expected_arrr + sr_consequential_bulk *silent scope narrowing*):

      1. Expected-recovery clamp — sum each cohort customer's per-customer
         AVERAGE 'sale' value (their typical past order size), take the
         conservative response band of it, and CLAMP the LLM's ``expected_arrr``
         DOWN to that (element-wise ``min``). The clamp can only SHRINK an
         inflated model number, never inflate it; it is applied ONLY when real
         spend was found (else the LLM range stands verbatim).
      2. N-of-M scope — the tenant's TOTAL lapsed count via the VT-632 single
         definition (``count_lapsed`` @ ``LAPSED_WINDOW_DAYS`` — never re-literal
         45), so a silently-narrowed cohort becomes visible to the owner.

    Runs at plan-accept (collapse), strictly BEFORE the approval is armed — it
    never fires on an already-armed plan (a resume does not re-enter collapse),
    so armed plans stay immutable.

    Fail-soft end to end: ANY read error (or absent spend) returns the LLM figure
    verbatim + ``total_lapsed=None``, so the summary is byte-identical to the
    pre-R4 output — grounding must never break a proposal.
    """
    llm_low_rupees = plan.expected_arrr.low_paise // 100
    llm_high_rupees = plan.expected_arrr.high_paise // 100
    try:
        ids = [str(c) for c in plan.target_cohort.customer_ids]
        with tenant_connection(tenant_id) as conn:
            total_lapsed = CustomersWrapper().count_lapsed(
                tenant_id, days=LAPSED_WINDOW_DAYS, conn=conn
            )
            # Per-customer AVERAGE 'sale' value, summed across the cohort.
            # customer_ledger_entries is NOT a no-direct-tenant-db-access hot
            # table (the gate watches customers/campaigns/pending_approvals/…,
            # not the ledger); RLS still scopes the read via tenant_connection.
            row = conn.execute(
                "SELECT COALESCE(SUM(per.aov), 0) AS cohort_aov_sum FROM ("
                "  SELECT customer_id, AVG(amount_paise) AS aov "
                "  FROM customer_ledger_entries "
                "  WHERE tenant_id = %(tid)s AND entry_type = 'sale' "
                "    AND customer_id = ANY(%(ids)s::uuid[]) "
                "  GROUP BY customer_id"
                ") per",
                {"tid": str(tenant_id), "ids": ids},
            ).fetchone()
        cohort_aov_sum = int(
            (row.get("cohort_aov_sum") if isinstance(row, dict) else row[0]) or 0
        ) if row is not None else 0
        if cohort_aov_sum > 0:
            derived_low_paise = cohort_aov_sum * _ARRR_RESPONSE_BAND_LOW_PCT // 100
            derived_high_paise = cohort_aov_sum * _ARRR_RESPONSE_BAND_HIGH_PCT // 100
            # Clamp DOWN over the LLM figure — never inflate.
            clamped_low = min(plan.expected_arrr.low_paise, derived_low_paise)
            clamped_high = min(plan.expected_arrr.high_paise, derived_high_paise)
            return _SummaryMoney(
                low_rupees=clamped_low // 100,
                high_rupees=clamped_high // 100,
                arrr_grounded=True,
                total_lapsed=total_lapsed,
            )
        return _SummaryMoney(
            low_rupees=llm_low_rupees,
            high_rupees=llm_high_rupees,
            arrr_grounded=False,
            total_lapsed=total_lapsed,
        )
    except Exception:  # noqa: BLE001 — an honesty read must never break a proposal
        logger.warning(
            "collapse: summary-money derivation failed (fail-soft, LLM figure "
            "stands) tenant=%s",
            tenant_id,
            exc_info=True,
        )
        return _SummaryMoney(llm_low_rupees, llm_high_rupees, False, None)


def _build_chat_summary_body(
    plan: CampaignPlanProposed,
    tenant_id: UUID,
    *,
    money: _SummaryMoney | None = None,
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
    # Build R4: derive the clamped recovery range + total-lapsed scope count.
    # ``money`` is threaded in by ``_build_approval_request`` so the derivation
    # runs ONCE per turn (and the approval template {{3}} shows the same range);
    # a direct caller (unit tests / a lone summary render) may omit it and let
    # this compute — both paths are fail-soft.
    if money is None:
        money = _derive_summary_money(plan, tenant_id)
    low_rupees = money.low_rupees
    high_rupees = money.high_rupees
    start = window.start.strftime("%d %b")
    end = window.end.strftime("%d %b")
    # Basis note — only when the ₹ range is actually grounded in cohort spend.
    arrr_basis_en = f" {_ARRR_BASIS_NOTE_EN}" if money.arrr_grounded else ""
    arrr_basis_hi = f" ({_ARRR_BASIS_NOTE_HI})" if money.arrr_grounded else ""
    # N-of-M scope clause — only when the tenant has MORE lapsed customers than
    # this plan targets (LLM narrowing made visible). Absent when equal or
    # unknown, so a cohort==total plan stays byte-identical to today.
    nofm_en = ""
    nofm_hi = ""
    if money.total_lapsed is not None and money.total_lapsed > cohort.cohort_size:
        nofm_en = (
            f" {money.total_lapsed} of your customers count as lapsed in total; "
            f"this plan targets {cohort.cohort_size} — reply with any changes to widen it."
        )
        nofm_hi = (
            f" कुल {money.total_lapsed} ग्राहक लैप्स्ड हैं; यह योजना "
            f"{cohort.cohort_size} को लक्षित करती है — बदलाव के लिए जवाब दें।"
        )
    # RC1 (Fazal 2026-07-12 trust-floor): the approval template is armed + sent THIS turn, right
    # after this summary — so a future-tense promise of a separate approval message falsely commits
    # to something that never arrives (read as a loop_stall). Present-tense it + state the gate.
    en = (
        f"I've drafted a campaign for {cohort.cohort_size} customers "
        f"({label}), running {start}–{end}, with an expected "
        f"recovery of ₹{low_rupees:,}–₹{high_rupees:,}{arrr_basis_en}. Here's the approval "
        f"request — reply to approve, and nothing goes out until you do.{nofm_en}"
    )
    # Hindi wrapper, English detail set off in its own clause after the colon —
    # brain-composed per-language copy is the program end-state; this
    # deterministic splice is the interim (VT-594 review MINOR note).
    hi = (
        f"मैंने {cohort.cohort_size} ग्राहकों ({label}) के लिए एक "
        f"अभियान तैयार किया है: {start}–{end}, अनुमानित वसूली "
        f"₹{low_rupees:,}–₹{high_rupees:,}{arrr_basis_hi}। यह रहा अनुमोदन अनुरोध — मंज़ूरी देने "
        f"के लिए जवाब दें; जब तक आप मंज़ूर नहीं करते, कुछ नहीं भेजा जाएगा।{nofm_hi}"
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
    # Build R4: derive the money facts ONCE (clamped recovery range + N-of-M
    # scope count) and reuse them for BOTH the approval template {{3}} and the
    # chat summary, so the owner sees the SAME cohort-grounded ₹ range on both
    # surfaces. Fail-soft to the LLM figure (see _derive_summary_money).
    money = _derive_summary_money(plan, tenant_id)
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
            "3": f"{money.low_rupees:,}–{money.high_rupees:,}",
        },
        "language": "en",
        "timeout_hours": 48,
        "chat_summary": _build_chat_summary_body(plan, tenant_id, money=money),
    }
