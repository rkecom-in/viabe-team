"""Business failure taxonomy (VT-29).

Nine *business* failure types, with per-type policy (severity, retryable,
default strategy, max retries, escalation threshold). The router
(``error_router.route_failure``) reads these specs to pick a
``Strategy``; the executor for that strategy consults the policy fields
(e.g. ``backoff`` reads ``max_retries``).

Two-layer rule
--------------
This module is the *business* layer. System errors (Railway crash,
transient DB drop, network timeout) belong to DBOS auto-resume — they
never become a ``FailureRecord``. Any caught business exception MUST
become a classified ``FailureRecord``; no silent swallowing (Pillar 1 /
CL-219).

Hard-limit breach (VT-29 / VT-35 split)
--------------------------------------
``agent_hard_limit_breach`` is *defined* here, in VT-29, so VT-35 can
emit it. VT-35 owns the *detection* logic (five axes: 80K tokens,
25 tool calls, depth 8, 5min wall-clock, ₹50 cost) and the runtime
counters that trip it. This module just makes the type, the axis enum,
and the routing policy exist on ``main`` ahead of VT-35.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from orchestrator.strategies import Strategy


class Severity(str, Enum):
    """Failure severity. Drives observability filtering, not routing."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FailureType(str, Enum):
    """The nine business failure types (VT-29)."""

    TOOL_CALL_TIMEOUT = "tool_call_timeout"
    TOOL_CALL_ERROR = "tool_call_error"
    AGENT_HARD_LIMIT_BREACH = "agent_hard_limit_breach"
    AGENT_REFUSAL = "agent_refusal"
    AGENT_INVALID_OUTPUT = "agent_invalid_output"
    EXTERNAL_API_ERROR = "external_api_error"
    DATABASE_ERROR = "database_error"
    WEBHOOK_SIGNATURE_FAILURE = "webhook_signature_failure"
    UNKNOWN_ERROR = "unknown_error"


class HardLimitAxis(str, Enum):
    """The five axes that can trip ``agent_hard_limit_breach`` (VT-35).

    Defined here so VT-35's detectors can emit ``FailureRecord`` with a
    structured ``metadata["axis"]`` value, and so the router / observers
    can reason about which axis fired without parsing strings.
    """

    TOKENS = "tokens"  # 80,000 tokens per run
    TOOL_CALLS = "tool_calls"  # 25 tool calls per run
    DEPTH = "depth"  # 8 levels of nesting
    WALL_CLOCK = "wall_clock"  # 5 minutes wall-clock
    COST = "cost"  # ₹50 (5000 paise) per run


@dataclass(frozen=True)
class FailureTypeSpec:
    """Policy attached to each ``FailureType``.

    Read by ``error_router.route_failure`` to pick a default strategy, and
    by retry executors to know when to stop retrying / start escalating.
    """

    severity: Severity
    retryable: bool
    default_strategy: Strategy
    max_retries: int
    escalation_threshold: int


# The nine specs. Values mirror the VT-29 subtask defaults; the router
# treats them as canonical. If product policy changes (e.g. a different
# escalation_threshold for external_api_error), update HERE — call sites
# do not hard-code these numbers.
SPECS: dict[FailureType, FailureTypeSpec] = {
    FailureType.TOOL_CALL_TIMEOUT: FailureTypeSpec(
        severity=Severity.MEDIUM,
        retryable=True,
        default_strategy=Strategy.RETRY_WITH_BACKOFF,
        max_retries=3,
        escalation_threshold=3,
    ),
    FailureType.TOOL_CALL_ERROR: FailureTypeSpec(
        severity=Severity.MEDIUM,
        retryable=True,
        default_strategy=Strategy.RETRY_WITH_BACKOFF,
        max_retries=3,
        escalation_threshold=3,
    ),
    FailureType.AGENT_HARD_LIMIT_BREACH: FailureTypeSpec(
        severity=Severity.HIGH,
        retryable=False,
        default_strategy=Strategy.ESCALATE_TO_OWNER,
        max_retries=0,
        escalation_threshold=1,
    ),
    FailureType.AGENT_REFUSAL: FailureTypeSpec(
        severity=Severity.MEDIUM,
        retryable=False,
        default_strategy=Strategy.RETRY_AFTER_OWNER_CLARIFICATION,
        max_retries=1,
        escalation_threshold=1,
    ),
    FailureType.AGENT_INVALID_OUTPUT: FailureTypeSpec(
        severity=Severity.MEDIUM,
        retryable=True,
        default_strategy=Strategy.RETRY_WITH_BACKOFF,
        max_retries=2,
        escalation_threshold=2,
    ),
    FailureType.EXTERNAL_API_ERROR: FailureTypeSpec(
        severity=Severity.MEDIUM,
        retryable=True,
        default_strategy=Strategy.RETRY_WITH_BACKOFF,
        max_retries=5,
        escalation_threshold=5,
    ),
    FailureType.DATABASE_ERROR: FailureTypeSpec(
        severity=Severity.HIGH,
        retryable=False,
        default_strategy=Strategy.ESCALATE_TO_FAZAL,
        max_retries=0,
        escalation_threshold=1,
    ),
    FailureType.WEBHOOK_SIGNATURE_FAILURE: FailureTypeSpec(
        severity=Severity.HIGH,
        retryable=False,
        default_strategy=Strategy.ACCEPT_AND_LOG,
        max_retries=0,
        escalation_threshold=1,
    ),
    FailureType.UNKNOWN_ERROR: FailureTypeSpec(
        severity=Severity.CRITICAL,
        retryable=False,
        default_strategy=Strategy.ESCALATE_TO_FAZAL,
        max_retries=0,
        escalation_threshold=1,
    ),
}


@dataclass
class FailureRecord:
    """A classified business failure.

    Constructed at the wrap-site of every caught business exception (the
    two-layer rule: no silent swallow). The router maps it to a
    ``Strategy``; the executor reads it back for retry/escalation
    context. ``metadata`` carries type-specific detail (e.g.
    ``{"axis": "tokens", "limit": 80000, "observed": 81234}`` for a
    hard-limit breach).

    ``tenant_id`` / ``run_id`` are optional ONLY because some failures
    occur pre-tenant-resolution (e.g. ``webhook_signature_failure``
    before the secret is even verified). Once tenant/run are known they
    MUST be populated.
    """

    failure_type: FailureType
    message: str
    occurred_at: datetime
    tenant_id: UUID | None = None
    run_id: UUID | None = None
    vendor: str | None = None  # for external_api_error / circuit-breaker keying
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def spec(self) -> FailureTypeSpec:
        return SPECS[self.failure_type]


__all__ = [
    "FailureRecord",
    "FailureType",
    "FailureTypeSpec",
    "HardLimitAxis",
    "SPECS",
    "Severity",
]
