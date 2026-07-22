"""VT-367 Gap-3 — DB-backed behavioral tests for the owner-inbound INTERCEPT
(``orchestrator.onboarding.journey.maybe_handle_journey_reply``).

The intercept is the hot-path gate that routes an owner's inbound to the paced
journey BEFORE the generic brain. Its contract:

  (a) LAZY-START — no journey + a FRESH phase (onboarding/trial/lapsed) →
      compose the question set from the 2a draft + start the journey + send the
      first question, returning {started: True}. The owner's first message must
      never reach the cold brain.
  (b) ACTIVE journey → delegate to ``handle_reply`` + send + return its dict.
  (c) ESTABLISHED tenant (e.g. phase='paid_active') + no journey → return None
      (fall through; the normal brain runs).
  (d) FAIL-OPEN — any exception → None (owner-inbound is never blocked).
  (e) COMPLETE / abandoned journey → None (fall through).

Requires a real Postgres + the dbos stack. Mirrors the substrate pattern in
``tests/orchestrator/onboarding/test_draft_profile.py``: migrations applied
once, DBOS launched so the ``tenant_connection`` pool exists, tenants + drafts
seeded for the RLS'd app_role path.

The Twilio send is never live: ``send_freeform_message`` is monkeypatched to a
no-op SPY throughout, both to keep tests off the network and to ASSERT the
intercept sent the right question. (The package autouse fixture already stubs
the Twilio client; the spy additionally lets us assert send happened.)
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
    reason="DATABASE_URL not set — VT-367 journey-intercept substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so ``graph._pool`` exists. Mirrors
    test_dsr_purge_substrate / test_draft_profile."""
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


# --- Seeding helpers (direct service-role / BYPASSRLS) ---------------------


def _new_tenant(
    dsn: str,
    *,
    name: str = "VT-367 intercept test",
    phase: str = "trial",
    business_type: str = "restaurant",
) -> UUID:
    """Seed a tenant with phase + business_type (the columns the intercept reads)."""
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
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, question_queue, cursor FROM onboarding_journey "
            "WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    return {"status": row[0], "question_queue": list(row[1] or []), "cursor": row[2]}


@pytest.fixture
def send_spy(monkeypatch):  # type: ignore[no-untyped-def]
    """Monkeypatch BOTH owner-send paths ``journey._send`` may take to a no-op spy. ``journey._send``
    imports them at call time, so patching the module attributes intercepts them. Returns the list of
    (text, recipient) calls — VT-479: a CONFIRM question now sends via ``send_interactive_message``
    (Yes/No/Skip buttons), a gap/re-present via ``send_freeform_message``; the spy NORMALIZES both to
    (question_text, recipient) so the existing assertions ("the question was sent") hold for either path.
    """
    from orchestrator.utils import twilio_send

    calls: list[tuple[str, str]] = []

    def _freeform_spy(body: str, recipient_phone: str, **_kw) -> str:
        calls.append((body, recipient_phone))
        return "SM" + "0" * 32

    def _interactive_spy(content_sid: str, recipient_phone: str, *, content_variables=None, **_kw) -> str:
        # VT-479: the confirm question text is content_variables["1"]; normalize to (text, recipient).
        text = (content_variables or {}).get("1", "")
        calls.append((text, recipient_phone))
        return "SM" + "0" * 32

    monkeypatch.setattr(twilio_send, "send_freeform_message", _freeform_spy)
    monkeypatch.setattr(twilio_send, "send_interactive_message", _interactive_spy)
    return calls


# --- (a) NO-JOURNEY fall-through + PENDING-fill (journeys start at the signup seam, not lazily) ----


