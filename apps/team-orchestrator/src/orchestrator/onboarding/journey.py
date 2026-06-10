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
    _kickoff_business_plan(tenant_id)


def _kickoff_business_plan(tenant_id) -> None:
    """VT-368: kick the business-plan generator (the Gap-4 spine) — non-blocking DBOS bg workflow,
    best-effort: a generator/kick failure must never block journey completion. Skipped cleanly if
    DBOS isn't launched (tests / non-workflow contexts). The observability seam emit above stays —
    this is the execution subscriber."""
    try:
        from dbos import DBOS

        from orchestrator.business_plan.generator import generate_business_plan_workflow

        DBOS.start_workflow(generate_business_plan_workflow, str(tenant_id))
    except Exception:  # noqa: BLE001 — best-effort; journey completion already committed
        logger.exception("journey: gap4 business-plan kickoff failed tenant=%s", tenant_id)


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


# --- Owner-inbound INTERCEPT (the hot-path gate; mirrors runner.try_resume_pending_approval) -------


def _tenant_phase_and_type(tenant_id: UUID | str) -> tuple[str | None, str | None]:
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT phase, business_type FROM tenants WHERE id = %s", (str(tenant_id),)
        ).fetchone()
    if row is None:
        return None, None
    return (row["phase"], row["business_type"]) if isinstance(row, dict) else (row[0], row[1])


def _compose_queue(tenant_id: UUID | str, business_type: str | None) -> list[dict[str, Any]]:
    """Compose the ordered question set from the 2a draft via 2b. [] if the draft isn't ready yet."""
    from orchestrator.onboarding.draft_profile import get_draft
    from orchestrator.onboarding.question_brain import compose_onboarding_questions

    draft = get_draft(tenant_id)
    if not draft.get("attributes"):
        return []
    questions = compose_onboarding_questions(business_type or "other", draft, answered=[])
    return [
        {"field": q.field, "kind": q.kind, "prompt_en": q.prompt_en, "prompt_hi": q.prompt_hi,
         "draft_value": q.draft_value}
        for q in questions
    ]


def _opener() -> dict[str, Any]:
    return {
        "prompt_en": "Hi! Give us a moment — we're setting up your assistant and will ask a couple of quick questions.",
        "prompt_hi": "नमस्ते! एक पल दीजिए — हम आपका असिस्टेंट तैयार कर रहे हैं और कुछ छोटे सवाल पूछेंगे।",
    }


def _send(recipient: str | None, q: dict[str, Any], lang: str) -> None:
    """Best-effort owner send of one question (WABA-gated/stubbed — never crash the pipeline)."""
    if not recipient:
        return
    text = q.get("prompt_hi") if lang == "hi" else q.get("prompt_en")
    if not text:
        return
    try:
        from orchestrator.utils.twilio_send import send_freeform_message

        send_freeform_message(text, recipient)
    except Exception:  # noqa: BLE001 — send is WABA-gated; the journey state advances regardless
        logger.warning("journey: owner send failed (recipient hashed in send util) — state advanced")


def maybe_handle_journey_reply(
    tenant_id: UUID | str, body: str, message_sid: str | None, recipient: str | None, *, lang: str = "en"
) -> dict[str, Any] | None:
    """THE owner-inbound gate. Returns a result dict if the journey handled this inbound (caller
    short-circuits the brain), else None (fall through to the normal pipeline). **FAIL-OPEN**: any
    error → None (never block owner-inbound). Lazy-starts on a fresh tenant's first inbound so the
    owner's first message NEVER reaches the cold brain."""
    try:
        # VT-329 / DPDP (compliance-critical): opt-out / DSR / STOP ALWAYS wins over any other
        # interpretation. The journey gate runs BEFORE pre_filter, so it MUST NOT consume an opt-out /
        # DSR message as a journey answer — short-circuit to None so the inbound falls through to
        # pre_filter, which routes it to the authoritative opt-out/DSR handler. Phase-aware reply gates
        # call this matcher for exactly this reason; the journey gate is one of them.
        from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

        if matches_opt_out_or_dsr(body or ""):
            return None
        g = get_journey(tenant_id)
        if g is None:
            # No journey row → NOT in onboarding (the journey is created at the signup seam, pending).
            # Established/pre-feature tenants have no row → fall through to the normal pipeline. This
            # is what keeps owner-inbound for non-onboarding tenants untouched (no over-firing).
            return None
        if g["status"] != "active":
            return None  # complete / abandoned → normal flow
        if not g["question_queue"]:
            # Pending lazy-start: the draft may have landed — try to fill the queue now.
            _, btype = _tenant_phase_and_type(tenant_id)
            queue = _compose_queue(tenant_id, btype)
            if queue:
                set_queue_if_empty(tenant_id, queue)
                g = get_journey(tenant_id) or g
                _send(recipient, _current(g) or _opener(), lang)
            else:
                _send(recipient, _opener(), lang)  # still setting up
            return {"done": False, "pending": True}
        r = handle_reply(tenant_id, body, message_sid, lang=lang)
        _send(recipient, {"prompt_en": r["reply_en"], "prompt_hi": r["reply_hi"]}, lang)
        return r
    except Exception:  # noqa: BLE001 — owner-inbound HOT PATH: any failure falls through, never blocks
        logger.exception("maybe_handle_journey_reply failed tenant=%s — fall through", tenant_id)
        return None


__all__ = [
    "start_journey", "set_queue_if_empty", "get_journey", "is_active", "handle_reply",
    "maybe_handle_journey_reply",
]
