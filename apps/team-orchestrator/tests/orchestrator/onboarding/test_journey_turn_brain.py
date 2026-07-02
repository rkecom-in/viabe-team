"""VT-569 — DB-backed wiring tests for the LLM turn-brain path in ``orchestrator.onboarding.journey``.

The turn-brain composes the SAY + interprets the reply; the DETERMINISTIC layer (this journey module)
still owns the durable spine — it records the brain's proposed extractions through the EXISTING
recorders (never-assert promotion gate intact) and advances the cursor. These tests monkeypatch
``turn_brain.compose_turn`` (so no live LLM) and pin:

  - gate ON, a rejected confirm records NOTHING + leaves the field a candidate (the reply is the
    brain's, not the identical question);
  - multi-field extraction records via the existing recorders; a CONFIRMED valid taxonomy business_type
    is promoted to the canonical profile; an OFF-taxonomy business_type is NEVER promoted (recorded as
    a free answer only) — the taxonomy-coercion guard;
  - a turn-brain failure (compose_turn → None) falls back to the deterministic walker for that turn,
    including the VT-569a non-identical bare-"No" re-prompt;
  - gate OFF is the deterministic walker, byte-identical to pre-VT-569 (a confirm 'yes' promotes);
  - the completion turn uses the durable closer + fires the integration seam.

Substrate mirrors ``test_journey.py`` (migrations once, DBOS launched, tenants seeded service-role).
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
    reason="DATABASE_URL not set — VT-569 turn-brain substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
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


# --- seeding + readback (direct service-role / BYPASSRLS) ---------------------------------------


def _new_tenant(dsn: str, *, name: str, business_type: str = "services") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, business_type, "
            "whatsapp_number) VALUES (%s, 'founding', 'trial', now(), %s, %s) RETURNING id",
            (name, business_type, f"+9199{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _journey_row(dsn: str, tenant_id: UUID) -> dict[str, Any] | None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, question_queue, cursor, answers, skipped, last_message_sid "
            "FROM onboarding_journey WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    return {
        "status": row[0], "question_queue": list(row[1] or []), "cursor": row[2],
        "answers": dict(row[3] or {}), "skipped": list(row[4] or []), "last_message_sid": row[5],
    }


def _canonical_profile(dsn: str, tenant_id: UUID) -> dict[str, Any] | None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT attributes FROM l1_entities WHERE tenant_id = %s AND entity_type = 'business_profile'",
            (str(tenant_id),),
        ).fetchone()
    return dict(row[0] or {}) if row is not None else None


def _seed_draft(dsn: str, tenant_id: UUID, attributes: dict[str, Any]) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO business_profile_draft (tenant_id, attributes, provenance) "
            "VALUES (%s, %s::jsonb, '{}'::jsonb) "
            "ON CONFLICT (tenant_id) DO UPDATE SET attributes = EXCLUDED.attributes",
            (str(tenant_id), psycopg.types.json.Jsonb(attributes)),
        )


def _confirm_q(field: str, draft_value: Any) -> dict[str, Any]:
    return {"field": field, "kind": "confirm",
            "prompt_en": f"We found {field}: {draft_value} — correct?",
            "prompt_hi": f"हमें {field} मिला: {draft_value} — सही है?", "draft_value": draft_value}


def _gap_q(field: str) -> dict[str, Any]:
    return {"field": field, "kind": "gap", "prompt_en": f"Could you tell us your {field}?",
            "prompt_hi": f"क्या आप अपना {field} बता सकते हैं?", "draft_value": None}


@pytest.fixture()
def _stub_sends(monkeypatch):  # type: ignore[no-untyped-def]
    """Stub every send seam so nothing hits the wire; capture the freeform bodies for assertions."""
    from orchestrator.utils import twilio_send

    sent: list[str] = []
    monkeypatch.setattr(twilio_send, "send_freeform_message", lambda body, *a, **k: sent.append(body) or "SM0")
    monkeypatch.setattr(twilio_send, "send_interactive_message", lambda *a, **k: sent.append("<interactive>") or "SM0")
    return sent


def _enable_turn_brain(monkeypatch, fake_compose):  # type: ignore[no-untyped-def]
    from orchestrator.onboarding import turn_brain

    monkeypatch.setenv("ONBOARDING_TURN_BRAIN", "1")
    monkeypatch.setattr(turn_brain, "compose_turn", fake_compose)


# --- tests --------------------------------------------------------------------------------------


def test_no_to_confirm_records_nothing_and_reply_is_non_identical(substrate, monkeypatch, _stub_sends):
    """Gate ON: the brain reads a bare 'No' as a rejection — nothing is recorded, the confirm stays a
    candidate (cursor unmoved), and the sent reply is the brain's (NOT the identical confirm question)."""
    from orchestrator.onboarding import journey, turn_brain

    confirm_prompt = "We found business_type: services — correct?"

    def _fake(journey_state, draft_attrs, owner_message, *, locale="en", provenance=None, is_start=False):
        return turn_brain.TurnPlan(
            reply_text="No problem — what kind of business is it then?",
            buttons=(), extracted_answers={}, mark_confirmed=(), mark_rejected=("business_type",),
        )

    _enable_turn_brain(monkeypatch, _fake)
    tenant = _new_tenant(substrate.dsn, name="tb no-confirm")
    _seed_draft(substrate.dsn, tenant, {"business_type": "services"})
    journey.start_journey(tenant, [_confirm_q("business_type", "services"), _gap_q("operating_hours")])

    r = journey.maybe_handle_journey_reply(tenant, "No", "SM-tb-no", recipient="+919999000001")
    assert r is not None and r.get("turn_brain") is True and r.get("done") is False

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["cursor"] == 0, "a rejected confirm must NOT advance the cursor"
    assert row["answers"] == {}, "a rejection records nothing"
    assert _canonical_profile(substrate.dsn, tenant) is None, "nothing promoted on a rejection"
    assert _stub_sends and _stub_sends[-1] != confirm_prompt, "the reply must not be the identical question"
    assert "what kind of business" in _stub_sends[-1]


