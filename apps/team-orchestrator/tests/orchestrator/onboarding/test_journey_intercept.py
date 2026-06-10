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
    """Monkeypatch ``send_freeform_message`` to a no-op spy. ``journey._send``
    imports it at call time (``from orchestrator.utils.twilio_send import
    send_freeform_message``), so patching the module attribute intercepts it.
    Returns the list of (body, recipient) calls."""
    from orchestrator.utils import twilio_send

    calls: list[tuple[str, str]] = []

    def _spy(body: str, recipient_phone: str) -> str:
        calls.append((body, recipient_phone))
        return "SM" + "0" * 32

    monkeypatch.setattr(twilio_send, "send_freeform_message", _spy)
    return calls


# --- (a) LAZY-START ---------------------------------------------------------


def test_lazy_start_fresh_trial_tenant_with_draft(substrate, send_spy, monkeypatch):  # type: ignore[no-untyped-def]
    """A fresh trial tenant with a seeded draft, no journey → the intercept
    LAZY-STARTS: composes the queue (from the draft, via a monkeypatched
    question-brain so NO live LLM), starts the journey, sends the first
    question, and returns {started: True, done: False}."""
    from orchestrator.onboarding import journey, question_brain
    from orchestrator.onboarding.draft_profile import write_draft

    tenant = _new_tenant(substrate.dsn, name="lazy-start trial", phase="trial")

    # A draft must exist or _compose_queue returns [] (no attributes → no questions).
    write_draft(tenant, {"category": "restaurant"}, source="gbp")

    # Monkeypatch the question-brain to a FIXED set of Question objects — NO live
    # LLM. ``_compose_queue`` imports it as
    # ``from orchestrator.onboarding.question_brain import compose_onboarding_questions``,
    # so patch the module attribute.
    fixed = [
        question_brain.Question(
            field="category",
            kind="confirm",
            prompt_en="We found you're a restaurant — is that right?",
            prompt_hi="हमें पता चला आप restaurant हैं — क्या यह सही है?",
            draft_value="restaurant",
        ),
        question_brain.Question(
            field="operating_hours",
            kind="gap",
            prompt_en="What are your operating hours?",
            prompt_hi="आपके काम के घंटे क्या हैं?",
        ),
    ]
    monkeypatch.setattr(
        question_brain,
        "compose_onboarding_questions",
        lambda *a, **k: fixed,
    )

    result = journey.maybe_handle_journey_reply(
        tenant, "hi", "SM-lazy-1", recipient="+919999000111"
    )

    assert result is not None
    assert result.get("started") is True
    assert result.get("done") is False

    # The journey is now active with the composed queue installed.
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None, "lazy-start did not create the journey row"
    assert row["status"] == "active"
    assert row["cursor"] == 0
    assert len(row["question_queue"]) == 2, "the composed queue must be installed"
    assert row["question_queue"][0]["field"] == "category"

    # The first question was SENT to the owner.
    assert len(send_spy) == 1, f"expected exactly one send; got {send_spy}"
    sent_body, sent_to = send_spy[0]
    assert sent_to == "+919999000111"
    assert "restaurant" in sent_body, (
        f"the first composed question must be sent; got {sent_body!r}"
    )


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


def test_complete_journey_returns_none(substrate, send_spy):  # type: ignore[no-untyped-def]
    """A COMPLETE journey (status != 'active') → the intercept returns None
    (fall through). The completed onboarding owner now talks to the normal brain."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="complete falls through", phase="trial")
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

    send_spy.clear()
    result = journey.maybe_handle_journey_reply(
        tenant, "another inbound", "SM-complete-fallthrough", recipient="+919999000555"
    )

    assert result is None, "a complete journey must fall through to the normal brain"
    assert send_spy == [], "nothing should be sent on a complete-journey fall-through"
