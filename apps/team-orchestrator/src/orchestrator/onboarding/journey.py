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
import os
import re
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from orchestrator.db import tenant_connection

logger = logging.getLogger(__name__)


def _turn_brain_enabled() -> bool:
    """VT-569 config gate — the in-session conversation is LLM-driven ONLY when this is explicitly on
    (default OFF; dev flips it). Read FRESH per call (parity with dispatch's ``MANAGER_MEMORY_RETRIEVAL``
    idiom) so an env flip takes effect without a restart. When off, the deterministic walker below runs
    byte-identical to pre-VT-569 (except the mandated VT-569a bare-negative re-prompt fix)."""
    return os.environ.get("ONBOARDING_TURN_BRAIN", "").strip().lower() in {"1", "true", "yes"}

# Deterministic affirmations / skips (EN + HI/Hinglish), token-exact (the approval_reply discipline).
_YES = {"yes", "y", "correct", "right", "ok", "okay", "haan", "ha", "sahi", "हाँ", "हां", "सही", "ठीक"}
_SKIP = {"skip", "later", "pass", "baad", "naa", "बाद", "छोड़ो", "स्किप"}
# Bare negatives to a CONFIRM ("no") — NOT a value (a city isn't named "no"). A bare-negative confirm
# is treated as "not yet answered": re-present the question so the owner supplies the correct value,
# rather than recording "no" verbatim as the field value. Token-exact, same discipline as _YES/_SKIP.
_NO = {"no", "nope", "nah", "nahi", "nahin", "galat", "नहीं", "नही", "गलत"}
# Bare greetings (EN + HI/Hinglish) — a greeting is NEVER an answer to the pending question (the live
# "Hi → category" bug). Token-exact; a body that is ONLY greeting tokens is re-presented, not recorded.
_GREETING = {
    "hi", "hello", "hey", "helo", "hii", "hiii", "hey", "yo", "hola",
    "namaste", "namaskar", "namaskaar", "namastey", "नमस्ते", "नमस्कार",
    "salaam", "salam", "assalamualaikum", "adaab",
    "morning", "evening", "afternoon",  # "good morning"/"good evening" — "good" alone isn't a greeting
}


def _tokens(body: str) -> set[str]:
    norm = (body or "").strip().casefold().replace("'", "")
    return {t for t in re.split(r"[\s,.!?;:।/\\-]+", norm) if t}


def _is_bare_greeting(body: str) -> bool:
    """True iff EVERY token in ``body`` is a greeting token (a bare greeting like "hi" / "namaste" /
    "good morning") — i.e. the owner greeted but gave no answer. A greeting MIXED with substantive
    content ("hi my hours are 9-9") is NOT bare → it still carries an answer and is recorded. Empty
    body → not a greeting (handled by the existing empty-body guards)."""
    toks = _tokens(body)
    return bool(toks) and toks <= _GREETING


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


def _current_q_reply(q: dict[str, Any], *, done: bool = False) -> dict[str, Any]:
    """A reply that re-emits the in-flight question verbatim (no greet-back)."""
    return {"reply_en": q.get("prompt_en", ""), "reply_hi": q.get("prompt_hi", ""), "done": done}


def _greet_then_question(q: dict[str, Any]) -> dict[str, Any]:
    """A conversational re-present: a brief manager greet-back PREPENDED to the pending question. Used
    when the owner sends a bare greeting / non-answer mid-question — we acknowledge the greeting and
    re-ask, WITHOUT recording it as the answer or advancing the cursor (the VT live "Hi → category"
    bug). ``re_present=True`` tells the intercept this is a fresh, sendable re-presentation."""
    en = f"Hi! {q.get('prompt_en', '')}".strip()
    hi = f"नमस्ते! {q.get('prompt_hi', '')}".strip()
    return {"reply_en": en, "reply_hi": hi, "done": False, "re_present": True}


def _reprompt_after_no(q: dict[str, Any]) -> dict[str, Any]:
    """VT-569a (the deterministic dead-end fix) — a bare negative ("no") to a CONFIRM must NOT re-send
    the IDENTICAL question. The live defect: replying "No" to "We found you're a Local services
    business — is that right?" re-presented that exact string forever. Instead we acknowledge the
    rejection and ask for the CORRECT value, referencing what they rejected — a DIFFERENT string from
    the confirm prompt. State is untouched (cursor/answers unchanged; the field stays a candidate);
    ``re_present=True`` makes the intercept send this. Holds even with the turn-brain OFF / LLM down."""
    dv = q.get("draft_value")
    field = q.get("field")
    not_txt_en = f" (not {dv})" if dv not in (None, "") else ""
    not_txt_hi = f" ({dv} नहीं)" if dv not in (None, "") else ""
    if field in ("business_type", "category"):
        en = f"No problem — so what kind of business is it?{not_txt_en}"
        hi = f"कोई बात नहीं — तो यह किस तरह का व्यापार है?{not_txt_hi}"
    elif field == "city":
        en = f"Got it — which city are you actually based in?{not_txt_en}"
        hi = f"ठीक है — आप असल में किस शहर में हैं?{not_txt_hi}"
    else:
        label = field or "value"
        en = f"Got it — what's the correct {label} then?"
        hi = f"ठीक है — तो सही {label} क्या है?"
    return {"reply_en": en, "reply_hi": hi, "done": False, "re_present": True}