def test_multi_field_extraction_records_and_promotes_valid_confirm(substrate, monkeypatch, _stub_sends):
    """Gate ON: several fields extracted from one message are recorded via the existing recorders; a
    CONFIRMED valid taxonomy business_type is promoted to the canonical profile (the never-assert gate
    fires through confirm_draft). The cursor jumps past every resolved field."""
    from orchestrator.onboarding import journey, turn_brain

    def _fake(journey_state, draft_attrs, owner_message, *, locale="en", provenance=None, is_start=False):
        return turn_brain.TurnPlan(
            reply_text="Great, got all that!",
            buttons=(),
            extracted_answers={"business_type": "services", "operating_hours": "9-9", "city": "Pune"},
            mark_confirmed=("business_type",),
        )

    _enable_turn_brain(monkeypatch, _fake)
    tenant = _new_tenant(substrate.dsn, name="tb multi-extract")
    _seed_draft(substrate.dsn, tenant, {"business_type": "services"})
    journey.start_journey(
        tenant,
        [_confirm_q("business_type", "services"), _gap_q("operating_hours"), _gap_q("city")],
    )

    journey.maybe_handle_journey_reply(tenant, "services biz, 9-9, in Pune", "SM-tb-multi", recipient="+919999000002")

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["answers"].get("business_type") == "services"
    assert row["answers"].get("operating_hours") == "9-9"
    assert row["answers"].get("city") == "Pune"
    assert row["cursor"] == 3, "the cursor jumps past all three resolved queue fields"
    promoted = _canonical_profile(substrate.dsn, tenant)
    assert promoted is not None and promoted.get("business_type") == "services", (
        "a confirmed valid taxonomy business_type must be promoted to canonical fact"
    )
    # operating_hours/city were extracted (not confirm-kind promotions) → recorded, not promoted.
    assert "operating_hours" not in promoted


def test_offtaxonomy_business_type_is_recorded_but_never_promoted(substrate, monkeypatch, _stub_sends):
    """The taxonomy-coercion guard: a CONFIRMED business_type that is NOT a valid taxonomy key is
    recorded as a free answer but NEVER promoted to canonical fact (the LLM cannot assert garbage)."""
    from orchestrator.onboarding import journey, turn_brain

    def _fake(journey_state, draft_attrs, owner_message, *, locale="en", provenance=None, is_start=False):
        return turn_brain.TurnPlan(
            reply_text="Noted!",
            extracted_answers={"business_type": "totally-made-up-type"},
            mark_confirmed=("business_type",),
        )

    _enable_turn_brain(monkeypatch, _fake)
    tenant = _new_tenant(substrate.dsn, name="tb garbage-type")
    _seed_draft(substrate.dsn, tenant, {"business_type": "services"})
    journey.start_journey(tenant, [_confirm_q("business_type", "services")])

    journey.maybe_handle_journey_reply(tenant, "it's a xyzzy shop", "SM-tb-garbage", recipient="+919999000003")

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["answers"].get("business_type") == "totally-made-up-type", "recorded as a free answer"
    profile = _canonical_profile(substrate.dsn, tenant)
    assert profile is None or profile.get("business_type") != "totally-made-up-type", (
        "an off-taxonomy business_type must NEVER be promoted to canonical fact"
    )


