"""VT-367 Gap-3 — DB-backed behavioral tests for the guided onboarding journey
(``orchestrator.onboarding.journey``): ``handle_reply`` and the journey-state
primitives (``start_journey`` / ``set_queue_if_empty`` / ``get_journey``).

``handle_reply`` is THE heart of the paced journey. Its load-bearing behaviours:

  - confirm-kind Q + a yes token → calls 2a ``confirm_draft`` (the never-assert
    promotion gate), so the confirmed field lands in the canonical
    ``business_profile`` (l1_entities entity_type='business_profile'), and the
    answer is recorded + cursor advances;
  - gap-kind Q → the body IS the value, stored in ``answers``, cursor advances;
  - a 'skip' token → the field is added to ``skipped``, cursor advances;
  - IDEMPOTENT redelivery: a redelivered ``message_sid`` (== last_message_sid)
    re-emits the SAME current question WITHOUT advancing the cursor (WhatsApp
    redelivers; a double-advance would silently drop a question);
  - on queue exhaustion → status flips to 'complete' AND the named Gap-4 seam
    fires the ``onboarding_journey_completed`` observability event.

Requires a real Postgres + the dbos stack. Mirrors the substrate pattern in
``tests/orchestrator/test_dsr_purge_substrate.py`` /
``tests/orchestrator/onboarding/test_draft_profile.py``: migrations applied
once, DBOS launched so the ``tenant_connection`` pool exists, tenants seeded via
a direct service-role (BYPASSRLS) psycopg connection. The journey writers go
through ``tenant_connection`` (the RLS'd app_role path); assertions read back via
direct service-role SELECTs.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-367 journey substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so ``graph._pool`` (the substrate the
    ``tenant_connection`` path resolves) exists. Mirrors test_dsr_purge_substrate."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


# --- Seeding + readback helpers (direct service-role / BYPASSRLS) ----------


def _new_tenant(
    dsn: str,
    *,
    name: str = "VT-367 journey test",
    phase: str = "trial",
    business_type: str = "restaurant",
) -> UUID:
    """Seed a tenant with the columns the journey intercept reads (phase,
    business_type) via a direct service-role connection (RLS bypassed at seed)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, "
            "phase_entered_at, business_type, whatsapp_number) "
            "VALUES (%s, 'founding', %s, now(), %s, %s) RETURNING id",
            (
                name,
                phase,
                business_type,
                f"+9199{uuid4().int % 10**8:08d}",
            ),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _journey_row(dsn: str, tenant_id: UUID) -> dict[str, Any] | None:
    """The raw onboarding_journey row via a direct service-role SELECT. Default
    psycopg row_factory → tuple; index positionally."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, question_queue, cursor, answers, skipped, "
            "last_message_sid FROM onboarding_journey WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    return {
        "status": row[0],
        "question_queue": list(row[1] or []),
        "cursor": row[2],
        "answers": dict(row[3] or {}),
        "skipped": list(row[4] or []),
        "last_message_sid": row[5],
    }


def _canonical_profile_attributes(dsn: str, tenant_id: UUID) -> dict[str, Any] | None:
    """The canonical business_profile attributes via a direct service-role SELECT
    on l1_entities (entity_type='business_profile'). ``None`` if no row exists.
    Default psycopg row_factory → tuple."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT attributes FROM l1_entities "
            "WHERE tenant_id = %s AND entity_type = 'business_profile'",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    return dict(row[0] or {})


# --- Question-queue builders (dicts as stored in question_queue) ------------


def _confirm_q(field: str, draft_value: Any) -> dict[str, Any]:
    return {
        "field": field,
        "kind": "confirm",
        "prompt_en": f"We found {field}: {draft_value} — correct?",
        "prompt_hi": f"हमें {field} मिला: {draft_value} — सही है?",
        "draft_value": draft_value,
    }


def _gap_q(field: str) -> dict[str, Any]:
    return {
        "field": field,
        "kind": "gap",
        "prompt_en": f"Could you tell us your {field}?",
        "prompt_hi": f"क्या आप अपना {field} बता सकते हैं?",
        "draft_value": None,
    }


# --- Tests: state primitives -----------------------------------------------