def test_no_journey_trial_tenant_falls_through(substrate, send_spy):  # type: ignore[no-untyped-def]
    """A trial tenant with NO journey row → None (fall through). The journey is created at the SIGNUP
    seam (pending), NOT lazily on an arbitrary inbound — so a tenant without a journey row is NEVER
    intercepted. THIS is the regression fix: owner-inbound for non-onboarding / direct-seeded tenants
    (e.g. the twilio-ingress tests) reaches the brain untouched."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="trial no journey", phase="trial")
    result = journey.maybe_handle_journey_reply(tenant, "hi", "SM-nj-1", recipient="+919999000111")
    assert result is None, "no journey row → must fall through to the normal pipeline"
    assert _journey_row(substrate.dsn, tenant) is None, "no journey may be created on an inbound"
    assert send_spy == [], "nothing sent on a no-journey fall-through"


def test_pending_journey_fills_queue_from_draft(substrate, send_spy, monkeypatch):  # type: ignore[no-untyped-def]
    """A PENDING journey (started at signup with an EMPTY queue) whose draft has since landed → the
    next inbound composes the queue (2b monkeypatched, NO live LLM), fills it, and sends the first
    question. Returns {pending: True}. This is the 'start at signup regardless of draft-readiness'
    path: the owner's first message routes to the journey, never the cold brain."""
    from orchestrator.onboarding import journey, question_brain
    from orchestrator.onboarding.draft_profile import write_draft

    tenant = _new_tenant(substrate.dsn, name="pending fill", phase="trial")
    journey.start_journey(tenant, [])  # signup-style PENDING start (empty queue)
    write_draft(tenant, {"category": "restaurant"}, source="gbp")  # the async draft landed

    fixed = [
        question_brain.Question(field="category", kind="confirm",
                                prompt_en="We found you're a restaurant — is that right?",
                                prompt_hi="हमें पता चला आप restaurant हैं — सही है?", draft_value="restaurant"),
        question_brain.Question(field="operating_hours", kind="gap",
                                prompt_en="What are your operating hours?", prompt_hi="समय क्या हैं?"),
    ]
    monkeypatch.setattr(question_brain, "compose_onboarding_questions", lambda *a, **k: fixed)

    result = journey.maybe_handle_journey_reply(tenant, "hi", "SM-pf-1", recipient="+919999000222")

    assert result is not None and result.get("pending") is True
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None and row["status"] == "active"
    assert len(row["question_queue"]) == 2, "the composed queue must be installed on the pending journey"
    assert row["question_queue"][0]["field"] == "category"
    assert len(send_spy) == 1 and "restaurant" in send_spy[0][0], "the first question must be sent"


# --- (b) ACTIVE journey -----------------------------------------------------


