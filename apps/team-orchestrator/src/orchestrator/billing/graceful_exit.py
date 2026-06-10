"""Outbound-dispatch terminal guard (VT-328).

Pure deterministic predicate (Pillar 1 — no LLM, no I/O). This is the single
enforcement point (Pillar 8) that decides whether a tenant may push OUTBOUND
customer campaigns. Every present/future campaign-execution caller funnels
through :func:`dispatch_allowed`.

VT-365 (Fazal 2026-06-09): the money-clawback subsystem is GONE, so the old
read-only graceful-exit window keyed off a now-deleted terminal money-return
phase + its anchor column is removed too (``portal_access_allowed`` /
``GRACEFUL_EXIT_WINDOW``). Only the terminal/dormant outbound block survives.
"""

from __future__ import annotations

from datetime import datetime

# VT-328 / VT-365 — phases that forbid OUTBOUND customer dispatch (campaigns):
#   - ``cancelled`` — terminal; the tenant is gone.
#   - ``lapsed``    — 30-day trial expired without subscribe; dormant + re-subscribable,
#                     but with no active subscription it must not push to customers.
_DISPATCH_BLOCKED_PHASES = frozenset({"cancelled", "lapsed"})


def dispatch_allowed(
    phase: str, _unused_at: datetime | None = None, now: datetime | None = None
) -> bool:
    """Whether a tenant may dispatch OUTBOUND customer campaigns (VT-328).

    Window-independent: a ``cancelled`` or ``lapsed`` tenant is blocked from
    pushing more messages to customers, full stop (Pillar 7). The second and
    third positional parameters are accepted for call-site signature parity
    only and are unused — the block depends solely on ``phase``.

    INBOUND owner replies (DSR / opt-out) are unaffected: they never reach the
    campaign-execution chokepoint this rule guards.
    """
    return phase not in _DISPATCH_BLOCKED_PHASES