def handle_reply(
    tenant_id: UUID | str, body: str, message_sid: str | None, *, lang: str = "en"
) -> dict[str, Any]:
    """Process one owner reply against the in-flight question; advance the cursor; return
    {reply_en, reply_hi, done}. IDEMPOTENT: a redelivered message_sid (== last_message_sid) re-emits
    the SAME current question without double-advancing AND signals ``already_presented`` so the
    intercept does NOT re-send it (the VT live duplicate-question bug). A bare greeting / non-answer
    to the in-flight question is NOT recorded + does NOT advance — the question is re-presented
    conversationally (``re_present``). Confirm-Q → confirm_draft; gap-Q → store value; 'skip' → skip.
    On queue exhaustion → complete + fire the Gap-4 seam."""
    g = get_journey(tenant_id)
    if g is None or g["status"] != "active":
        return {"reply_en": "", "reply_hi": "", "done": True}

    # Idempotency: a redelivered inbound must not double-advance — AND must not re-SEND the in-flight
    # question (it was already presented on the first delivery). ``already_presented`` tells the
    # intercept to skip the send (the live duplicate "based in Mumbai?" bug: a redelivered inbound
    # re-emitted the same pending question and the intercept dutifully sent it a second time).
    if message_sid and message_sid == g.get("last_message_sid"):
        q = _current(g)
        if q is None:
            return {"reply_en": "", "reply_hi": "", "done": True, "already_presented": True}
        return {**_current_q_reply(q), "already_presented": True}

    q = _current(g)
    if q is None:
        _complete(tenant_id)
        return _completion_message()

    toks = _tokens(body)
    field = q.get("field")
    answers = g["answers"]
    skipped = g["skipped"]

    # A bare greeting / non-answer must NOT be recorded as the answer and must NOT advance the cursor
    # (the live "Hi → category" bug). For a CONFIRM question a bare negative ("no") is likewise NOT a
    # value (a city isn't named "no") — re-present so the owner supplies the correct value. yes / skip
    # / a real correction stay valid answers; only a greeting (any kind) or a bare-no (confirm) is
    # rejected. Re-present the pending question conversationally WITHOUT touching state.
    is_skip = bool(toks & _SKIP)
    is_bare_no_confirm = q.get("kind") == "confirm" and bool(toks) and toks <= _NO
    if not is_skip and _is_bare_greeting(body):
        # A bare greeting → acknowledge + re-present the SAME question (the owner just said hi).
        return _greet_then_question(q)
    if not is_skip and is_bare_no_confirm:
        # VT-569a — a bare "no" to a confirm → ask for the correct value, NOT the identical prompt
        # (the live dead-end). Deterministic; holds even with the turn-brain off / LLM unavailable.
        return _reprompt_after_no(q)

    if is_skip:
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

    # CONTRACT (unchanged, pre-VT-462): the owner's reply applied to the PRESENTED question
    # (``_current`` at the cursor) above, and the cursor advanced. The NEXT presented question is the
    # new cursor head, and ``done`` is the DETERMINISTIC queue-exhaustion check (every seeded question
    # answered/skipped). VT-462's conductor does NOT alter this per-reply apply/advance/done path — it
    # influences only WHICH questions are COMPOSED into the queue (the queue-composition seam in
    # ``maybe_handle_journey_reply``); the cursor then walks that composed queue deterministically.
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
    """Compose the ordered question set from the 2a draft. [] if the draft isn't ready yet.

    VT-462 — the QUEUE is conductor-COMPOSED: the conductor's ``decide_next_question`` orders the
    registry-bounded candidate set (confirm-first, then gaps) against current journey state (already-
    answered fields excluded so a volunteered/out-of-order answer is never queued; skipped fields
    deferred). The cursor then walks this composed queue deterministically (the apply/advance/done
    contract is unchanged) — the conductor decides WHICH questions/order; the cursor owns the walk.
    """
    from orchestrator.onboarding.conductor import decide_next_question
    from orchestrator.onboarding.draft_profile import get_draft

    draft = get_draft(tenant_id)
    if not draft.get("attributes"):
        return []
    # Re-derive against current state so the composed queue reflects anything the owner already
    # answered/skipped (resumability + volunteered/out-of-order handling). On a fresh start these are
    # empty, so the queue is the full registry-bounded confirm-first-then-gap set.
    g = get_journey(tenant_id) or {}
    answered = list((g.get("answers") or {}).keys())
    skipped = list(g.get("skipped") or [])
    decision = decide_next_question(
        business_type=business_type,
        draft=draft,
        answered=answered,
        skipped=skipped,
    )
    return [
        {"field": q.field, "kind": q.kind, "prompt_en": q.prompt_en, "prompt_hi": q.prompt_hi,
         "draft_value": q.draft_value}
        for q in decision.remaining
    ]


