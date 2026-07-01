"""VT-552 (B1 part-2b) — the run terminal-outcome enum + classifier.

pipeline_runs carries ``status`` + ``final_outcome``; this puts a canonical vocabulary over how a
terminal run actually ended, and names the dangerous case the B1 arc is about: a run that reached
``status='completed'`` but with NO definitive outcome and NO effect — a SILENT TERMINAL (the owner
never hears, ops never sees). Pure + dep-less — safe to import anywhere.
"""

from __future__ import annotations

from enum import Enum

_TERMINAL_STATUSES = frozenset(
    {"completed", "escalated", "aborted_hard_limit", "duplicate_rejected"}
)


class TerminalOutcome(str, Enum):
    RUNNING = "running"                             # not terminal yet
    COMPLETED_WITH_OUTCOME = "completed_with_outcome"
    COMPLETED_SILENT = "completed_silent"           # ended clean but no outcome + no effect
    ESCALATED = "escalated"
    ABORTED = "aborted"
    REJECTED = "rejected"


def classify_terminal(
    *, status: str | None, final_outcome: str | None, has_effect: bool = False
) -> TerminalOutcome:
    """Classify a run row's terminal outcome. ``has_effect`` (a real send/mutation was recorded) makes
    a completed run non-silent even when ``final_outcome`` is unset."""
    if status == "escalated":
        return TerminalOutcome.ESCALATED
    if status == "aborted_hard_limit":
        return TerminalOutcome.ABORTED
    if status == "duplicate_rejected":
        return TerminalOutcome.REJECTED
    if status == "completed":
        if (final_outcome or "").strip() or has_effect:
            return TerminalOutcome.COMPLETED_WITH_OUTCOME
        return TerminalOutcome.COMPLETED_SILENT
    return TerminalOutcome.RUNNING


def is_terminal(status: str | None) -> bool:
    return status in _TERMINAL_STATUSES


def is_silent_terminal(
    *, status: str | None, final_outcome: str | None, has_effect: bool = False
) -> bool:
    return (
        classify_terminal(status=status, final_outcome=final_outcome, has_effect=has_effect)
        is TerminalOutcome.COMPLETED_SILENT
    )


__all__ = ["TerminalOutcome", "classify_terminal", "is_terminal", "is_silent_terminal"]
