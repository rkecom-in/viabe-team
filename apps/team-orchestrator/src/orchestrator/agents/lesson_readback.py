"""VT-566 — the flywheel's read-back leg: recent owner OUTCOME signals + the manager-context render.

The correction store (``correction_store.get_recent_lessons``) supplies the AUTHORITATIVE lessons —
the owner's own edit/reject/approve verdicts. This module adds the WEAK-signal reader
(``owner_feedback``) and the single render that turns both into the Team-Manager's per-turn
``## Lessons from this owner`` (+ optional ``## Outcome signals (weak)``) system block.

TIER BRANCHING IS LOAD-BEARING (CL-2026-07-02-implicit-feedback-weak-signal): an ``owner_feedback``
row's ``tier`` decides how it renders.
  - ``emoji`` / ``dashboard`` = EXPLICIT owner feedback → renders as owner feedback in the lessons
    block (authoritative, alongside the corrections).
  - ``implicit`` = the VT-563 attribution-sweep's outcome-derived thumbs → renders ONLY as a clearly
    DOWN-WEIGHTED line in a separate ``## Outcome signals (weak)`` block, prefixed
    ``[weak signal — outcome-derived, not owner-stated]``, and is EXCLUDED from anything that could
    read as a correction. It must never carry correction-grade weight. No consumer of
    ``owner_feedback`` existed before VT-566 — this render sets the precedent every future consumer
    branches on.

Framing (CL-2026-07-01-no-fixed-playbook): the block instructs the manager to REASON WITH these
verdicts, not to obey them — lessons inform judgement, they never script it.

PII: ``correction_text`` is redacted at capture; ``owner_feedback.source_metadata`` is PII-free by
construction (CL-390) and this reader surfaces only ``tier`` + ``signal`` (never metadata). Callers
log presence booleans only, never block contents.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# owner_feedback tiers that are EXPLICIT (owner-stated). 'implicit' is outcome-derived and weak.
_EXPLICIT_TIERS = frozenset({"emoji", "dashboard"})

# The read-back cap — a handful of recent outcome signals, not a corpus.
_RECENT_OUTCOMES_LIMIT = 5


def get_recent_outcome_signals(
    tenant_id: UUID | str, *, limit: int = _RECENT_OUTCOMES_LIMIT, conn: Any = None
) -> list[dict[str, Any]]:
    """Recent ``owner_feedback`` rows for the tenant, most-recent-first, capped (~5). RLS-scoped
    (``tenant_connection`` / a passed tenant ``conn``): ``app_current_tenant()`` bounds it to this
    tenant. Returns dicts carrying ``tier`` + ``signal`` + ``created_at`` so ``render_lessons_block``
    can branch on ``tier`` per CL-2026-07-02-implicit-feedback-weak-signal. Surfaces NO PII — only
    the tier + signal (``source_metadata`` is never read). Fail-CLOSED: [] on any read error."""
    sql = (
        "SELECT tier, signal, created_at FROM owner_feedback "
        "WHERE tenant_id = app_current_tenant() "
        "ORDER BY created_at DESC LIMIT %s"
    )
    params = (int(limit),)
    try:
        rows = (
            conn.execute(sql, params).fetchall()
            if conn is not None
            else _read_tenant(tenant_id, sql, params)
        )
    except Exception:  # noqa: BLE001 — read-back is best-effort; never break dispatch
        logger.warning("VT-566 outcome-signal read-back failed (fail-soft)", exc_info=True)
        return []
    return [
        {
            "tier": _v(r, "tier", 0),
            "signal": _v(r, "signal", 1),
            "created_at": _v(r, "created_at", 2),
        }
        for r in rows
    ]


def render_lessons_block(
    lessons: list[dict[str, Any]], outcomes: list[dict[str, Any]]
) -> str | None:
    """Render the captured lessons + outcome signals as the manager's per-turn system block, or
    ``None`` when there is nothing to say. Corrections + EXPLICIT owner feedback (emoji/dashboard) go
    in ``## Lessons from this owner`` (authoritative); IMPLICIT outcome rows go in a separate,
    clearly-weak ``## Outcome signals (weak)`` block — the tier branch is the C3 contamination
    control (CL-2026-07-02-implicit-feedback-weak-signal)."""
    explicit = [o for o in outcomes if o.get("tier") in _EXPLICIT_TIERS]
    implicit = [o for o in outcomes if o.get("tier") == "implicit"]
    if not lessons and not explicit and not implicit:
        return None

    parts: list[str] = []
    if lessons or explicit:
        lines = [
            "## Lessons from this owner",
            "This owner has previously corrected or approved your team's proposals. Treat them as "
            "evidence about how THIS owner wants things done — reason with them and weigh them "
            "against the current situation; they inform your judgement, they do not script it:",
        ]
        lines.extend(_render_lesson_line(lesson) for lesson in lessons)
        # Explicit owner feedback (emoji/dashboard) is owner-STATED — authoritative, alongside the
        # corrections. (implicit is handled below, deliberately NOT here.)
        lines.extend(f"- [owner feedback] {o.get('signal')}" for o in explicit)
        parts.append("\n".join(lines))

    if implicit:
        # CL-2026-07-02-implicit-feedback-weak-signal: implicit rows are a DOWN-WEIGHTED,
        # distinctly-tagged prior — NEVER correction-grade. Separate block, explicitly labelled
        # outcome-derived (not owner-stated), excluded from the lessons list above.
        lines = [
            "## Outcome signals (weak)",
            "Down-weighted signals INFERRED from campaign outcomes — NOT the owner's stated "
            "feedback. Treat as faint background context only; never as a correction:",
        ]
        lines.extend(
            f"- [weak signal — outcome-derived, not owner-stated] {o.get('signal')}"
            for o in implicit
        )
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _render_lesson_line(lesson: dict[str, Any]) -> str:
    """One authoritative lesson line. ``approve`` = a positive example (the proposed template, no
    prose); ``edit``/``reject`` carry the owner's redacted correction text."""
    kind = lesson.get("kind")
    hint = lesson.get("template_hint")
    on = f" (on {hint})" if hint else ""
    if kind == "approve":
        return f"- [approved as-is]{on}"
    label = {"edit": "corrected", "reject": "rejected"}.get(kind, str(kind or "correction"))
    verb = lesson.get("verb") or kind
    text = (lesson.get("correction_text") or "").strip()
    return f"- [{label} · {verb}] {text}{on}" if text else f"- [{label} · {verb}]{on}"


def _v(row: Any, key: str, idx: int) -> Any:
    return row[key] if isinstance(row, dict) else row[idx]


def _read_tenant(tenant_id: UUID | str, sql: str, params: tuple) -> list:
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as c:
        return c.execute(sql, params).fetchall()


__all__ = ["get_recent_outcome_signals", "render_lessons_block"]
