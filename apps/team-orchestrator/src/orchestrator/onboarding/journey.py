"""VT-367 Gap-3 — the guided, paced onboarding journey.

Walks the owner through onboarding ONE question at a time over WhatsApp (confirm-the-draft first,
then 2b's reasoned gaps), resumable across days. State lives in ``onboarding_journey`` (migration
123). The owner-inbound INTERCEPT (``maybe_handle_journey_reply``, in runner) routes journey replies
here BEFORE the generic brain while a journey is active — deterministic-first, fail-OPEN, idempotent
on WhatsApp redelivery. A draft-confirm promotes ONLY the confirmed field via 2a ``confirm_draft``
(the never-assert boundary). On completion the named Gap-4 seam fires (business summary + 6-mo plan).
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from orchestrator.db import tenant_connection

logger = logging.getLogger(__name__)

# Deterministic affirmations / skips (EN + HI/Hinglish), token-exact (the approval_reply discipline).
_YES = {"yes", "y", "correct", "right", "ok", "okay", "haan", "ha", "sahi", "हाँ", "हां", "सही", "ठीक"}
_SKIP = {"skip", "later", "pass", "baad", "naa", "बाद", "छोड़ो", "स्किप"}


def _tokens(body: str) -> set[str]:
    norm = (body or "").strip().casefold().replace("'", "")
    return {t for t in re.split(r"[\s,.!?;:।/\\-]+", norm) if t}


def start_journey(tenant_id: UUID | str, question_queue: list[dict[str, Any]]) -> None:
    """Begin (or reset) the journey with the ordered question set (2b Question objects as dicts).
    Idempotent-ish: an existing row is replaced (re-start). ``question_queue`` may be empty if the
    draft isn't ready yet — the queue is filled in later via ``set_queue`` (the lazy-start path)."""
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            """
            INSERT INTO onboarding_journey (tenant_id, status, question_queue, cursor, answers, skipped)
            VALUES (%s, 'active', %s, 0, '{}'::jsonb, '[]'::jsonb)
            ON CONFLICT (tenant_id) DO UPDATE
              SET status = 'active', question_queue = EXCLUDED.question_queue, cursor = 0,
                  answers = '{}'::jsonb, skipped = '[]'::jsonb, updated_at = now(), completed_at = NULL
            """,
            (str(tenant_id), Jsonb(question_queue)),
        )


def set_queue_if_empty(tenant_id: UUID | str, question_queue: list[dict[str, Any]]) -> None:
    """Lazy-start fill: when the journey started in a pending state (draft not ready) and the draft
    later lands, install the composed queue — only if still empty + active (never clobber progress)."""
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            """
            UPDATE onboarding_journey
               SET question_queue = %s, updated_at = now()
             WHERE tenant_id = %s AND status = 'active'
               AND jsonb_array_length(question_queue) = 0
            """,
            (Jsonb(question_queue), str(tenant_id)),
        )


def get_journey(tenant_id: UUID | str) -> dict[str, Any] | None:
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT status, question_queue, cursor, answers, skipped, last_message_sid "
            "FROM onboarding_journey WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    g = dict(row) if isinstance(row, dict) else {
        "status": row[0], "question_queue": row[1], "cursor": row[2],
        "answers": row[3], "skipped": row[4], "last_message_sid": row[5],
    }
    g["question_queue"] = list(g["question_queue"] or [])
    g["answers"] = dict(g["answers"] or {})
    g["skipped"] = list(g["skipped"] or [])
    return g


def is_active(tenant_id: UUID | str) -> bool:
    """Cheap PK lookup for the intercept. Fail-OPEN: any error → False (fall through to normal flow)."""
    try:
        g = get_journey(tenant_id)
        return bool(g and g["status"] == "active")
    except Exception:  # noqa: BLE001 — owner-inbound hot path: never block on a journey-check error
        logger.exception("journey.is_active check failed tenant=%s — treating as inactive", tenant_id)
        return False


def _current(g: dict[str, Any]) -> dict[str, Any] | None:
    q = g["question_queue"]
    c = g["cursor"]
    return q[c] if 0 <= c < len(q) else None