def test_start_journey_and_get_journey_basics(substrate):  # type: ignore[no-untyped-def]
    """``start_journey`` INSERTs an active journey at cursor 0 with the supplied
    queue, empty answers/skipped; ``get_journey`` reads it back shaped."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="start-journey basics")
    queue = [_confirm_q("category", "restaurant"), _gap_q("operating_hours")]

    assert journey.get_journey(tenant) is None, "no journey before start"

    journey.start_journey(tenant, queue)

    g = journey.get_journey(tenant)
    assert g is not None
    assert g["status"] == "active"
    assert g["cursor"] == 0
    assert g["question_queue"] == queue
    assert g["answers"] == {}
    assert g["skipped"] == []
    assert g["last_message_sid"] is None
    assert journey.is_active(tenant) is True


def test_start_journey_replaces_existing(substrate):  # type: ignore[no-untyped-def]
    """A second ``start_journey`` (ON CONFLICT) RESETS the row: new queue,
    cursor back to 0, answers/skipped cleared, status active."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="start-journey reset")
    journey.start_journey(tenant, [_gap_q("a"), _gap_q("b")])
    journey.handle_reply(tenant, "first answer", "SM-reset-1")
    mid = journey.get_journey(tenant)
    assert mid is not None and mid["cursor"] == 1 and mid["answers"]

    new_queue = [_confirm_q("city", "Pune")]
    journey.start_journey(tenant, new_queue)

    g = journey.get_journey(tenant)
    assert g is not None
    assert g["status"] == "active"
    assert g["cursor"] == 0
    assert g["question_queue"] == new_queue
    assert g["answers"] == {}
    assert g["skipped"] == []


def test_set_queue_if_empty_fills_only_when_empty(substrate):  # type: ignore[no-untyped-def]
    """``set_queue_if_empty`` installs the composed queue ONLY when the active
    journey's queue is still empty; it NEVER clobbers a non-empty queue."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="set-queue-if-empty")

    # Start pending (empty queue) — the lazy-start shape.
    journey.start_journey(tenant, [])
    g = journey.get_journey(tenant)
    assert g is not None and g["question_queue"] == []

    filled = [_gap_q("products"), _gap_q("price_range")]
    journey.set_queue_if_empty(tenant, filled)
    g = journey.get_journey(tenant)
    assert g is not None and g["question_queue"] == filled, "empty queue must fill"

    # A second call must NOT clobber the now-non-empty queue.
    journey.set_queue_if_empty(tenant, [_gap_q("never")])
    g = journey.get_journey(tenant)
    assert g is not None and g["question_queue"] == filled, (
        "set_queue_if_empty clobbered a non-empty queue"
    )


def test_set_queue_if_empty_noop_on_complete(substrate):  # type: ignore[no-untyped-def]
    """``set_queue_if_empty`` only touches ACTIVE journeys — a complete journey
    (status != 'active') is left untouched even if its queue is empty."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="set-queue complete noop")
    # Empty queue → first handle_reply exhausts and completes immediately.
    journey.start_journey(tenant, [])
    journey.handle_reply(tenant, "hello", "SM-complete-noop")
    g = journey.get_journey(tenant)
    assert g is not None and g["status"] == "complete"

    journey.set_queue_if_empty(tenant, [_gap_q("late")])
    g = journey.get_journey(tenant)
    assert g is not None
    assert g["status"] == "complete", "set_queue_if_empty must not revive a complete journey"
    assert g["question_queue"] == [], "complete journey queue must stay empty"


# --- Tests: handle_reply — the heart ---------------------------------------