def test_active_journey_delegates_to_handle_reply(substrate, send_spy):  # type: ignore[no-untyped-def]
    """An ACTIVE journey → the intercept delegates to ``handle_reply``, sends the
    next question, and returns its result dict (reply_en/reply_hi/done)."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="active delegate", phase="trial")
    journey.start_journey(
        tenant,
        [
            {
                "field": "operating_hours",
                "kind": "gap",
                "prompt_en": "What are your operating hours?",
                "prompt_hi": "आपके काम के घंटे क्या हैं?",
                "draft_value": None,
            },
            {
                "field": "price_range",
                "kind": "gap",
                "prompt_en": "What is your price range?",
                "prompt_hi": "आपकी कीमत सीमा क्या है?",
                "draft_value": None,
            },
        ],
    )

    result = journey.maybe_handle_journey_reply(
        tenant, "9am to 11pm", "SM-active-1", recipient="+919999000222"
    )

    assert result is not None
    assert result.get("started") is None, "an active-journey result is not a lazy-start"
    assert result["done"] is False
    # handle_reply advanced to the second question — that's what got returned + sent.
    assert "price range" in result["reply_en"].lower()

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None and row["cursor"] == 1, "handle_reply must have advanced"

    assert len(send_spy) == 1
    sent_body, sent_to = send_spy[0]
    assert sent_to == "+919999000222"
    assert "price range" in sent_body.lower(), "the NEXT question must be sent"


# --- (b2) idempotent PRESENTATION — an already-presented pending Q isn't re-sent -------------------


def test_redelivered_inbound_does_not_resend_pending_question(substrate, send_spy):  # type: ignore[no-untyped-def]
    """THE live duplicate-question bug ("based in Mumbai?" sent TWICE): a redelivered inbound (same
    message_sid as the one already in flight) must NOT re-send the pending question — it was already
    presented on the first delivery. The FIRST presentation DOES send; the redelivery does not."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="no-resend on redeliver", phase="trial")
    journey.start_journey(
        tenant,
        [
            {"field": "operating_hours", "kind": "gap", "prompt_en": "What are your hours?",
             "prompt_hi": "समय?", "draft_value": None},
            {"field": "price_range", "kind": "gap", "prompt_en": "What is your price range?",
             "prompt_hi": "कीमत?", "draft_value": None},
        ],
    )

    # First inbound (new sid) → answer recorded, NEXT question presented + SENT once.
    journey.maybe_handle_journey_reply(tenant, "9 to 9", "SM-rd-1", recipient="+919999001000")
    assert len(send_spy) == 1, "the first presentation must send the next question"
    assert "price range" in send_spy[0][0].lower()
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None and row["cursor"] == 1

    # Redeliver the SAME sid → the in-flight (price_range) question is already presented → NO re-send.
    journey.maybe_handle_journey_reply(tenant, "9 to 9", "SM-rd-1", recipient="+919999001000")
    assert len(send_spy) == 1, (
        "a redelivered inbound must NOT re-send the already-presented pending question "
        f"(the live duplicate bug); sends={send_spy!r}"
    )
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None and row["cursor"] == 1, "redelivery must not advance the cursor"


def test_bare_greeting_mid_journey_re_presents_without_advancing(substrate, send_spy):  # type: ignore[no-untyped-def]
    """A bare greeting mid-journey (the live "Hi → category" bug) routes through the intercept: it is
    NOT recorded / does NOT advance, and the intercept SENDS a conversational re-present (greet-back +
    the pending question)."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="greeting re-present intercept", phase="trial")
    journey.start_journey(
        tenant,
        [{"field": "category", "kind": "confirm",
          "prompt_en": "We found you're a restaurant — is that right?",
          "prompt_hi": "रेस्टोरेंट — सही है?", "draft_value": "restaurant"}],
    )

    result = journey.maybe_handle_journey_reply(tenant, "Hi", "SM-greet-icpt-1", recipient="+919999001111")

    assert result is not None and result.get("re_present") is True
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None and row["cursor"] == 0, "a greeting must not advance the cursor"
    assert len(send_spy) == 1, "the intercept must SEND the conversational re-present"
    assert "is that right" in send_spy[0][0].lower(), "the re-present must re-ask the pending question"


# --- (c) ESTABLISHED tenant, no journey → fall through ----------------------


def test_established_tenant_no_journey_returns_none(substrate, send_spy):  # type: ignore[no-untyped-def]
    """An ESTABLISHED tenant (phase='paid_active', NOT a lazy-start phase) with
    no journey → the intercept returns None (fall through; the brain runs). No
    journey is created and nothing is sent."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(
        substrate.dsn, name="established paid_active", phase="paid_active"
    )

    result = journey.maybe_handle_journey_reply(
        tenant, "hello", "SM-established-1", recipient="+919999000333"
    )

    assert result is None, "established tenant must fall through to the normal brain"
    assert _journey_row(substrate.dsn, tenant) is None, (
        "no journey may be lazy-started for an established tenant"
    )
    assert send_spy == [], "nothing should be sent on a fall-through"


# --- (d) FAIL-OPEN ----------------------------------------------------------


