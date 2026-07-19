"""VT-683 P1 — the ONE session-window truth (Fazal ruling 2026-07-18: minimal template
whitelist; all other owner comms ride the 24h conversation session).

Generalizes the window logic that ``manager/stale_resume.py`` proved in the waiting-on-owner
loop into a shared module every owner-comms surface reads. ``stale_resume`` re-imports from
here — one definition, never re-derived (the LAPSED_WINDOW_DAYS discipline, applied to the
session window).

Derived from ``conversation_log`` (role='owner', the composite (tenant_id, created_at DESC)
index covers the read) — no new column, no migration. A fresh tenant that has never messaged
reads as CLOSED (fail-toward-template, the safe side: a freeform to a closed window dies with
Twilio 63016).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from orchestrator.db.tenant_connection import tenant_connection

logger = logging.getLogger("orchestrator.owner_surface.session_window")

#: WhatsApp's customer-service window — the period after an owner inbound during which
#: freeform (non-template) sends are allowed.
SESSION_WINDOW = timedelta(hours=24)


def last_owner_inbound_at(tenant_id: UUID | str) -> datetime | None:
    """The tenant's most recent OWNER-authored ``conversation_log`` turn, or ``None`` if the
    owner has never messaged (treated as window-CLOSED everywhere)."""
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT MAX(created_at) AS last_at FROM conversation_log "
            "WHERE tenant_id = %s AND role = 'owner'",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    val = row["last_at"] if isinstance(row, dict) else row[0]
    return val if isinstance(val, datetime) else None


def window_open(last_inbound_at: datetime | None, *, now: datetime | None = None) -> bool:
    """Pure predicate: is the 24h freeform window OPEN for an owner whose last inbound was
    ``last_inbound_at``? ``None`` (never messaged) → False."""
    now = now or datetime.now(timezone.utc)
    if last_inbound_at is None:
        return False
    return (now - last_inbound_at) <= SESSION_WINDOW


def session_open(tenant_id: UUID | str) -> bool:
    """DB-backed: is the tenant's 24h session currently open? Fail-CLOSED on any read error —
    a caller that can't prove the window is open must not attempt a freeform-only send."""
    try:
        return window_open(last_owner_inbound_at(tenant_id))
    except Exception:  # noqa: BLE001 — an unreadable window is a closed window
        logger.warning("session_window: read failed tenant=%s (fail-closed)", tenant_id)
        return False


def idle_minutes(tenant_id: UUID | str, *, now: datetime | None = None) -> float | None:
    """Minutes since the owner's last inbound — the VT-683 idle-pace signal (deliver queued
    items only when the owner isn't mid-exchange). ``None`` when the owner never messaged or
    the read fails."""
    try:
        last = last_owner_inbound_at(tenant_id)
    except Exception:  # noqa: BLE001 — best-effort signal
        logger.warning("session_window: idle read failed tenant=%s", tenant_id)
        return None
    if last is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - last).total_seconds() / 60.0)


__all__ = [
    "SESSION_WINDOW",
    "idle_minutes",
    "last_owner_inbound_at",
    "session_open",
    "window_open",
]