def _draft_with_reconciled_type(draft: dict[str, Any]) -> dict[str, Any]:
    """VT-478/VT-475 — return a COPY of ``draft`` whose ``attributes`` carry the reconciled
    ``business_type`` the VT-475 reconcile "would now produce" from the draft's own public signals
    (raw GBP ``category`` + website + business_name + GST nature). A pre-VT-475 draft has the raw
    category but no reconciled type (the reconcile ran only at GBP-discovery, which never re-ran for an
    existing tenant) — so re-deriving it here is what lets the confirm recompose surface the corrected
    business-type confirm and suppress the raw category. If a reconciled ``business_type`` is already
    present we leave it. Fail-soft: any error → the draft unchanged (the confirm step still works)."""
    try:
        attrs = dict(draft.get("attributes") or {})
        if attrs.get("business_type"):
            return draft  # already reconciled — nothing to add
        if not attrs.get("category"):
            return draft  # no raw category to reconcile from
        from orchestrator.onboarding.business_type_reconcile import reconcile_business_type

        reconciled = reconcile_business_type(
            business_name=attrs.get("business_name"),
            gbp_category=attrs.get("category"),
            website=attrs.get("website"),
            gst_nature=attrs.get("gst_nature") or attrs.get("nature_of_business"),
        ).business_type
        if reconciled:
            attrs["business_type"] = reconciled
            return {**draft, "attributes": attrs}
        return draft
    except Exception:  # noqa: BLE001 — reconcile is best-effort; never break the recompose
        logger.exception("journey: draft business-type reconcile (recompose) failed")
        return draft


def _live_confirm_questions(tenant_id: UUID | str, business_type: str | None) -> list[dict[str, Any]]:
    """VT-478 — the CONFIRM questions the question-brain would compose RIGHT NOW from the live draft,
    as queue dicts. This is the corrected confirm set used to detect + heal a STALE queue: when a
    queue was composed BEFORE VT-475's business-type reconcile landed, its head confirm still carries
    the raw GBP ``category`` (e.g. ``draft_value='Telecommunications service provider'``); re-deriving
    here yields the reconciled ``business_type`` confirm instead (and SUPPRESSES the raw category).

    Deterministic + cheap on purpose: the GAP source is stubbed to ``[]`` (``llm_fn=lambda …: []``) so
    this re-derivation NEVER hits the gap LLM — it recomposes only the confirm-the-draft questions
    (whose value comes from the draft + the deterministic reconcile, no network). Already-answered
    fields are excluded at source (passed as ``answered``) so a confirmed field is never re-queued.
    Returns [] when the draft isn't ready (nothing to confirm) or on any error (fail-soft).
    """
    try:
        from orchestrator.onboarding.draft_profile import get_draft
        from orchestrator.onboarding.question_brain import compose_onboarding_questions

        draft = get_draft(tenant_id)
        if not draft.get("attributes"):
            return []
        # VT-475 reconcile applied AT RECOMPOSE: the value VT-475 "would now produce". A pre-VT-475
        # draft carries the raw GBP ``category`` but NO reconciled ``business_type`` (the reconcile
        # then ran only at GBP-discovery, which never re-ran for an existing tenant). Re-derive the
        # reconciled type from the draft's own signals here so the live confirm set reflects the fix
        # even when the draft itself was never re-discovered. Best-effort; missing → leave the draft.
        draft = _draft_with_reconciled_type(draft)
        g = get_journey(tenant_id) or {}
        answered = list((g.get("answers") or {}).keys())
        questions = compose_onboarding_questions(
            business_type or "other", draft, answered=answered, llm_fn=lambda *a, **k: []
        )
        return [
            {"field": q.field, "kind": "confirm", "prompt_en": q.prompt_en,
             "prompt_hi": q.prompt_hi, "draft_value": q.draft_value}
            for q in questions
            if q.kind == "confirm"
        ]
    except Exception:  # noqa: BLE001 — recompose is best-effort; a derivation failure must not block
        logger.exception("journey: live-confirm derivation failed tenant=%s", tenant_id)
        return []