def handle_reply(
    tenant_id: UUID | str, body: str, message_sid: str | None, *, lang: str = "en"
) -> dict[str, Any]:
    """Process one owner reply against the in-flight question; advance the cursor; return
    {reply_en, reply_hi, done}. IDEMPOTENT: a redelivered message_sid (== last_message_sid) re-emits
    the SAME current question without double-advancing. Confirm-Q → confirm_draft; gap-Q → store
    value; 'skip' → skip. On queue exhaustion → complete + fire the Gap-4 seam."""
    g = get_journey(tenant_id)
    if g is None or g["status"] != "active":
        return {"reply_en": "", "reply_hi": "", "done": True}

    # Idempotency: a redelivered inbound must not double-advance.
    if message_sid and message_sid == g.get("last_message_sid"):
        q = _current(g)
        if q is None:
            return {"reply_en": "", "reply_hi": "", "done": True}
        return {"reply_en": q.get("prompt_en", ""), "reply_hi": q.get("prompt_hi", ""), "done": False}

    q = _current(g)
    if q is None:
        _complete(tenant_id)
        return _completion_message()

    toks = _tokens(body)
    field = q.get("field")
    answers = g["answers"]
    skipped = g["skipped"]

    if toks & _SKIP:
        if field and field not in skipped:
            skipped.append(field)
    elif q.get("kind") == "confirm":
        # yes → confirm the discovered draft_value; anything else → a correction (the body is the value).
        value = q.get("draft_value") if (toks & _YES) else body.strip()
        if field and value not in (None, ""):
            answers[field] = value
            _confirm(tenant_id, {field: value})
    else:  # gap question — the body IS the value
        if field and body.strip():
            answers[field] = body.strip()

    new_cursor = g["cursor"] + 1
    _advance(tenant_id, new_cursor, answers, skipped, message_sid)

    g2 = get_journey(tenant_id)
    nxt = _current(g2) if g2 else None
    if nxt is None:
        _complete(tenant_id)
        return _completion_message()
    return {"reply_en": nxt.get("prompt_en", ""), "reply_hi": nxt.get("prompt_hi", ""), "done": False}


def _advance(tenant_id, cursor, answers, skipped, message_sid) -> None:
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE onboarding_journey SET cursor = %s, answers = %s, skipped = %s, "
            "last_message_sid = %s, updated_at = now() WHERE tenant_id = %s AND status = 'active'",
            (cursor, Jsonb(answers), Jsonb(skipped), message_sid, str(tenant_id)),
        )


def _confirm(tenant_id, confirmed_fields: dict[str, Any]) -> None:
    """Promote a confirmed field to canonical via 2a confirm_draft. Best-effort — a promotion failure
    must not stall the journey (the answer is recorded in onboarding_journey regardless)."""
    try:
        from orchestrator.onboarding.draft_profile import confirm_draft

        confirm_draft(tenant_id, confirmed_fields)
    except Exception:  # noqa: BLE001
        logger.exception("journey: confirm_draft failed tenant=%s fields=%s", tenant_id, list(confirmed_fields))


def _complete(tenant_id) -> None:
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE onboarding_journey SET status = 'complete', completed_at = now(), updated_at = now() "
            "WHERE tenant_id = %s AND status = 'active'",
            (str(tenant_id),),
        )
    _emit_gap4_seam(tenant_id)


def _emit_gap4_seam(tenant_id) -> None:
    """Named seam for Gap 4 (post-ingestion business summary + 6-month plan). Emits an observability
    event NOW; Gap 4 wires its generator to this trigger. Best-effort."""
    try:
        from orchestrator.observability.log import log_event

        log_event(
            event_type="onboarding_journey_completed",
            run_id=uuid4(),
            tenant_id=tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id)),
            severity="info",
            component="onboarding",
            payload={"tenant_id": str(tenant_id), "gap4_trigger": True},
        )
    except Exception:  # noqa: BLE001
        logger.exception("journey: gap4 seam emit failed tenant=%s", tenant_id)


def _completion_message() -> dict[str, Any]:
    return {
        "reply_en": "Thanks — that's everything we need to get started. We're setting up your assistant now.",
        "reply_hi": "धन्यवाद — शुरू करने के लिए हमें इतना ही चाहिए था। हम आपका असिस्टेंट अभी तैयार कर रहे हैं।",
        "done": True,
    }


# Re-export json for callers that pass raw queue strings (kept explicit for clarity).
__all__ = [
    "start_journey", "set_queue_if_empty", "get_journey", "is_active", "handle_reply",
]
