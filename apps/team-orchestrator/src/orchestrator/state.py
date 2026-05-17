"""SubscriberState — the canonical subscriber-lifecycle state schema (VT-3.2).

A TypedDict (LangGraph-idiomatic; consistent with VT-3.1's OrchestratorState).
It supersedes VT-3.8's Pydantic ``Tenant`` stub as the state carried through
the orchestrator. Phase is mutated ONLY by ``transitions.apply_transition`` —
the orchestrator-agent and specialists read phase but never mutate it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypedDict
from uuid import UUID, uuid4

Phase = Literal[
    "onboarding",
    "trial",
    "trial_extended",
    "paid_active",
    "paid_at_risk",
    "cancelled",
    "refunded",
]

# Phases with no outgoing transitions — see transitions.TRANSITIONS.
TERMINAL_PHASES: frozenset[Phase] = frozenset({"cancelled", "refunded"})

# Max trial extensions (covers a 14 -> 60 day trial). Enforced as an invariant.
MAX_TRIAL_EXTENSIONS = 3


class SubscriberState(TypedDict):
    """Canonical lifecycle state for one subscriber. Fields per VT-3.2 Notion §1."""

    tenant_id: UUID
    run_id: UUID
    phase: Phase
    phase_entered_at: datetime
    trial_started_at: datetime | None
    trial_extension_count: int
    paid_conversion_at: datetime | None
    last_campaign_at: datetime | None
    # Campaign ids awaiting their T+7 attribution close.
    attribution_close_pending: list[UUID]
    total_arrr_paise: int
    cumulative_fees_paid_paise: int
    escalation_pending: bool
    last_owner_message_at: datetime | None
    # Append-only event log within the run; complements pipeline_steps (VT-122).
    history: list[dict]


def new_subscriber_state(
    tenant_id: UUID,
    run_id: UUID | None = None,
    *,
    phase: Phase = "onboarding",
) -> SubscriberState:
    """Build a SubscriberState with default field values (TypedDict has none)."""
    return SubscriberState(
        tenant_id=tenant_id,
        run_id=run_id if run_id is not None else uuid4(),
        phase=phase,
        phase_entered_at=datetime.now(UTC),
        trial_started_at=None,
        trial_extension_count=0,
        paid_conversion_at=None,
        last_campaign_at=None,
        attribution_close_pending=[],
        total_arrr_paise=0,
        cumulative_fees_paid_paise=0,
        escalation_pending=False,
        last_owner_message_at=None,
        history=[],
    )