def _confirm_is_stale(queued: dict[str, Any], live_confirms: list[dict[str, Any]]) -> bool:
    """A queued CONFIRM question is STALE iff the live reconcile would now produce a DIFFERENT confirm
    for the same conceptual field:

      - SAME field present in the live set with a DIFFERENT ``draft_value`` → stale (a plain value
        correction, e.g. the reconciled business_type label changed);
      - the queued raw GBP ``category`` confirm while the live set now confirms ``business_type``
        instead → stale (the VT-475 SUPPRESSION: the raw mis-categorized GBP field is replaced by the
        reconciled type — the exact 63211ce5 "Telecommunications service provider" case).

    CONSERVATIVE elsewhere: a non-confirm (gap) is never stale; a confirm whose field is simply absent
    from the live set for any OTHER reason is NOT treated as stale (we don't churn a queue we can't
    positively prove is wrong). The empty-live-set guard lives in the caller."""
    if queued.get("kind") != "confirm":
        return False
    field = queued.get("field")
    live_fields = {lc.get("field") for lc in live_confirms}
    for lc in live_confirms:
        if lc.get("field") == field:
            return lc.get("draft_value") != queued.get("draft_value")
    # The queued raw ``category`` confirm is superseded by the reconciled ``business_type`` confirm.
    if field == "category" and "business_type" in live_fields:
        return True
    return False


def _recompose_stale_confirms(tenant_id: UUID | str, g: dict[str, Any], business_type: str | None) -> bool:
    """VT-478 — heal a STALE onboarding queue IN-PLACE, preserving the owner's real progress.

    The forward-composition fix (VT-475) corrected how NEW queues are built but never touched EXISTING
    active queues, so a mid-journey tenant keeps being asked the pre-fix question (the raw GBP
    ``category`` confirm). This re-derives the live confirm set and, IFF the un-answered confirm tail
    is stale, rebuilds ONLY that tail's confirm questions with the reconciled value.

    PRESERVES PROGRESS (the cursor contract is sacrosanct):
      - the ANSWERED PREFIX ``queue[:cursor]`` is left byte-identical — an already-answered question is
        never re-asked, the cursor never moves, ``answers``/``skipped``/``last_message_sid`` are NOT
        touched (this writes ONLY ``question_queue``).
      - the queued GAP questions in the tail are carried forward VERBATIM (never re-run the gap LLM) —
        only the stale CONFIRM content is swapped for the reconciled confirm set.

    Returns True iff it rewrote the queue (a stale tail was found + healed), False otherwise. Cheap +
    fail-OPEN: any error → False (the existing queue stands, the owner inbound is never blocked).
    """
    try:
        queue = g["question_queue"]
        cursor = g["cursor"]
        if not (0 <= cursor < len(queue)):
            return False
        tail = queue[cursor:]
        live_confirms = _live_confirm_questions(tenant_id, business_type)
        # CONSERVATIVE: an EMPTY live confirm set is "can't re-derive" (no draft yet / derivation
        # failed), NOT evidence of staleness — never recompose against nothing (it would wrongly drop
        # the in-flight confirm + empty the queue). Only heal when we have a positive set to compare.
        if not live_confirms:
            return False
        # Detect: is any UN-answered confirm in the tail stale vs the live reconcile?
        if not any(_confirm_is_stale(q, live_confirms) for q in tail):
            return False

        answers = g.get("answers") or {}
        skipped = set(g.get("skipped") or [])
        answered = set(answers.keys())
        # Rebuild the tail: corrected confirms (excluding already-answered/skipped fields) FIRST, then
        # the existing queued GAP questions verbatim (minus any whose field is now answered). Stale
        # queued confirms are dropped — the live confirm set replaces them (handles the suppressed-
        # category → reconciled-business_type swap as well as a plain draft_value correction).
        new_confirms = [
            lc for lc in live_confirms
            if lc.get("field") not in answered and lc.get("field") not in skipped
        ]
        new_confirm_fields = {lc.get("field") for lc in new_confirms}
        carried_gaps = [
            q for q in tail
            if q.get("kind") != "confirm" and q.get("field") not in answered
            # a stale confirm whose field IS still confirmed lives in new_confirms; never double it
            and q.get("field") not in new_confirm_fields
        ]
        new_tail = new_confirms + carried_gaps
        new_queue = queue[:cursor] + new_tail

        if new_queue == queue:
            return False
        with tenant_connection(tenant_id) as conn:
            conn.execute(
                "UPDATE onboarding_journey SET question_queue = %s, updated_at = now() "
                "WHERE tenant_id = %s AND status = 'active'",
                (Jsonb(new_queue), str(tenant_id)),
            )
        logger.info("journey: recomposed stale confirm queue tenant=%s cursor=%s", tenant_id, cursor)
        return True
    except Exception:  # noqa: BLE001 — recompose is best-effort; never block the owner inbound
        logger.exception("journey: stale-confirm recompose failed tenant=%s — using existing queue", tenant_id)
        return False