def test_turn_brain_failure_falls_back_to_walker_bare_no(substrate, monkeypatch, _stub_sends):
    """compose_turn → None (LLM failure): the deterministic walker owns the turn. A bare 'No' to a
    confirm then gets the VT-569a NON-identical re-prompt (never the identical question), records
    nothing, and does not advance."""
    from orchestrator.onboarding import journey

    def _fail(*a, **k):
        return None

    _enable_turn_brain(monkeypatch, _fail)
    tenant = _new_tenant(substrate.dsn, name="tb fallback walker")
    _seed_draft(substrate.dsn, tenant, {"business_type": "services"})
    journey.start_journey(tenant, [_confirm_q("business_type", "services")])

    journey.maybe_handle_journey_reply(tenant, "no", "SM-tb-fallback", recipient="+919999000004")

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["cursor"] == 0, "the walker fallback re-presents a bare-no without advancing"
    assert row["answers"] == {}, "a bare 'no' records nothing under the walker fallback"
    assert _stub_sends, "a reply was sent"
    last = _stub_sends[-1]
    assert last != "We found business_type: services — correct?", "must not re-send the identical question"
    assert "what kind of business" in last.lower(), "the VT-569a re-prompt asks for the correct value"


def test_gate_off_is_deterministic_walker(substrate, monkeypatch, _stub_sends):
    """Gate OFF: the deterministic walker runs (byte-identical pre-VT-569) — a confirm 'yes' promotes
    the draft_value to canonical, advances the cursor, and never touches the (unpatched) turn-brain."""
    from orchestrator.onboarding import journey, turn_brain

    monkeypatch.delenv("ONBOARDING_TURN_BRAIN", raising=False)

    def _should_not_run(*a, **k):
        raise AssertionError("turn-brain must NOT run when the gate is off")

    monkeypatch.setattr(turn_brain, "compose_turn", _should_not_run)
    tenant = _new_tenant(substrate.dsn, name="tb gate-off")
    _seed_draft(substrate.dsn, tenant, {"business_type": "services"})
    journey.start_journey(tenant, [_confirm_q("business_type", "services")])

    journey.maybe_handle_journey_reply(tenant, "yes", "SM-tb-off", recipient="+919999000005")

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["cursor"] == 1, "the walker confirm-yes advances 0→1"
    assert row["answers"].get("business_type") == "services", "draft_value recorded (not 'yes')"
    promoted = _canonical_profile(substrate.dsn, tenant)
    assert promoted is not None and promoted.get("business_type") == "services"


def test_completion_uses_durable_closer_and_fires_seam(substrate, monkeypatch, _stub_sends):
    """Gate ON: when the extraction resolves the LAST queue field, the journey completes with the
    durable closer (not a dangling LLM question) and fires the integration seam."""
    from orchestrator.onboarding import journey, shopify_onboarding, turn_brain

    seam_calls: list[Any] = []
    monkeypatch.setattr(shopify_onboarding, "begin_shopify_onboarding",
                        lambda tid, rcp, *a, **k: seam_calls.append(tid))

    def _fake(journey_state, draft_attrs, owner_message, *, locale="en", provenance=None, is_start=False):
        return turn_brain.TurnPlan(reply_text="anything", extracted_answers={"operating_hours": "9-9"})

    _enable_turn_brain(monkeypatch, _fake)
    tenant = _new_tenant(substrate.dsn, name="tb completion")
    _seed_draft(substrate.dsn, tenant, {"business_type": "services"})
    journey.start_journey(tenant, [_gap_q("operating_hours")])

    r = journey.maybe_handle_journey_reply(tenant, "we're open 9-9", "SM-tb-done", recipient="+919999000006")
    assert r is not None and r.get("done") is True

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None and row["status"] == "complete"
    assert len(seam_calls) == 1, "the integration seam fires exactly once on completion"
    assert "setting up your assistant" in _stub_sends[-1], "the durable completion closer is sent"


