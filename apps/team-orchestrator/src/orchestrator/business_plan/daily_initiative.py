"""VT-679 D2/D3/D4 — the daily proactive-initiative selection + dispatch.

The second half of §7A proactive planning (the first half is ``generator.refresh_business_plan_
workflow`` — the monthly re-ground). Once a day, per active tenant: pick the next ``accepted``
roadmap item (deterministic, NO LLM — a selection is an OUTCOME, not an intent, per the standing
LLM-for-intent/deterministic-for-outcomes split) and dispatch it through the EXISTING reactive
manager machinery (``plan_store.create_plan`` + ``start_manager_task_workflow``) — this module adds
ONLY the trigger-side selection + task-mint + owner-surface send; the loop itself is untouched.

Selection rule (D2, deterministic):
  1. ``items_for_agent(tenant, agent, statuses=("accepted",))`` across every ``OWNING_AGENTS``
     value, re-merged and sorted by the roadmap's own dense ``seq`` (plan order).
  2. The FIRST item whose idempotency key (``plan-item:{item_id}:{YYYYMM}``) has NO existing
     ``manager_tasks`` row — i.e. not already dispatched this calendar month.

Guardrails (D4, all deterministic, v1):
  - Daily cap: one initiative per tenant per call (the function returns after the first successful
    dispatch; the caller invokes it once per tenant per scheduled fire).
  - Back-pressure skip: a tenant with ANY ``manager_tasks`` row in ``_BUSY_STATUSES`` is skipped
    entirely. This set is DELIBERATELY narrower than ``task_store.TASK_ACTIVE`` — it excludes
    ``'blocked'`` — per the ratified design brief: a blocked/escalated task needs an operator's
    resolution, not to permanently starve the tenant of any future proactive pick.
  - Money floor unchanged: this module only mints a generic ``clarification`` step (mirrors
    ``triage_seam._build_draft_plan`` — the SAME minimal-template shape used for an inbound turn
    with no pre-known specialist) and lets the EXISTING manager loop reason about it; any money
    effect still lands at arm-for-approval through the loop's own unchanged rails.

Owner surface BEFORE effect (D3): the initiative's FIRST owner-visible act is a short WhatsApp
line, sent BEFORE ``start_manager_task_workflow`` — never after. A send failure is best-effort
(logged, never blocks the initiative from starting).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from orchestrator.business_plan import delivery
from orchestrator.business_plan.seams import items_for_agent, report_item_status
from orchestrator.business_plan.store import OWNING_AGENTS, RoadmapItem
from orchestrator.db import tenant_connection

logger = logging.getLogger(__name__)

# D4 back-pressure — deliberately narrower than manager.task_store.TASK_ACTIVE (excludes
# 'blocked'/'queued'/'shadow'). See module docstring.
_BUSY_STATUSES = ("running", "waiting_owner", "verifying", "clarifying", "planned")

_SURFACE_EN = (
    "From your business plan, I'm starting {item} — I'll bring you anything that needs "
    "your approval."
)
_SURFACE_HI = (
    "आपके बिज़नेस प्लान से, मैं {item} शुरू कर रहा/रही हूँ — जिसमें आपकी मंज़ूरी चाहिए होगी, "
    "वह मैं आपके पास लाऊंगा/लाऊंगी।"
)


def _idempotency_key(item_id: str, year_month: str) -> str:
    """``plan-item:{item_id}:{YYYYMM}`` (VT-679 D2.3, exact literal format — no dash in the month,
    unlike ``monthly_impact``'s ``target_month``). Month-scoped once-ness on the EXISTING
    ``manager_tasks_idem`` unique index — a redelivery/re-fire this month is a structural no-op."""
    return f"plan-item:{item_id}:{year_month}"


def _tenant_is_busy(tenant_id: UUID | str) -> bool:
    """D4 back-pressure check: does this tenant have ANY ``manager_tasks`` row in
    ``_BUSY_STATUSES``? RLS-scoped read; never piles proactive work on an already-busy manager."""
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT 1 FROM manager_tasks WHERE tenant_id = %s AND status = ANY(%s) LIMIT 1",
            (str(tenant_id), list(_BUSY_STATUSES)),
        ).fetchone()
    return row is not None


def select_next_item(tenant_id: UUID | str, year_month: str) -> RoadmapItem | None:
    """D2.1-2 — the deterministic selection: gather ``accepted`` items across every owning agent,
    re-merge into plan order (the roadmap's own dense ``seq`` — reconstituted correctly regardless
    of per-agent iteration order since ``seq`` is a single running counter over the WHOLE roadmap),
    then return the FIRST whose idempotency key has no existing task. ``None`` when there is no
    plan, no accepted items, or every accepted item already has a task this month."""
    candidates: list[RoadmapItem] = []
    for agent in sorted(OWNING_AGENTS):
        candidates.extend(items_for_agent(tenant_id, agent, statuses=("accepted",)))
    candidates.sort(key=lambda item: item.seq)

    from orchestrator.manager import task_store

    for item in candidates:
        key = _idempotency_key(item.item_id, year_month)
        if task_store.find_task_id(tenant_id, key) is None:
            return item
    return None


def _surface_initiative(tenant_id: UUID | str, item: RoadmapItem) -> None:
    """D3 — the initiative's FIRST owner-visible act, sent BEFORE ``start_manager_task_workflow``
    (never after — "surface before effect"). Best-effort: a send failure must never block the
    initiative from starting (mirrors ``delivery.deliver_plan``'s own best-effort send posture)."""
    try:
        from orchestrator.owner_surface.owner_locale import resolve_owner_locale
        from orchestrator.utils.twilio_send import (
            get_tenant_whatsapp_number,
            send_freeform_message,
        )

        tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
        recipient = get_tenant_whatsapp_number(tid)
        if not recipient:
            logger.warning(
                "VT-679 daily_initiative: tenant=%s has no whatsapp_number — skipping surface send",
                tenant_id,
            )
            return
        locale = resolve_owner_locale(tenant_id)
        template = _SURFACE_HI if locale == "hi" else _SURFACE_EN
        body = template.format(item=delivery._strip_citations(item.objective))
        send_freeform_message(body, recipient, tenant_id=tenant_id, surface="manager")
    except Exception:  # noqa: BLE001 — surface send is best-effort; the initiative still starts
        logger.exception(
            "VT-679 daily_initiative: surface-message send failed tenant=%s item=%s — "
            "continuing (the initiative still starts)",
            tenant_id,
            item.item_id,
        )


def dispatch_daily_initiative(tenant_id: UUID | str, *, now: datetime) -> dict[str, Any] | None:
    """The daily per-tenant proactive-initiative attempt (D2+D3+D4). Returns a small result dict
    when a task was actually admitted 'planned' and started, ``None`` when nothing was dispatched
    (back-pressure skip, no plan, or no accepted item left this month for this tenant)."""
    if _tenant_is_busy(tenant_id):
        return None

    year_month = now.strftime("%Y%m")
    item = select_next_item(tenant_id, year_month)
    if item is None:
        return None

    from orchestrator.manager import plan_store, task_store
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep
    from orchestrator.manager.workflow import start_manager_task_workflow

    idempotency_key = _idempotency_key(item.item_id, year_month)
    objective = delivery._strip_citations(item.objective)[:500]
    situation = delivery._strip_citations(item.why)[:500]
    plan = ManagerPlan(
        objective=objective,
        acceptance_criteria=[
            "an owner-visible reply addressing this business-plan initiative is recorded in "
            "the conversation log for this task",
        ],
        steps=[
            PlanStep(
                step_seq=1,
                kind="clarification",
                situation=situation,
                desired_outcome=objective,
            )
        ],
    )
    task_id = plan_store.create_plan(tenant_id, plan, source_message_sid=idempotency_key)

    task_row = task_store.get_task(tenant_id, task_id)
    if task_row is None or str(task_row.get("status")) != "planned":
        # create_plan admitted 'queued' (an active/blocked task already occupies the tenant's
        # one-active-task slot) or something unexpected — mirrors triage_seam's own "create_plan
        # didn't admit 'planned' — caller falls through" discipline: do NOT surface/start/flip the
        # item here. The queued task sits for the EXISTING _promote_next_queued path to pick up
        # once the tenant's active task clears; a later day's sweep will simply pick the next
        # eligible item in the meantime (this item's key already has a task, so it's skipped).
        logger.info(
            "VT-679 daily_initiative: tenant=%s item=%s created task=%s but NOT admitted "
            "'planned' (status=%s) — not surfacing/starting this run",
            tenant_id,
            item.item_id,
            task_id,
            task_row.get("status") if task_row else None,
        )
        return {
            "task_id": str(task_id),
            "item_id": item.item_id,
            "owning_agent": item.owning_agent,
            "status": task_row.get("status") if task_row else None,
        }

    _surface_initiative(tenant_id, item)

    start_manager_task_workflow(tenant_id, task_id)

    try:
        report_item_status(tenant_id, item.item_id, "in_progress", agent=item.owning_agent)
    except Exception:  # noqa: BLE001 — the task is already dispatched; a status-mark failure
        # here must not be treated as "nothing happened" (it did) nor crash the sweep.
        logger.exception(
            "VT-679 daily_initiative: report_item_status failed tenant=%s item=%s — task "
            "already dispatched, continuing",
            tenant_id,
            item.item_id,
        )

    return {
        "task_id": str(task_id),
        "item_id": item.item_id,
        "owning_agent": item.owning_agent,
        "status": "planned",
    }


__all__ = [
    "dispatch_daily_initiative",
    "select_next_item",
]