def _opener() -> dict[str, Any]:
    return {
        "prompt_en": "Hi! Give us a moment — we're setting up your assistant and will ask a couple of quick questions.",
        "prompt_hi": "नमस्ते! एक पल दीजिए — हम आपका असिस्टेंट तैयार कर रहे हैं और कुछ छोटे सवाल पूछेंगे।",
    }


# VT-479: the interactive quick-reply Content object (Yes/No/Skip buttons) for confirm questions.
# Registered in twilio_templates.yaml (NO hardcoded SID — resolved at send time). In-session use needs
# NO Meta approval. The journey ONLY sends in RESPONSE to an owner inbound, so the 24h window is open by
# construction → buttons are always deliverable here (no separate window check needed).
_CONFIRM_BUTTONS_TEMPLATE = "onboarding_confirm_yesno"


def _send(recipient: str | None, q: dict[str, Any], lang: str) -> None:
    """Best-effort owner send of one question (WABA-gated/stubbed — never crash the pipeline).

    VT-479: a CONFIRM question is sent as tappable Yes/No/Skip quick-reply BUTTONS (in-session
    interactive Content object) — the button title ("Yes"/"No"/"Skip") flows back as the inbound Body
    and matches the EXISTING _YES/_NO/_SKIP token sets in handle_reply, so no answer-parse change is
    needed; buttons just remove the brittle free-text "yes" reliance. Any failure (no SID resolved /
    WABA / transport) falls back to the plain freeform text — the journey never breaks on presentation.
    Non-confirm questions stay plain freeform text.
    """
    if not recipient:
        return
    text = q.get("prompt_hi") if lang == "hi" else q.get("prompt_en")
    if not text:
        return
    # CONFIRM → try interactive Yes/No/Skip buttons first; fall back to plain text on any failure.
    if q.get("kind") == "confirm":
        try:
            from orchestrator.templates_registry import content_sid_for
            from orchestrator.utils.twilio_send import send_interactive_message

            content_sid = content_sid_for(_CONFIRM_BUTTONS_TEMPLATE, "en")
            if content_sid:
                # {{1}} = the question text (the reconciled confirm prompt); buttons are fixed Yes/No/Skip.
                send_interactive_message(content_sid, recipient, content_variables={"1": text})
                return
        except Exception:  # noqa: BLE001 — buttons are an enhancement; fall through to plain text
            logger.warning(
                "journey: interactive confirm-button send failed — falling back to freeform text"
            )
    try:
        from orchestrator.utils.twilio_send import send_freeform_message

        send_freeform_message(text, recipient)
    except Exception:  # noqa: BLE001 — send is WABA-gated; the journey state advances regardless
        logger.warning("journey: owner send failed (recipient hashed in send util) — state advanced")


# --- VT-569: the LLM turn-brain path (behind ONBOARDING_TURN_BRAIN) ---------------------------------


def _is_confirm_button_set(buttons: list[str]) -> bool:
    """True iff EVERY requested button is a Yes/No/Skip token — the ONLY interactive button set with a
    registered Twilio Content object (``onboarding_confirm_yesno``). WhatsApp quick-reply buttons are
    deliverable ONLY via a pre-registered Content object, so dynamically-titled buttons (discovered
    alternatives) cannot be sent as tappable buttons with today's infra — they degrade to inline text
    in ``_send_turn``. Reuses the existing token sets so the button titles round-trip through
    ``handle_reply``'s _YES/_NO/_SKIP matching unchanged."""
    if not buttons:
        return False
    allowed = _YES | _NO | _SKIP
    return all(bool(_tokens(b)) and _tokens(b) <= allowed for b in buttons)


def _send_turn(recipient: str | None, text: str, buttons: list[str], lang: str) -> None:
    """Send a turn-brain reply: ``text`` free-form, with quick-reply buttons when they help. A Yes/No/
    Skip button set reuses the registered interactive Content object (parity with the confirm-question
    send). Any OTHER button set has no registered Content object (WhatsApp needs one per button set),
    so its options are appended inline as text — the owner can still reply with the option. Best-effort:
    any transport failure degrades to plain free-form; the journey state has already advanced."""
    if not recipient or not text:
        return
    if buttons and _is_confirm_button_set(buttons):
        try:
            from orchestrator.templates_registry import content_sid_for
            from orchestrator.utils.twilio_send import send_interactive_message

            content_sid = content_sid_for(_CONFIRM_BUTTONS_TEMPLATE, "en")
            if content_sid:
                send_interactive_message(content_sid, recipient, content_variables={"1": text})
                return
        except Exception:  # noqa: BLE001 — buttons are an enhancement; fall through to plain text
            logger.warning("journey: turn-brain interactive confirm send failed — freeform fallback")
    body = text
    if buttons and not _is_confirm_button_set(buttons):
        body = f"{text}\n\n({' / '.join(buttons[:3])})"
    try:
        from orchestrator.utils.twilio_send import send_freeform_message

        send_freeform_message(body, recipient)
    except Exception:  # noqa: BLE001 — send is WABA-gated; the journey state advances regardless
        logger.warning("journey: turn-brain owner send failed (recipient hashed) — state advanced")