def test_fail_open_on_internal_error_returns_none(substrate, send_spy, monkeypatch):  # type: ignore[no-untyped-def]
    """FAIL-OPEN: if an internal step raises (here: ``get_journey``), the
    intercept swallows it and returns None — it NEVER propagates, so owner-
    inbound is never blocked by a journey bug."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="fail-open", phase="trial")

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("simulated journey-substrate failure")

    monkeypatch.setattr(journey, "get_journey", _boom)

    # Must NOT raise.
    result = journey.maybe_handle_journey_reply(
        tenant, "hi", "SM-failopen-1", recipient="+919999000444"
    )
    assert result is None, "a journey error must fall through to the brain, not block"


# --- (e) COMPLETE journey → fall through ------------------------------------


def test_complete_journey_drives_flow_then_falls_through(substrate, send_spy):  # type: ignore[no-untyped-def]
    """VT-576/CL-2026-07-03: a COMPLETE journey no longer falls straight through — its next inbound
    enters the PACED post-profile flow (readiness ask). Only once the flow reaches its TERMINAL beat
    (``plan_kicked``) does the intercept return None, handing the owner to the normal brain."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="complete drives flow", phase="trial")
    # Drive the journey to completion: a single-question queue + one answer.
    journey.start_journey(
        tenant,
        [
            {
                "field": "operating_hours",
                "kind": "gap",
                "prompt_en": "What are your operating hours?",
                "prompt_hi": "आपके काम के घंटे क्या हैं?",
                "draft_value": None,
            }
        ],
    )
    journey.handle_reply(tenant, "9 to 9", "SM-drive-complete")
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None and row["status"] == "complete"
    g = journey.get_journey(tenant)
    assert g is not None and g["answers"].get("__flow__") == "profile_previewed", "completion opens the paced flow"

    # The owner's next message enters the flow (VT-698: the team INTRO leads), NOT a fall-through.
    send_spy.clear()
    result = journey.maybe_handle_journey_reply(
        tenant, "another inbound", "SM-complete-flow", recipient="+919999000555"
    )
    assert result is not None and result.get("routed") == "flow_team_intro"

    # Once the flow reaches its terminal beat, the intercept falls through to the brain.
    journey._set_flow(tenant, journey._FLOW_PLAN_KICKED)
    send_spy.clear()
    terminal = journey.maybe_handle_journey_reply(
        tenant, "hi again", "SM-complete-fallthrough", recipient="+919999000555"
    )
    assert terminal is None, "a terminal (plan_kicked) flow falls through to the normal brain"
    assert send_spy == [], "nothing is sent on the terminal fall-through"


# --- (f) OPT-OUT / DSR / STOP mid-journey ALWAYS wins (VT-329 / DPDP) --------


@pytest.mark.parametrize(
    "optout_body",
    ["STOP", "unsubscribe", "delete my data", "बंद करो", "मेरा डेटा हटाओ", "band karo"],
)
def test_optout_during_active_journey_falls_through_not_consumed(substrate, send_spy, optout_body):  # type: ignore[no-untyped-def]
    """VT-329/DPDP (compliance-critical): an ACTIVE-journey owner sending opt-out/DSR/STOP (EN +
    Devanagari + Hinglish) must route to the authoritative opt-out/DSR path — the intercept returns
    None (fall through to pre_filter), and the journey cursor MUST NOT advance / store it as an answer.
    Opt-out always wins; it is never swallowed mid-journey."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="optout mid-journey", phase="trial")
    journey.start_journey(
        tenant,
        [{"field": "operating_hours", "kind": "gap", "prompt_en": "What are your hours?",
          "prompt_hi": "समय?", "draft_value": None}],
    )
    before = _journey_row(substrate.dsn, tenant)

    result = journey.maybe_handle_journey_reply(tenant, optout_body, "SM-optout", recipient="+919999000666")

    assert result is None, f"opt-out {optout_body!r} must fall through to the opt-out/DSR handler"
    after = _journey_row(substrate.dsn, tenant)
    assert after is not None and after["cursor"] == before["cursor"], "opt-out must NOT advance the cursor"
    assert after["status"] == "active", "opt-out must not complete/abandon the journey"
    assert send_spy == [], "no journey question is sent on an opt-out"
