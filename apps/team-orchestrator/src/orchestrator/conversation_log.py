"""VT-579 — the LIFETIME owner↔system conversation log + the Team-Manager active window.

CL-2026-07-03-conversation-memory-architecture (Fazal, live drill): the entire owner↔system conversation
lives in permanent storage (referred to whenever required), and the last ≤20 turns within 24h are ALWAYS
in the Team-Manager's LLM context. This module is the seam:

  - ``record_turn``      — append one turn (both directions), fail-soft + idempotent per message_sid.
  - ``active_window``    — the last ≤20 turns within 24h, CHRONOLOGICAL (oldest-first) — the always-on
                           dispatch context block (agent/dispatch.py) + the substrate the onboarding turn
                           brain's search tool reads.
  - ``search_history``   — a simple newest-first ILIKE over the lifetime log — the brain-commanded
                           retrieval tool (manager + onboarding turn brain). No embeddings yet; honest.
  - ``maybe_compact`` / ``conversation_compact_workflow`` — the VT-571 distiller pattern generalised to
                           the manager: when turns scroll out of the window they are FOLDED (off the hot
                           path) into a durable summary (agent_memory, agent='manager') the window block
                           carries ABOVE the raw turns — compact, never a silent drop.

Import discipline: this module carries the DBOS workflow decorator (dbos at top) + lazy psycopg /
anthropic / agent_memory imports inside functions, so its callers (runner / dispatch / twilio_send /
journey) import IT lazily and the dep-less smoke never pulls it in (the journey ⇄ memory_distiller split).

Fail-soft is the contract everywhere: conversation memory must NEVER block a send or a receive, and a
read miss must never break dispatch. The lifetime log is retention = lifetime-of-relationship, DSR-erased
(dsr_purge._PURGE_ORDER carries the table); text is the tenant's own conversation, never app-logged.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from dbos import DBOS

logger = logging.getLogger(__name__)

# The active window Fazal specified: last ≤20 turns, none older than 24h — ALWAYS in the manager context.
_ACTIVE_MAX_TURNS = 20
_ACTIVE_MAX_AGE_H = 24
_TEXT_CAP = 1000  # per-turn text cap; a runaway body is truncated, never a wall of text in context.

# Compaction trigger: once MORE than this many turns have scrolled OUT of the active window without being
# summarised, fold them into the durable summary. Deliberately generous — the log is permanent + the
# window is always fresh, so compaction is a slow background rollup, not a hot-path concern.
_COMPACT_SCROLLED_TRIGGER = 40

# The manager's durable conversation memory lives in agent_memory (VT-550) — RLS + DSR already covered —
# under a stable per-tenant key (agent='manager'). Two keys: the running summary + a watermark marking the
# newest turn already folded into it (so compaction never re-folds the same turns).
_MANAGER_AGENT = "manager"
_SUMMARY_KEY = "conversation_summary"
_WATERMARK_KEY = "conversation_summary_watermark"


def record_turn(
    tenant_id: UUID | str,
    role: str,
    text: str,
    *,
    message_sid: str | None = None,
    surface: str | None = None,
) -> None:
    """Append one owner↔system turn to the lifetime log. FAIL-SOFT — never blocks a send/receive.

    ``role`` is 'owner' (owner → us) or 'assistant' (us → owner). ``text`` is capped at ~1000 chars;
    an empty text is a no-op (nothing to remember). Idempotent per (tenant, message_sid): a redelivered
    Twilio message or a DBOS step retry collapses to ONE row via ON CONFLICT DO NOTHING against the
    partial unique index. message_sid=None records with no dedup (nothing to dedup on)."""
    try:
        if role not in ("owner", "assistant"):
            return
        clean = (text or "").strip()[:_TEXT_CAP]
        if not clean:
            return
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            if message_sid:
                conn.execute(
                    "INSERT INTO conversation_log (tenant_id, role, text, message_sid, surface) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (tenant_id, message_sid) WHERE message_sid IS NOT NULL DO NOTHING",
                    (str(tenant_id), role, clean, message_sid, surface),
                )
            else:
                conn.execute(
                    "INSERT INTO conversation_log (tenant_id, role, text, surface) "
                    "VALUES (%s, %s, %s, %s)",
                    (str(tenant_id), role, clean, surface),
                )
    except Exception:  # noqa: BLE001 — conversation memory is never a gate; a write miss must not break flow
        logger.warning("conversation_log: record_turn failed (fail-soft) tenant=%s", tenant_id, exc_info=True)


def active_window(
    tenant_id: UUID | str,
    *,
    max_turns: int = _ACTIVE_MAX_TURNS,
    max_age_h: int = _ACTIVE_MAX_AGE_H,
    exclude_message_sid: str | None = None,
) -> list[dict[str, Any]]:
    """The last ≤``max_turns`` turns within ``max_age_h`` hours, CHRONOLOGICAL (oldest-first).

    This is the always-on Team-Manager context window. ``exclude_message_sid`` drops the CURRENT inbound
    turn (which dispatch also carries as the HumanMessage) so it is not duplicated. Returns [{role, text,
    created_at, surface}]. Fail-soft: any error → [] (dispatch proceeds without the block)."""
    try:
        from orchestrator.db import tenant_connection

        params: list[Any] = [str(tenant_id), f"{int(max_age_h)} hours"]
        sid_clause = ""
        if exclude_message_sid:
            sid_clause = "AND (message_sid IS NULL OR message_sid <> %s) "
            params.append(exclude_message_sid)
        params.append(int(max_turns))
        with tenant_connection(tenant_id) as conn:
            rows = conn.execute(
                "SELECT role, text, created_at, surface FROM conversation_log "
                "WHERE tenant_id = %s AND created_at > now() - %s::interval "
                f"{sid_clause}"
                "ORDER BY created_at DESC LIMIT %s",
                tuple(params),
            ).fetchall()
        # Fetched newest-first (so LIMIT keeps the most-recent); reverse to chronological for the block.
        turns = [_row_to_turn(r) for r in rows]
        turns.reverse()
        return turns
    except Exception:  # noqa: BLE001 — a window-read miss must never break dispatch
        logger.warning("conversation_log: active_window read failed (fail-soft) tenant=%s", tenant_id, exc_info=True)
        return []


def search_history(
    tenant_id: UUID | str, query: str, *, limit: int = 10
) -> list[dict[str, Any]]:
    """Brain-commanded retrieval over the LIFETIME log — a simple case-insensitive substring match,
    NEWEST-first, tenant-scoped, k-capped. No embeddings yet (honest: this is lexical, not semantic).
    Returns [{role, text, created_at, surface}]. Fail-soft: any error → []."""
    try:
        q = (query or "").strip()
        if not q:
            return []
        k = max(1, min(int(limit), 50))
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            rows = conn.execute(
                "SELECT role, text, created_at, surface FROM conversation_log "
                "WHERE tenant_id = %s AND text ILIKE %s "
                "ORDER BY created_at DESC LIMIT %s",
                (str(tenant_id), f"%{q}%", k),
            ).fetchall()
        return [_row_to_turn(r) for r in rows]
    except Exception:  # noqa: BLE001 — a retrieval miss returns nothing, never raises into the tool loop
        logger.warning("conversation_log: search_history failed (fail-soft) tenant=%s", tenant_id, exc_info=True)
        return []


def _row_to_turn(r: Any) -> dict[str, Any]:
    if isinstance(r, dict):
        return {"role": r["role"], "text": r["text"], "created_at": r["created_at"], "surface": r.get("surface")}
    return {"role": r[0], "text": r[1], "created_at": r[2], "surface": r[3]}


def recent_owner_texts(tenant_id: UUID | str, *, limit: int = 20) -> list[str]:
    """NEWEST-first owner message texts from the UNIFIED lifetime log's active window.

    VT-583 addendum (CL-2026-07-03): this is the ONE substrate every "did the owner already tell us X"
    scan should read. The disease it cures is substrate FRAGMENTATION — ``journey.recent_turns`` is a
    journey-local window, so a turn dropped there (a silent/consumed path that never appended) was
    invisible to context scans even though the runner seam had recorded it HERE. The canonical failure
    is the owner sending their store URL 3× because ``_recent_shop_domain`` only read the journey window.
    Built on :func:`active_window` (the always-on ≤20-turns/24h manager context); fail-soft → []."""
    turns = active_window(tenant_id, max_turns=max(1, int(limit)))
    return [
        str(t["text"]) for t in reversed(turns) if t.get("role") == "owner" and t.get("text")
    ]


# --- Compaction: fold scrolled-out turns into a durable manager summary (VT-571 pattern) ---------------


def read_manager_summary(tenant_id: UUID | str) -> str | None:
    """The running DISTILLED conversation memory the window block carries ABOVE the raw turns — durable
    facts/decisions/preferences from turns that have scrolled out of the 24h window. ALWAYS-ON read (this
    is CONVERSATION, not the gated learned/VTR memory), targeted at the manager's summary row. None when
    nothing has been folded yet. Fail-soft."""
    return _read_manager_memory(tenant_id, _SUMMARY_KEY)


def _read_manager_memory(tenant_id: UUID | str, memory_key: str) -> str | None:
    try:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT content FROM agent_memory "
                "WHERE tenant_id = %s AND agent = %s AND memory_key = %s LIMIT 1",
                (str(tenant_id), _MANAGER_AGENT, memory_key),
            ).fetchone()
        if row is None:
            return None
        return (row["content"] if isinstance(row, dict) else row[0]) or None
    except Exception:  # noqa: BLE001 — a summary read miss just means "no summary yet"
        logger.warning("conversation_log: manager-memory read failed (fail-soft) tenant=%s", tenant_id, exc_info=True)
        return None


def maybe_compact(tenant_id: UUID | str) -> None:
    """CHEAP guard fired once per owner-inbound: if enough turns have scrolled out of the window since the
    last fold, start the off-hot-path compaction workflow. A COUNT + a conditional DBOS.start_workflow —
    the real work (re-read, distill, persist, advance watermark) runs in the workflow. Fully fail-soft."""
    try:
        watermark = _read_manager_memory(tenant_id, _WATERMARK_KEY)
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            if watermark:
                row = conn.execute(
                    "SELECT count(*) AS n FROM conversation_log "
                    "WHERE tenant_id = %s AND created_at > %s::timestamptz",
                    (str(tenant_id), watermark),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT count(*) AS n FROM conversation_log WHERE tenant_id = %s",
                    (str(tenant_id),),
                ).fetchone()
        if row is None:
            return
        n = int((row["n"] if isinstance(row, dict) else row[0]) or 0)
        # Trigger only once MORE than a full window (20) PLUS the scroll-out threshold has accumulated
        # unsummarised — i.e. there is a real backlog to fold, not just a fresh window.
        if n <= _ACTIVE_MAX_TURNS + _COMPACT_SCROLLED_TRIGGER:
            return
        DBOS.start_workflow(conversation_compact_workflow, str(tenant_id))
    except Exception:  # noqa: BLE001 — compaction is best-effort; a fire miss degrades to no-compaction
        logger.warning("conversation_log: maybe_compact failed (fail-soft) tenant=%s", tenant_id, exc_info=True)


def _compact_run(tenant_id: UUID | str) -> None:
    """Plain (non-DBOS) body: fold the turns that have SCROLLED OUT of the active window into the running
    summary, then advance the watermark. Kept as a plain function so it is unit-testable without a DBOS
    context (the memory_distiller ``_run_distill`` split). Fail-soft throughout — memory only."""
    try:
        watermark = _read_manager_memory(tenant_id, _WATERMARK_KEY)
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            if watermark:
                rows = conn.execute(
                    "SELECT role, text, created_at FROM conversation_log "
                    "WHERE tenant_id = %s AND created_at > %s::timestamptz ORDER BY created_at ASC",
                    (str(tenant_id), watermark),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT role, text, created_at FROM conversation_log "
                    "WHERE tenant_id = %s ORDER BY created_at ASC",
                    (str(tenant_id),),
                ).fetchall()
        turns = [_row_to_turn(r) for r in rows]
        # Everything EXCEPT the most-recent window has scrolled out and is eligible to fold.
        if len(turns) <= _ACTIVE_MAX_TURNS:
            return
        scrolled_out = turns[: len(turns) - _ACTIVE_MAX_TURNS]
        if len(scrolled_out) < _COMPACT_SCROLLED_TRIGGER:
            return  # not enough backlog yet — leave the summary + watermark untouched
        # Reuse the VT-571 distiller: fold {role, text} turns + the prior summary into the new summary.
        from orchestrator.onboarding.memory_distiller import distill_evicted_turns

        prior = read_manager_summary(tenant_id)
        evicted = [{"role": t["role"], "text": t["text"]} for t in scrolled_out]
        new_summary = distill_evicted_turns(tenant_id, evicted, prior)
        if not new_summary:
            return  # distill failed / nothing durable → keep prior summary + watermark (drop-silently)
        from orchestrator.agents.agent_memory import upsert_learned

        upsert_learned(tenant_id, memory_key=_SUMMARY_KEY, content=new_summary, agent=_MANAGER_AGENT)
        new_watermark = _iso(scrolled_out[-1]["created_at"])
        if new_watermark:
            upsert_learned(
                tenant_id, memory_key=_WATERMARK_KEY, content=new_watermark, agent=_MANAGER_AGENT
            )
    except Exception:  # noqa: BLE001 — a compaction failure never surfaces; the log itself is intact
        logger.warning("conversation_log: compaction run failed (fail-soft) tenant=%s", tenant_id, exc_info=True)


@DBOS.workflow()
def conversation_compact_workflow(tenant_id: str) -> None:
    """DBOS background entrypoint (fired fire-and-forget from ``maybe_compact`` — OFF the owner-inbound hot
    path). Thin wrapper so the body stays plain + unit-testable. Fold the scrolled-out turns into the
    durable manager summary and advance the watermark."""
    _compact_run(tenant_id)


def _iso(value: Any) -> str | None:
    """Render a created_at (datetime or str) as an ISO-8601 string for the watermark. None on anything odd."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, str) and value:
        return value
    return None


__all__ = [
    "active_window",
    "conversation_compact_workflow",
    "maybe_compact",
    "read_manager_summary",
    "record_turn",
    "search_history",
]