def test_idempotent_redelivery_does_not_reinvoke_llm(substrate, monkeypatch, _stub_sends):
    """A redelivered sid (== last_message_sid) must NOT re-invoke the turn-brain nor double-apply — it
    signals already_presented so the intercept does not re-send."""
    from orchestrator.onboarding import journey, turn_brain

    calls: list[int] = []

    def _fake(journey_state, draft_attrs, owner_message, *, locale="en", provenance=None, is_start=False):
        calls.append(1)
        return turn_brain.TurnPlan(reply_text="ok", extracted_answers={"city": "Pune"}, mark_confirmed=())

    _enable_turn_brain(monkeypatch, _fake)
    tenant = _new_tenant(substrate.dsn, name="tb idempotent")
    _seed_draft(substrate.dsn, tenant, {"business_type": "services"})
    journey.start_journey(tenant, [_gap_q("city"), _gap_q("operating_hours")])

    journey.maybe_handle_journey_reply(tenant, "Pune", "SM-tb-dup", recipient="+919999000007")
    first = _journey_row(substrate.dsn, tenant)
    assert first is not None and first["answers"].get("city") == "Pune"
    assert len(calls) == 1

    # Redeliver the SAME sid — the brain must not run again, no second write.
    r = journey.maybe_handle_journey_reply(tenant, "different body", "SM-tb-dup", recipient="+919999000007")
    assert r is not None and r.get("already_presented") is True
    assert len(calls) == 1, "a redelivered sid must NOT re-invoke the turn-brain"
    after = _journey_row(substrate.dsn, tenant)
    assert after is not None and after["answers"] == first["answers"], "no second write on redelivery"


# --- pure deterministic helpers (no monkeypatch needed) -----------------------------------------


def test_reprompt_after_no_is_non_identical(substrate):
    """``_reprompt_after_no`` (VT-569a) never returns the identical confirm prompt — it asks for the
    correct value, references what was rejected, and flags re_present so the intercept sends it."""
    from orchestrator.onboarding import journey

    q = _confirm_q("business_type", "services")
    r = journey._reprompt_after_no(q)
    assert r["re_present"] is True and r["done"] is False
    assert r["reply_en"] != q["prompt_en"], "must differ from the confirm question"
    assert "services" in r["reply_en"], "references the rejected guess"
    # city + generic variants
    assert "city" in journey._reprompt_after_no(_confirm_q("city", "Mumbai"))["reply_en"].lower()
    assert "correct" in journey._reprompt_after_no(_confirm_q("about", "x"))["reply_en"].lower()


def test_confirm_button_set_detection_and_inline_cap(substrate, monkeypatch):
    """Yes/No/Skip button sets route to the interactive Content object; other button sets have no
    registered object and degrade to an inline list capped at 3."""
    from orchestrator.onboarding import journey
    from orchestrator.utils import twilio_send

    assert journey._is_confirm_button_set(["Yes", "No", "Skip"]) is True
    assert journey._is_confirm_button_set(["haan", "nahi"]) is True
    assert journey._is_confirm_button_set(["Retail", "Other"]) is False
    assert journey._is_confirm_button_set([]) is False

    sent: list[str] = []
    monkeypatch.setattr(twilio_send, "send_freeform_message", lambda body, *a, **k: sent.append(body) or "SM0")
    journey._send_turn("+919999000009", "Pick one:", ["A", "B", "C", "D"], "en")
    assert sent and sent[-1].count("/") == 2, "at most 3 inline options (2 separators)"
    assert "D" not in sent[-1], "the 4th option is dropped (cap 3)"


# ---------------------------------------------------------------------------
# VT-569 follow-up (live-drill amnesia): conversation memory (mig 162)
# ---------------------------------------------------------------------------

def test_build_prompts_includes_recent_conversation() -> None:
    """The turn brain must see what IT proposed last turn (affirmation-extraction depends on it)."""
    from orchestrator.onboarding.turn_brain import _build_prompts

    state = {
        "question_queue": [{"field": "about", "kind": "gap", "prompt_en": "Tell me about it"}],
        "cursor": 0, "answers": {}, "skipped": [],
        "recent_turns": [
            {"role": "bot", "text": "Should I use: AI-powered business intelligence?"},
            {"role": "owner", "text": "Use that"},
        ],
    }
    _, user = _build_prompts(state, {}, "Use that", locale="en", provenance=None, is_start=False)
    assert "RECENT CONVERSATION" in user
    assert "AI-powered business intelligence" in user
    assert "OWNER: Use that" in user


def test_append_recent_turns_caps_and_preserves_order(substrate) -> None:
    from orchestrator.onboarding.journey import _append_recent_turns, get_journey, start_journey

    tenant = _new_tenant(substrate.dsn, name="VT-569 memory cap")
    start_journey(tenant, [{"field": "about", "kind": "gap", "prompt_en": "x"}])
    for i in range(6):
        _append_recent_turns(
            tenant, {"role": "owner", "text": f"o{i}"}, {"role": "bot", "text": f"b{i}"}
        )
    g = get_journey(tenant)
    turns = g["recent_turns"]
    assert len(turns) == 8  # capped
    assert turns[-1]["text"] == "b5" and turns[-2]["text"] == "o5"  # newest last, order kept
    assert turns[0]["text"] == "o2"  # oldest surviving entry
