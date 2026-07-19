"""VT-474 B — the customer-SEND DECAYING-CHECKPOINT (a thin NAMING of the EXISTING decay model).

The SEND ruling (design §8, Fazal): the customer-SEND action EARNS autonomy — tight owner visibility
on the FIRST sends per new tenant/campaign → DECAYS to full autonomy once proven safe. NOT
per-send-forever. Reuse the VTR decay + owner-approval.

CRITICAL — this is NOT a new decay model. The decay ALREADY EXISTS and is ALREADY WIRED into the
send path; VT-474 B CONFIRMS + NAMES it as the design's "decaying checkpoint" curve so the lanes have
ONE legible call. The two existing pieces it composes (do NOT duplicate them):

  1. ``autonomy.get_autonomy(tenant, agent)`` — the L2/L3 trust tier (migration 129):
       * L2 (the DEFAULT — a NEW tenant / a missing row / an un-proven agent): the owner approves
         EACH send (``arm_agent_send_approval`` — the checkpoint). This IS "the FIRST sends per new
         tenant are owner-visible."
       * L3 (EARNED — a 20-clean-approval streak + an explicit owner opt-in, ``grant_l3``): the send
         runs AUTONOMOUSLY through the delivery-anchored hold (``l3_hold``). This IS "decays to full
         autonomy once proven safe." A regression TIGHTENS back to L2 (the decay is two-way).

  2. ``autonomy.is_always_confirm(...)`` — the NON-BYPASSABLE floor re-derived PER BATCH at the send
     choke (CL-438), TRUE for: first-contact (a customer with NO prior contact), novel-template
     (never sent by this tenant), bulk (> L3_AUTO_MAX_BATCH), money (a money-bearing template). ANY
     floor trip forces an L3-eligible batch BACK to the L2 checkpoint (``l3_hold.enter_l3_hold`` falls
     back to the L2 approval path). This is the "FIRST sends per new CAMPAIGN are owner-visible" leg:
     a novel template OR a first-contact customer ALWAYS checkpoints, even for a proven (L3) tenant —
     the campaign earns its own trust, not just the tenant.

So the curve the design asks for is exactly: {L2 ⇒ checkpoint} ∪ {L3 + always-confirm-floor ⇒
checkpoint} ∪ {L3 + no floor ⇒ autonomous}. This module is the ONE function that states that
composition; the actual send path (``customer_send.agent_send_draft`` via ``l3_hold.enter_l3_hold``)
already enforces it. ``send_checkpoint_decision`` lets a lane ASK "would this batch checkpoint or run
autonomously?" without re-deriving the rule — and a test PROVE the curve in one place.

Compliance rails (VT-460: consent/opt-out/onboarded/caps) are UNTOUCHED and still bind UNDERNEATH
this — autonomy decides checkpoint-vs-autonomous; the compliance gates decide send-vs-skip regardless.
A proven (L3, no-floor) batch still passes every consent/cap gate at send time.

CL-390: IDs + agent + a reason CODE + counts only — never a customer phone/name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


class SendAutonomyDecision(str, Enum):
    """The deterministic send-autonomy outcome — exactly two terminal decisions.

    - ``CHECKPOINT`` — the send is owner-visible (routes through ``arm_agent_send_approval``, the L2
      Pillar-7 approval). The FIRST sends per new tenant/campaign land here (un-proven, or a floor
      trip). This is the owner-visibility leg of the decaying checkpoint.
    - ``AUTONOMOUS`` — the send runs without per-send owner approval (the L3 delivery-anchored hold).
      A proven tenant + a non-floor batch lands here. This is the decayed-to-autonomy leg.
    """

    CHECKPOINT = "checkpoint"
    AUTONOMOUS = "autonomous"


# Deterministic reason markers (CL-390: a code, never an instruction body).
REASON_L2_NOT_PROVEN = "l2_not_proven"       # tier L2 — un-proven; every send checkpoints (the default)
REASON_FROZEN = "frozen"                     # the agent is frozen (kill switch) — checkpoint
REASON_ALWAYS_CONFIRM_FLOOR = "always_confirm_floor"  # an L3 tenant but a floor trip (first/novel/bulk/money)
REASON_L3_AUTONOMOUS = "l3_autonomous"       # tier L3 + no floor — decayed to autonomy (proven)


@dataclass(frozen=True, slots=True)
class SendCheckpointResult:
    """The deterministic decision. PII-safe (tier + reason CODE + the floor marker only)."""

    decision: SendAutonomyDecision
    reason: str
    level: str                       # the L2/L3 tier read at decision time
    floor_reason: str = ""           # the is_always_confirm marker when a floor tripped, else ""

    @property
    def checkpoint(self) -> bool:
        return self.decision is SendAutonomyDecision.CHECKPOINT

    @property
    def autonomous(self) -> bool:
        return self.decision is SendAutonomyDecision.AUTONOMOUS


def send_checkpoint_decision(
    tenant_id: UUID | str,
    *,
    agent: str,
    batch_customer_ids: list[str],
    template_name: str,
    money_bearing: bool,
    conn: Any,
) -> SendCheckpointResult:
    """Decide whether THIS customer-send batch CHECKPOINTS (owner-visible) or runs AUTONOMOUS — the
    decaying-checkpoint curve, composed from the EXISTING decay model (no new model).

    DETERMINISTIC (zero LLM). The ladder — exactly the rule the send path already enforces, named in
    ONE place:
      1. tier L2 (un-proven — a NEW tenant / a missing row) OR frozen → CHECKPOINT. The first sends
         per new tenant are owner-visible (the default; a kill switch tightens back here).
      2. tier L3 (earned) BUT the always-confirm floor trips (first-contact customer / novel template
         / bulk / money) → CHECKPOINT. The first sends per new CAMPAIGN are owner-visible even for a
         proven tenant — the campaign earns its own trust.
      3. tier L3 + no floor → AUTONOMOUS. Decayed to full autonomy once proven safe.

    REUSES ``autonomy.get_autonomy`` + ``autonomy.is_always_confirm`` verbatim — this function makes
    NO trust decision of its own; it READS the existing tier + floor. ``conn`` is the caller's
    RLS-scoped ``tenant_connection``.
    """
    from orchestrator.agents.autonomy import get_autonomy, is_always_confirm

    tid = str(tenant_id)
    state = get_autonomy(tid, agent, conn=conn)

    # 1. Un-proven (L2) or frozen → checkpoint (the first-sends-per-tenant leg; the kill-switch tighten).
    if state.frozen:
        return _result(SendAutonomyDecision.CHECKPOINT, REASON_FROZEN, state.level, "")
    if state.level != "L3":
        return _result(SendAutonomyDecision.CHECKPOINT, REASON_L2_NOT_PROVEN, state.level, "")

    # 2. L3 but a per-batch floor trip → checkpoint (the first-sends-per-CAMPAIGN leg — CL-438).
    floor, floor_reason = is_always_confirm(
        tid,
        agent=agent,
        batch_customer_ids=batch_customer_ids,
        template_name=template_name,
        money_bearing=money_bearing,
        conn=conn,
    )
    if floor:
        return _result(
            SendAutonomyDecision.CHECKPOINT, REASON_ALWAYS_CONFIRM_FLOOR, state.level, floor_reason
        )

    # 3. L3 + no floor → autonomous (decayed to full autonomy — proven).
    return _result(SendAutonomyDecision.AUTONOMOUS, REASON_L3_AUTONOMOUS, state.level, "")


def _result(
    decision: SendAutonomyDecision, reason: str, level: str, floor_reason: str
) -> SendCheckpointResult:
    logger.info(
        "send_checkpoint: decision=%s reason=%s level=%s floor=%s",
        decision.value, reason, level, floor_reason or "-",
    )
    return SendCheckpointResult(
        decision=decision, reason=reason, level=level, floor_reason=floor_reason
    )


__all__ = [
    "SendAutonomyDecision",
    "SendCheckpointResult",
    "REASON_L2_NOT_PROVEN",
    "REASON_FROZEN",
    "REASON_ALWAYS_CONFIRM_FLOOR",
    "REASON_L3_AUTONOMOUS",
    "send_checkpoint_decision",
]