def _coerce_answer(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _apply_turn_plan(
    tenant_id: UUID | str, g: dict[str, Any], plan: Any, draft_attrs: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Record the turn-brain's proposed extractions through the EXISTING deterministic recorders — the
    never-assert boundary is preserved: a field is PROMOTED to canonical (``confirm_draft``) ONLY when
    the owner confirmed it, and an extracted ``business_type`` is promoted ONLY when it is a valid
    taxonomy key (garbage is recorded as a plain answer, never asserted as fact). Returns the updated
    (answers, skipped); the caller persists them via ``_advance`` (this does NOT write the queue)."""
    answers = dict(g.get("answers") or {})
    skipped = list(g.get("skipped") or [])

    # 1. Record every extracted answer (gap-style; the body IS the value — parity with the walker).
    for fieldname, value in (plan.extracted_answers or {}).items():
        v = _coerce_answer(value)
        if fieldname and v:
            answers[fieldname] = v

    # 2. Promote CONFIRMED fields through the promotion gate (confirm_draft). A confirmed field with no
    #    explicit new value takes the discovered draft value. business_type is taxonomy-guarded so the
    #    LLM can never promote un-validated garbage as fact (CL-390 never-assert).
    from orchestrator.onboarding.business_type_reconcile import is_valid_business_type

    promote: dict[str, Any] = {}
    for fieldname in plan.mark_confirmed or ():
        raw = plan.extracted_answers.get(fieldname, draft_attrs.get(fieldname))
        v = _coerce_answer(raw)
        if not fieldname or not v:
            continue
        if fieldname == "business_type" and not is_valid_business_type(v):
            answers[fieldname] = v  # record as a free answer, but NEVER assert an off-taxonomy type
            continue
        answers[fieldname] = v
        promote[fieldname] = v
    if promote:
        _confirm(tenant_id, promote)
    return answers, skipped


def _advance_cursor_past_answered(g: dict[str, Any], answers: dict[str, Any], skipped: list[str]) -> int:
    """The new cursor = the first queue entry from the current cursor whose field is neither answered
    nor skipped. The turn-brain may resolve several fields (or an out-of-order one) in a single reply,
    so the cursor can jump past all of them — fewer turns, no burden. Past the end → the queue (the
    conductor-composed objective set) is exhausted → completion. Preserves the durable-queue spine."""
    queue = list(g.get("question_queue") or [])
    cursor = int(g.get("cursor") or 0)
    ans = set(answers or {})
    skip = set(skipped or [])
    c = max(cursor, 0)
    while c < len(queue) and (queue[c].get("field") in ans or queue[c].get("field") in skip):
        c += 1
    return c


_URL_RE = re.compile(r"(?:https?://|www\.)[^\s>\"']+", re.IGNORECASE)


def _maybe_refresh_owner_website(
    tenant_id: UUID | str, body: str, draft_attrs: dict[str, Any]
) -> None:
    """VT-568/569 follow-up (live drill): when the owner's message names a URL, record it as the
    owner-stated website and fire the async website-source refresh (``website_refresh_workflow``)
    so the NEXT turn genuinely knows the site — the agent must never claim to have "checked" a site
    it hasn't. No-op when the draft already carries this website (idempotent per URL). Fully
    fail-soft: a refresh failure never touches the reply path."""
    try:
        m = _URL_RE.search(body or "")
        if not m:
            return
        url = m.group(0).rstrip(".,;:!?)")
        if not url.lower().startswith("http"):
            url = f"https://{url}"
        current = str(draft_attrs.get("website") or "")
        if current and current.rstrip("/").lower() == url.rstrip("/").lower():
            return  # already known — nothing to refresh
        from dbos import DBOS

        from orchestrator.onboarding.auto_discovery import website_refresh_workflow

        DBOS.start_workflow(website_refresh_workflow, str(tenant_id), url)
        draft_attrs["website"] = url  # visible to THIS turn's prompt as an owner-stated fact
        logger.info("journey: owner-stated website refresh fired (tenant=%s)", tenant_id)
    except Exception:  # noqa: BLE001 — enrichment only; the reply path must never break
        logger.warning("journey: owner-website refresh failed (fail-soft)", exc_info=True)


def _handle_reply_with_turn_brain(
    tenant_id: UUID | str, body: str, message_sid: str | None, *, lang: str = "en", is_start: bool = False
) -> dict[str, Any]:
    """The LLM-driven per-reply path. Composes the SAY + interprets the reply via the turn-brain, then
    records extractions through the EXISTING deterministic recorders and advances the durable cursor.
    FAIL-SOFT: if the turn-brain returns None (LLM error/timeout/unparseable), fall back to the
    deterministic walker for THIS turn — onboarding never stalls. Idempotent on redelivery (same as the
    walker): a redelivered sid re-presents without re-invoking the LLM or double-applying."""
    g = get_journey(tenant_id)
    if g is None or g["status"] != "active":
        return {"reply_en": "", "reply_hi": "", "done": True}

    # Idempotency: a redelivered inbound must not re-invoke the LLM nor double-advance. Mirror the
    # walker — signal already_presented so the intercept does NOT re-send (the first delivery sent it).
    if message_sid and message_sid == g.get("last_message_sid"):
        return {"already_presented": True, "done": _current(g) is None}

    from orchestrator.onboarding import turn_brain
    from orchestrator.onboarding.draft_profile import get_draft

    draft = get_draft(tenant_id)
    draft_attrs = dict(draft.get("attributes") or {})
    provenance = dict(draft.get("provenance") or {})

    # VT-568/569 follow-up (live drill): the owner naming their OWN website mid-chat is the strongest
    # identity anchor there is — record it + fire the async website-source refresh so the NEXT turn
    # genuinely knows what the site says (the agent must never fake having "checked" it). Fail-soft;
    # fires at most once per distinct URL (the draft merge makes the second detection a no-op check).
    _maybe_refresh_owner_website(tenant_id, body, draft_attrs)

    plan = turn_brain.compose_turn(
        g, draft_attrs, body, locale=lang, provenance=provenance, is_start=is_start
    )
    if plan is None:
        # Fail-soft: the deterministic walker owns this turn (and applies the VT-569a bare-no re-prompt).
        return handle_reply(tenant_id, body, message_sid, lang=lang)

    answers, skipped = _apply_turn_plan(tenant_id, g, plan, draft_attrs)
    new_cursor = _advance_cursor_past_answered(g, answers, skipped)
    _advance(tenant_id, new_cursor, answers, skipped, message_sid)

    done = new_cursor >= len(g.get("question_queue") or [])
    if done:
        _complete(tenant_id)
        completion = _completion_message()
        # On completion send the durable closer (not a possibly-questioning LLM line); the integration
        # seam then continues the conversation, so the owner is never left on a dangling question.
        return {
            "turn_brain": True,
            "reply_text": completion["reply_hi"] if lang == "hi" else completion["reply_en"],
            "buttons": [],
            "done": True,
        }
    return {
        "turn_brain": True,
        "reply_text": plan.reply_text,
        "buttons": list(plan.buttons),
        "done": False,
    }


def _fire_integration_seam(tenant_id: UUID | str, recipient: str | None) -> None:
    """VT-425 — the journey → connector-onboarding handoff on profile-confirm completion. Best-effort +
    fail-OPEN: a seam failure must never block the journey's own completion (already committed)."""
    try:
        from orchestrator.onboarding.shopify_onboarding import begin_shopify_onboarding

        begin_shopify_onboarding(tenant_id, recipient)
    except Exception:  # noqa: BLE001 — seam is best-effort; journey completion already committed
        logger.exception("journey→integration seam (begin_shopify_onboarding) failed tenant=%s", tenant_id)


def _run_turn_brain_and_send(
    tenant_id: UUID | str, body: str, message_sid: str | None, recipient: str | None,
    *, lang: str, is_start: bool,
) -> dict[str, Any]:
    """Run the turn-brain reply path, SEND the result, and fire the completion seam. Sends the composed
    ``reply_text`` (+ any buttons) for a turn-brain result, or the deterministic reply for a fail-soft
    walker fallback (which carries reply_en/reply_hi, not reply_text)."""
    r = _handle_reply_with_turn_brain(tenant_id, body, message_sid, lang=lang, is_start=is_start)
    if not r.get("already_presented"):
        if r.get("turn_brain"):
            _send_turn(recipient, r.get("reply_text", ""), r.get("buttons") or [], lang)
        else:
            _send(recipient, {"prompt_en": r.get("reply_en", ""), "prompt_hi": r.get("reply_hi", "")}, lang)
    if r.get("done"):
        _fire_integration_seam(tenant_id, recipient)
    return r


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
                # The first presented question is the head of the just-composed queue (the cursor at
                # 0). VT-462: the QUEUE itself is conductor-composed (``_compose_queue`` orders it via
                # the conductor's registry-grounded decision over the just-discovered draft), so the
                # cursor head already reflects the conductor's dynamic pick — no separate per-reply
                # conductor call (that would break the cursor/apply contract).
                g = get_journey(tenant_id) or g
                # VT-569 — the JOURNEY-START turn. With the turn-brain on, greet ONCE + open with the
                # first question conversationally (no "Hi! {canned question}" prefix) and absorb any info
                # the owner volunteered in their first message. Fail-soft: any turn-brain error falls
                # back to the deterministic first-question send below.
                if _turn_brain_enabled():
                    try:
                        return _run_turn_brain_and_send(
                            tenant_id, body, message_sid, recipient, lang=lang, is_start=True
                        )
                    except Exception:  # noqa: BLE001 — start turn falls back to the deterministic opener
                        logger.exception(
                            "journey: turn-brain start turn failed tenant=%s — deterministic opener", tenant_id
                        )
                _send(recipient, _current(g) or _opener(), lang)
            else:
                _send(recipient, _opener(), lang)  # still setting up
            return {"done": False, "pending": True}
        # VT-478 — LAZY recompose of a STALE queue, BEFORE the current confirm question is presented.
        # VT-475 fixed forward composition but never recomposed EXISTING active queues, so a tenant
        # whose queue was composed pre-VT-475 keeps being asked the wrong confirm (the raw GBP
        # ``category``, e.g. "Telecommunications service provider?") instead of the reconciled
        # ``business_type``. If the question about to be presented is a confirm whose ``draft_value``
        # is stale vs the live reconcile, swap the un-answered confirm tail in-place — preserving
        # cursor/answers/skipped/last_message_sid (the idempotency marker + no-double-advance survive).
        # Cheap (only when a confirm is the cursor head) + fail-OPEN (any error → existing queue stands,
        # the owner inbound is never blocked). This auto-heals any mid-journey tenant on their next
        # inbound — no migration/sweep needed.
        cur = _current(g)
        if cur is not None and cur.get("kind") == "confirm":
            _, btype = _tenant_phase_and_type(tenant_id)
            if _recompose_stale_confirms(tenant_id, g, btype):
                g = get_journey(tenant_id) or g  # re-read so a downstream read sees the healed queue
        # VT-569 — the LLM turn-brain reply path (behind ONBOARDING_TURN_BRAIN). It composes the SAY +
        # interprets the reply, recording extractions through the SAME deterministic recorders and
        # advancing the durable cursor. Any turn-brain failure that reaches here falls back to the
        # deterministic walker for this turn (onboarding never stalls); a persist failure means nothing
        # was sent, so the deterministic path re-runs cleanly. The stale-confirm heal above still runs
        # first so the brain presents the reconciled draft, not the pre-VT-475 category.
        if _turn_brain_enabled():
            try:
                return _run_turn_brain_and_send(
                    tenant_id, body, message_sid, recipient, lang=lang, is_start=False
                )
            except Exception:  # noqa: BLE001 — turn-brain failure → deterministic walker (never stall)
                logger.exception(
                    "journey: turn-brain reply path failed tenant=%s — deterministic walker", tenant_id
                )
        r = handle_reply(tenant_id, body, message_sid, lang=lang)
        # Idempotent presentation: a redelivered inbound re-emits the SAME in-flight question that was
        # already presented on its first delivery — do NOT send it again (the live duplicate-question
        # bug). ``handle_reply`` flags this with ``already_presented``. A FIRST presentation, a normal
        # advance, and a conversational re-present (``re_present`` — a bare greeting mid-question) all
        # DO send.
        if not r.get("already_presented"):
            _send(recipient, {"prompt_en": r["reply_en"], "prompt_hi": r["reply_hi"]}, lang)
        # VT-425 — the journey → integration SEAM (sequential handoff, CL-443 plan §8 option a).
        # When the journey COMPLETES profile-confirm on THIS reply, hand off to connector
        # onboarding so the owner isn't dropped into a cold brain between the two spines. The
        # journey owns "confirm who you are"; the integration onboarding owns "connect your data".
        # Best-effort + fail-OPEN: a seam failure must never block the journey's own completion.
        if r.get("done"):
            _fire_integration_seam(tenant_id, recipient)
        return r
    except Exception:  # noqa: BLE001 — owner-inbound HOT PATH: any failure falls through, never blocks
        logger.exception("maybe_handle_journey_reply failed tenant=%s — fall through", tenant_id)
        return None


__all__ = [
    "start_journey", "set_queue_if_empty", "get_journey", "is_active", "handle_reply",
    "maybe_handle_journey_reply", "_recompose_stale_confirms",
]