def test_handle_reply_confirm_promotes_to_canonical_profile(substrate):  # type: ignore[no-untyped-def]
    """A confirm-kind Q + a yes token → ``handle_reply`` calls 2a
    ``confirm_draft({field: draft_value})``, which promotes the field to the
    canonical business_profile (l1_entities entity_type='business_profile').
    The answer is recorded in ``answers`` and the cursor advances. THE never-
    assert promotion boundary fires only on the owner's confirmation."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="confirm promotes")
    journey.start_journey(
        tenant, [_confirm_q("category", "restaurant"), _gap_q("operating_hours")]
    )

    # Nothing promoted before the owner confirms.
    assert _canonical_profile_attributes(substrate.dsn, tenant) is None

    r = journey.handle_reply(tenant, "yes", "SM-confirm-1")
    assert r["done"] is False
    # Next question is the gap Q (cursor advanced).
    assert "operating_hours" in r["reply_en"]

    g = journey.get_journey(tenant)
    assert g is not None
    assert g["cursor"] == 1, "confirm must advance the cursor"
    assert g["answers"].get("category") == "restaurant", (
        "confirmed draft_value must be recorded as the answer"
    )

    # The promotion gate fired: the confirmed field is now canonical fact.
    promoted = _canonical_profile_attributes(substrate.dsn, tenant)
    assert promoted is not None, "confirm_draft did not create the canonical profile"
    assert promoted.get("category") == "restaurant", (
        f"confirmed 'category' must be promoted to the canonical business_profile; "
        f"got {promoted!r}"
    )


def test_handle_reply_confirm_correction_is_the_value(substrate):  # type: ignore[no-untyped-def]
    """A confirm-kind Q + a NON-yes body → the body is treated as a CORRECTION
    value: it (not the draft_value) is recorded as the answer AND promoted."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="confirm correction")
    journey.start_journey(tenant, [_confirm_q("city", "Mumbai")])

    journey.handle_reply(tenant, "Actually Bengaluru", "SM-correct-1")

    g = journey.get_journey(tenant)
    assert g is not None
    assert g["answers"].get("city") == "Actually Bengaluru", (
        "a non-yes reply to a confirm Q must be stored as the correction value, "
        "not the draft_value"
    )
    promoted = _canonical_profile_attributes(substrate.dsn, tenant)
    assert promoted is not None
    assert promoted.get("city") == "Actually Bengaluru", (
        "the corrected value must be promoted to the canonical profile"
    )


def test_handle_reply_gap_stored_in_answers(substrate):  # type: ignore[no-untyped-def]
    """A gap-kind Q → the body IS the value: stored in ``answers`` under the
    field, cursor advances. A gap answer is NOT promoted to the canonical
    profile (only confirms hit ``confirm_draft``)."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="gap stored")
    journey.start_journey(tenant, [_gap_q("operating_hours"), _gap_q("price_range")])

    r = journey.handle_reply(tenant, "9am to 11pm daily", "SM-gap-1")
    assert r["done"] is False
    assert "price_range" in r["reply_en"], "next question should be the second gap"

    g = journey.get_journey(tenant)
    assert g is not None
    assert g["cursor"] == 1
    assert g["answers"].get("operating_hours") == "9am to 11pm daily"
    # A gap answer does not touch the canonical profile (no confirm_draft call).
    assert _canonical_profile_attributes(substrate.dsn, tenant) is None, (
        "a gap answer must NOT be promoted to the canonical business_profile"
    )


def test_handle_reply_skip_adds_to_skipped(substrate):  # type: ignore[no-untyped-def]
    """A 'skip' token → the field is added to ``skipped`` (NOT ``answers``),
    cursor advances. Hindi/Hinglish skip tokens work too (token-exact)."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="skip adds skipped")
    journey.start_journey(tenant, [_gap_q("price_range"), _gap_q("peak_days")])

    journey.handle_reply(tenant, "skip", "SM-skip-1")
    g = journey.get_journey(tenant)
    assert g is not None
    assert g["skipped"] == ["price_range"], "skipped field must be recorded"
    assert "price_range" not in g["answers"], "a skipped field is not an answer"
    assert g["cursor"] == 1

    # Hindi skip token ('baad') on the next field also skips.
    journey.handle_reply(tenant, "baad", "SM-skip-2")
    g = journey.get_journey(tenant)
    assert g is not None
    assert "peak_days" in g["skipped"], "Hindi/Hinglish skip token must be recognised"


