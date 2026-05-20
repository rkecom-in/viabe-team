"""Recovery strategies for classified business failures (VT-29).

The five strategies the ``error_router`` may select. Strategies are values
(enum), not closures — the executor that consumes a ``Strategy`` decides
how to act on it (e.g. ``retry_with_backoff`` consults ``backoff.compute``;
``escalate_to_owner`` enqueues an owner-facing WhatsApp template; etc.).

Two-layer rule (VT-29): these strategies apply ONLY to *business* failures.
System errors (Railway crash, transient DB drop, network blip) are
DBOS auto-resume and never reach this module.
"""

from __future__ import annotations

from enum import Enum


class Strategy(str, Enum):
    """The five recovery strategies a classified failure can route to.

    Values are strings so a ``Strategy`` round-trips cleanly through the
    JSONB ``pipeline_steps.error_envelope`` column (CL-122 / VT-12.2).
    """

    RETRY_WITH_BACKOFF = "retry_with_backoff"
    RETRY_AFTER_OWNER_CLARIFICATION = "retry_after_owner_clarification"
    ESCALATE_TO_OWNER = "escalate_to_owner"
    ESCALATE_TO_FAZAL = "escalate_to_fazal"
    ACCEPT_AND_LOG = "accept_and_log"


__all__ = ["Strategy"]
