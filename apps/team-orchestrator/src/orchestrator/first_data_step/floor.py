"""VT-267 PR-B — first-data-step FLOOR state machine.

The "floor" is the minimum the owner must confirm before the agent acts autonomously
on their data. State machine (Cowork-approved):

    infer  →  propose_confirm  →  confirmed            (owner confirms the inferred method)
    (un-inferable) → ask                               (agent asks; text/voice/photo/file)
    ghost (no response after max nudges) → HOLD-SAFE-MINIMAL

Persistence: NO new table (decision #3) — floor state lives in
``tenant_integration_state.pending_owner_input.metadata.floor`` (mig 031 JSONB). The
``metadata`` dict is free-form under PendingOwnerInput (which is otherwise extra=forbid),
so the floor sub-object coexists with the agent's prompt fields.

Tunable constants (Type-2): nudge_interval 24h, max_nudges 3. After max_nudges with no
confirm → state='ghost' → ``floor_complete=false`` → the agent must HOLD (refuse
autonomous action). RLS-scoped via tenant_connection (CL-71/82/88). interruption_mode
defaults to 'batch'.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator.db import tenant_connection

logger = logging.getLogger(__name__)

# --- tunable constants (Type-2; Cowork-approved) ---
NUDGE_INTERVAL = timedelta(hours=24)
MAX_NUDGES = 3
DEFAULT_INTERRUPTION_MODE = "batch"

FloorStateName = Literal["infer", "propose_confirm", "confirmed", "ask", "ghost"]


@dataclass(frozen=True, slots=True)
class FloorState:
    state: FloorStateName
    nudge_count: int
    interruption_mode: str
    floor_complete: bool
    method: str | None = None
    last_nudge_at: str | None = None
    next_nudge_at: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _floor_from_metadata(meta: dict[str, Any]) -> FloorState:
    f = (meta or {}).get("floor") or {}
    return FloorState(
        state=f.get("state", "infer"),
        nudge_count=int(f.get("nudge_count", 0)),
        interruption_mode=f.get("interruption_mode", DEFAULT_INTERRUPTION_MODE),
        floor_complete=bool(f.get("floor_complete", False)),
        method=f.get("method"),
        last_nudge_at=f.get("last_nudge_at"),
        next_nudge_at=f.get("next_nudge_at"),
    )


def _read_pending(conn: Any, tenant_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pending_owner_input FROM tenant_integration_state WHERE tenant_id = %s",
            (tenant_id,),
        )
        row = cur.fetchone()
    if row is None:
        return {}
    raw = row["pending_owner_input"] if isinstance(row, dict) else row[0]
    return raw or {}


def _persist(tenant_id: str, floor: FloorState, *, prompt_text: str, awaiting: str) -> None:
    """Upsert the tenant_integration_state row, writing the floor sub-object into
    pending_owner_input.metadata.floor (and keeping a valid PendingOwnerInput shape:
    awaiting + prompt_text)."""
    tid = str(tenant_id)
    with tenant_connection(tenant_id) as conn:
        existing = _read_pending(conn, tid)
        metadata = dict(existing.get("metadata") or {})
        metadata["floor"] = {
            "state": floor.state,
            "nudge_count": floor.nudge_count,
            "interruption_mode": floor.interruption_mode,
            "floor_complete": floor.floor_complete,
            "method": floor.method,
            "last_nudge_at": floor.last_nudge_at,
            "next_nudge_at": floor.next_nudge_at,
        }
        pending = {
            "awaiting": awaiting,
            "prompt_text": prompt_text,
            "valid_responses": existing.get("valid_responses"),
            "connector_id": existing.get("connector_id"),
            "walkthrough_url": existing.get("walkthrough_url"),
            "expires_at": existing.get("expires_at"),
            "metadata": metadata,
        }
        conn.execute(
            """
            INSERT INTO tenant_integration_state (tenant_id, pending_owner_input)
            VALUES (%s, %s)
            ON CONFLICT (tenant_id) DO UPDATE SET
                pending_owner_input = EXCLUDED.pending_owner_input,
                updated_at = now()
            """,
            (tid, Jsonb(pending)),
        )


def get_floor_state(tenant_id: UUID | str) -> FloorState:
    """Read the current floor state (defaults to 'infer'/incomplete if no row)."""
    with tenant_connection(tenant_id) as conn:
        pending = _read_pending(conn, str(tenant_id))
    return _floor_from_metadata(pending.get("metadata") or {})


def propose_method(tenant_id: UUID | str, method: str, prompt_text: str) -> FloorState:
    """infer → propose_confirm: the agent proposes the selected method, awaits confirm.
    floor_complete stays False until the owner confirms."""
    floor = FloorState(
        state="propose_confirm", nudge_count=0,
        interruption_mode=DEFAULT_INTERRUPTION_MODE, floor_complete=False, method=method,
    )
    _persist(tenant_id, floor, prompt_text=prompt_text, awaiting="field_mapping_confirm")
    logger.info("floor propose tenant=%s method=%s", tenant_id, method)
    return floor


def confirm(tenant_id: UUID | str) -> FloorState:
    """propose_confirm/ask → confirmed: owner confirmed → floor_complete=True."""
    cur = get_floor_state(tenant_id)
    floor = FloorState(
        state="confirmed", nudge_count=cur.nudge_count,
        interruption_mode=cur.interruption_mode, floor_complete=True, method=cur.method,
        last_nudge_at=cur.last_nudge_at, next_nudge_at=None,
    )
    _persist(tenant_id, floor, prompt_text="Confirmed — recording started.", awaiting="cadence_choice")
    logger.info("floor confirmed tenant=%s", tenant_id)
    return floor


def record_nudge(tenant_id: UUID | str) -> FloorState:
    """Record a nudge. After MAX_NUDGES with no confirm → state='ghost' (HOLD-safe-
    minimal; floor_complete stays False). Returns the new state."""
    cur = get_floor_state(tenant_id)
    if cur.floor_complete:
        return cur  # already done — no nudging
    count = cur.nudge_count + 1
    now = _now()
    if count >= MAX_NUDGES:
        floor = FloorState(
            state="ghost", nudge_count=count, interruption_mode=cur.interruption_mode,
            floor_complete=False, method=cur.method,
            last_nudge_at=_iso(now), next_nudge_at=None,
        )
        prompt = "No response after reminders — holding (safe minimal). Reply any time to start."
        awaiting = "field_mapping_confirm"
    else:
        floor = FloorState(
            state=cur.state if cur.state in ("propose_confirm", "ask") else "ask",
            nudge_count=count, interruption_mode=cur.interruption_mode,
            floor_complete=False, method=cur.method,
            last_nudge_at=_iso(now), next_nudge_at=_iso(now + NUDGE_INTERVAL),
        )
        prompt = "Just checking in — ready to start recording your data? Reply to begin."
        awaiting = "field_mapping_confirm"
    _persist(tenant_id, floor, prompt_text=prompt, awaiting=awaiting)
    logger.info("floor nudge tenant=%s count=%d state=%s", tenant_id, count, floor.state)
    return floor


def is_floor_complete(tenant_id: UUID | str) -> bool:
    """Fail-CLOSED gate: the agent may act autonomously ONLY if the floor is complete.
    A ghost/incomplete floor returns False → HOLD-safe-minimal."""
    return get_floor_state(tenant_id).floor_complete


__all__ = [
    "NUDGE_INTERVAL",
    "MAX_NUDGES",
    "FloorState",
    "get_floor_state",
    "propose_method",
    "confirm",
    "record_nudge",
    "is_floor_complete",
]