def test_handle_reply_bare_greeting_mid_confirm_not_recorded(substrate):  # type: ignore[no-untyped-def]
    """THE live bug: a bare greeting ("Hi") to a CONFIRM question must NOT be recorded as the answer
    and must NOT advance the cursor — the question is re-presented (re_present) instead, with the
    field untouched so the owner can still answer it. ("Hi" became the category in the live DB.)"""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="greeting mid-confirm")
    journey.start_journey(
        tenant, [_confirm_q("category", "restaurant"), _confirm_q("city", "Mumbai")]
    )

    r = journey.handle_reply(tenant, "Hi", "SM-greet-1")

    # Re-presented, not advanced, not recorded.
    assert r["done"] is False
    assert r.get("re_present") is True, "a bare greeting must re-present the pending question"
    # The re-present re-asks the SAME confirm Q (the _confirm_q helper's template), greet-back prepended.
    assert "category: restaurant" in r["reply_en"], "the re-present must re-ask the SAME confirm Q"
    assert r["reply_en"].lower().startswith("hi!"), "the re-present greets back conversationally"

    g = journey.get_journey(tenant)
    assert g is not None
    assert g["cursor"] == 0, "a greeting must NOT advance the cursor"
    assert "category" not in g["answers"], "a greeting must NOT be recorded as the category answer"
    assert g["answers"] == {}, f"no answer may be recorded for a greeting; got {g['answers']!r}"
    # The canonical profile is untouched (no confirm_draft fired on a greeting).
    assert _canonical_profile_attributes(substrate.dsn, tenant) is None

    # A real answer on the NEXT (new-sid) inbound still works — the cursor contract is intact.
    journey.handle_reply(tenant, "yes", "SM-greet-2")
    g = journey.get_journey(tenant)
    assert g is not None and g["cursor"] == 1
    assert g["answers"].get("category") == "restaurant", "a real 'yes' after a greeting still confirms"


