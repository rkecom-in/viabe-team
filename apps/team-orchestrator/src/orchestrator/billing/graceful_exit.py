"""VT-93 — 30-day graceful-exit window (read-only portal access post-refund).

Pure deterministic predicate (Pillar 1 — no LLM, no I/O). The ENFORCEMENT point
(team-web read endpoints / middleware that returns 403) is **VT-87** (read-only
owner portal), which does not exist yet — the dashboard is a scaffold. This module
is the shared rule VT-87 imports so the cutoff lives in exactly one place
(Pillar 8). Phase-1 split: the orchestrator owns the rule + ``tenants.refunded_at``;
team-web wires the response when the portal lands.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Type-3 commitment (Pillar 7): 30 days to download data, no retention pressure.
GRACEFUL_EXIT_WINDOW = timedelta(days=30)


def portal_access_allowed(
    phase: str, refunded_at: datetime | None, now: datetime | None = None
) -> bool:
    """Whether a tenant may still access the (read-only) portal.

    - Non-``refunded`` tenants: True (their normal access is governed elsewhere).
    - ``refunded`` tenants: True for :data:`GRACEFUL_EXIT_WINDOW` from
      ``refunded_at``, then False (access revoked).
    - ``refunded`` with NULL ``refunded_at``: False — fail-closed (no anchor =
      no grace; an inconsistent row must not grant indefinite access).
    """
    if phase != "refunded":
        return True
    if refunded_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now <= refunded_at + GRACEFUL_EXIT_WINDOW


# VT-328 — terminal phases that forbid OUTBOUND customer dispatch (campaigns).
_DISPATCH_BLOCKED_PHASES = frozenset({"refunded", "cancelled"})


def dispatch_allowed(
    phase: str, refunded_at: datetime | None = None, now: datetime | None = None
) -> bool:
    """Whether a tenant may dispatch OUTBOUND customer campaigns (VT-328).

    Asymmetric with :func:`portal_access_allowed` on purpose:
    - Portal READS are window-bounded — a refunded tenant keeps read access for
      GRACEFUL_EXIT_WINDOW, then loses it.
    - Outbound WRITES (customer campaigns) are off the MOMENT the tenant is
      ``refunded`` or ``cancelled`` — in-window AND after. A terminated tenant
      must never push more messages to customers (Pillar 7). ``refunded_at`` /
      ``now`` are accepted for signature parity but unused — the block is
      window-independent.

    INBOUND owner replies (DSR / opt-out / refund-decision) are unaffected: they
    never reach the campaign-execution chokepoint this rule guards.
    """
    return phase not in _DISPATCH_BLOCKED_PHASES