def test_handle_reply_bare_greeting_mid_gap_not_recorded(substrate):  # type: ignore[no-untyped-def]
    """A bare greeting ("namaste") to a GAP question is likewise re-presented, not stored as the value
    — a greeting is never an answer, regardless of question kind."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="greeting mid-gap")
    journey.start_journey(tenant, [_gap_q("operating_hours"), _gap_q("price_range")])

    r = journey.handle_reply(tenant, "namaste", "SM-greet-gap-1")
    assert r.get("re_present") is True
    g = journey.get_journey(tenant)
    assert g is not None
    assert g["cursor"] == 0, "a greeting must NOT advance the cursor on a gap question"
    assert "operating_hours" not in g["answers"], "a greeting is not a gap answer"


def test_handle_reply_bare_no_to_confirm_not_recorded_as_value(substrate):  # type: ignore[no-untyped-def]
    """A bare negative ("no") to a CONFIRM is NOT a value (a city isn't named "no") — it re-presents
    so the owner supplies the correct value, rather than recording "no" verbatim. A real CORRECTION
    ("Actually Bengaluru") is still a valid answer (covered by the correction test)."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="bare-no confirm")
    journey.start_journey(tenant, [_confirm_q("city", "Mumbai")])

    r = journey.handle_reply(tenant, "no", "SM-no-1")
    assert r.get("re_present") is True
    g = journey.get_journey(tenant)
    assert g is not None
    assert g["cursor"] == 0, "a bare 'no' must NOT advance the cursor"
    assert g["answers"].get("city") != "no", "a bare 'no' must NOT be recorded as the city value"
    assert g["answers"] == {}


def test_handle_reply_greeting_mixed_with_answer_is_recorded(substrate):  # type: ignore[no-untyped-def]
    """A greeting MIXED with substantive content ("hi 9am to 11pm") is NOT a bare greeting — it
    carries an answer and is recorded normally (only a BARE greeting is rejected)."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="greeting mixed answer")
    journey.start_journey(tenant, [_gap_q("operating_hours")])

    r = journey.handle_reply(tenant, "hi 9am to 11pm", "SM-mixed-1")
    assert r["done"] is True, "a real answer (mixed with a greeting) still advances + completes"
    g = journey.get_journey(tenant)
    assert g is not None
    assert g["answers"].get("operating_hours") == "hi 9am to 11pm", (
        "a greeting mixed with content is recorded as the answer, not rejected"
    )


def test_handle_reply_idempotent_redelivery_no_double_advance(substrate):  # type: ignore[no-untyped-def]
    """IDEMPOTENCY: a redelivered ``message_sid`` (== last_message_sid) re-emits
    the SAME current question WITHOUT advancing the cursor and WITHOUT mutating
    answers/skipped. WhatsApp redelivers; a double-advance silently drops a Q."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="idempotent redelivery")
    journey.start_journey(
        tenant, [_gap_q("operating_hours"), _gap_q("price_range"), _gap_q("peak_days")]
    )

    sid = "SM-redeliver-1"
    first = journey.handle_reply(tenant, "9 to 9", sid)
    after_first = _journey_row(substrate.dsn, tenant)
    assert after_first is not None
    assert after_first["cursor"] == 1
    assert after_first["last_message_sid"] == sid
    assert after_first["answers"].get("operating_hours") == "9 to 9"
    # The reply now prompts the SECOND question (cursor advanced once).
    assert "price_range" in first["reply_en"]

    # Redeliver the SAME sid with a DIFFERENT body — must be ignored as a duplicate.
    redelivered = journey.handle_reply(tenant, "totally different body", sid)
    after_redeliver = _journey_row(substrate.dsn, tenant)
    assert after_redeliver is not None
    assert after_redeliver["cursor"] == 1, (
        "redelivered duplicate message_sid advanced the cursor — double-advance bug"
    )
    assert after_redeliver["answers"] == after_first["answers"], (
        "redelivery mutated answers — the duplicate body must not be recorded"
    )
    # Re-emits the SAME current (second) question, not a new one.
    assert redelivered["done"] is False
    assert "price_range" in redelivered["reply_en"], (
        "redelivery must re-emit the SAME in-flight question"
    )

    # A genuinely NEW sid then advances normally (idempotency is sid-keyed, not stuck).
    journey.handle_reply(tenant, "mid-range", "SM-redeliver-2")
    after_new = _journey_row(substrate.dsn, tenant)
    assert after_new is not None
    assert after_new["cursor"] == 2
    assert after_new["answers"].get("price_range") == "mid-range"


def test_handle_reply_completion_fires_gap4_seam(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """On queue exhaustion ``handle_reply`` → status flips to 'complete' AND the
    Gap-4 seam fires the ``onboarding_journey_completed`` observability event.

    We monkeypatch ``orchestrator.observability.log.log_event`` to a spy (the
    real writer dispatches async on a daemon thread — racy to assert against),
    and assert it fired exactly once with that event_type and this tenant."""
    from orchestrator.observability import log as obs_log

    calls: list[dict[str, Any]] = []

    def _spy_log_event(**kwargs: Any) -> None:
        calls.append(kwargs)

    # ``_emit_gap4_seam`` imports log_event at call time from this module, so
    # patching the module attribute intercepts it.
    monkeypatch.setattr(obs_log, "log_event", _spy_log_event)

    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="completion seam")
    journey.start_journey(tenant, [_gap_q("operating_hours")])

    # One answer exhausts the single-question queue → completion.
    r = journey.handle_reply(tenant, "9 to 9", "SM-complete-1")
    assert r["done"] is True

    g = journey.get_journey(tenant)
    assert g is not None
    assert g["status"] == "complete", "queue exhaustion must flip status to complete"

    completed = [
        c for c in calls if c.get("event_type") == "onboarding_journey_completed"
    ]
    assert len(completed) == 1, (
        f"expected exactly one onboarding_journey_completed event; got "
        f"{[c.get('event_type') for c in calls]}"
    )
    seam = completed[0]
    assert str(seam.get("tenant_id")) == str(tenant), "seam must carry this tenant_id"
    assert seam.get("component") == "onboarding"
    assert (seam.get("payload") or {}).get("gap4_trigger") is True, (
        "the Gap-4 seam payload must carry the gap4_trigger flag"
    )


def test_handle_reply_on_complete_journey_returns_done(substrate):  # type: ignore[no-untyped-def]
    """``handle_reply`` against a non-active (complete) journey is a safe no-op
    that returns done=True (the caller falls through to the normal pipeline)."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="reply after complete")
    journey.start_journey(tenant, [_gap_q("operating_hours")])
    journey.handle_reply(tenant, "9 to 9", "SM-done-1")
    assert journey.get_journey(tenant)["status"] == "complete"  # type: ignore[index]

    r = journey.handle_reply(tenant, "another message", "SM-done-2")
    assert r["done"] is True
    # State unchanged — no new answer recorded.
    g = journey.get_journey(tenant)
    assert g is not None
    assert g["cursor"] == 1
    assert "another message" not in g["answers"].values()
