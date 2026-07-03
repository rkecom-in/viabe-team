"""VT-575 — DB-backed tests for POPULATE-FIRST onboarding (CL-2026-07-03-populate-first-onboarding).

The binding ruling: when discovery is ANCHORED to the owner's real identity (an entity-ACCEPTED GBP
listing or an owner-LINKED website), the DERIVABLE profile facts are AUTO-POPULATED — promoted to the
canonical business_profile + recorded into the journey answers so the conductor stops queueing per-field
confirms — and PRESENTED as ONE editable profile card. Per-field confirm questions for derivable facts
are FORBIDDEN (the live-drill double-ask defect: confirm the site-derived description → the NEXT turn
re-asks the same substance as ``about``). Load-bearing behaviours pinned here:

  - ``populate_profile_from_draft`` promotes only VALIDATED derivable fields (business_type gated by the
    taxonomy validator; raw category suppressed when a business_type resolves), is IDEMPOTENT (a re-run
    with no change returns {} → no re-card), REFRESHES a populate-owned field whose discovery value
    changed, and NEVER downgrades an owner-stated value (owner_stated wins on conflict);
  - populate requires an IDENTITY ANCHOR — a weaker public-guess draft stays confirm-gated;
  - MID-FLIGHT catch-up: on a reply the derivable queue entries are auto-resolved (recorded into
    answers) and the NEXT presented objective EXCLUDES them (double-ask regression) while the profile
    CARD is presented (turn brain, mocked);
  - EMPTY necessities after populate → the journey completes through the existing seam (card first);
  - EDIT-after-populate re-promotes the owner's new value to canonical.

Substrate mirrors ``test_journey.py`` (migrations once, DBOS launched, tenants seeded service-role). The
turn-brain tests monkeypatch ``turn_brain.compose_turn`` (no live LLM).
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
    reason="DATABASE_URL not set — VT-575 populate-first substrate tests skipped",
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


def _seed_draft(
    dsn: str, tenant_id: UUID, attributes: dict[str, Any], provenance: dict[str, Any] | None = None
) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO business_profile_draft (tenant_id, attributes, provenance) "
            "VALUES (%s, %s::jsonb, %s::jsonb) "
            "ON CONFLICT (tenant_id) DO UPDATE SET attributes = EXCLUDED.attributes, "
            "provenance = EXCLUDED.provenance",
            (str(tenant_id), psycopg.types.json.Jsonb(attributes),
             psycopg.types.json.Jsonb(provenance or {})),
        )


def _anchored_attrs(**fields: Any) -> dict[str, Any]:
    """Attributes carrying the ENTITY-ACCEPTED anchor (an accepted GBP listing) + the given fields."""
    return {"entity_resolution": {"decision": "accept", "confidence": 0.9}, **fields}


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


def _confirm_q(field: str, draft_value: Any) -> dict[str, Any]:
    return {"field": field, "kind": "confirm",
            "prompt_en": f"We found {field}: {draft_value} — correct?",
            "prompt_hi": f"हमें {field} मिला: {draft_value} — सही है?", "draft_value": draft_value}


def _gap_q(field: str) -> dict[str, Any]:
    return {"field": field, "kind": "gap", "prompt_en": f"Could you tell us your {field}?",
            "prompt_hi": f"क्या आप अपना {field} बता सकते हैं?", "draft_value": None}


@pytest.fixture()
def _stub_sends(monkeypatch):  # type: ignore[no-untyped-def]
    from orchestrator.utils import twilio_send

    sent: list[str] = []
    monkeypatch.setattr(twilio_send, "send_freeform_message", lambda body, *a, **k: sent.append(body) or "SM0")
    monkeypatch.setattr(twilio_send, "send_interactive_message",
                        lambda *a, **k: sent.append("<interactive>") or "SM0")
    return sent


def _enable_turn_brain(monkeypatch, fake_compose):  # type: ignore[no-untyped-def]
    from orchestrator.onboarding import turn_brain

    monkeypatch.setenv("ONBOARDING_TURN_BRAIN", "1")
    monkeypatch.setattr(turn_brain, "compose_turn", fake_compose)


# --- populate seam (pure state; no LLM) ---------------------------------------------------------


def test_populate_promotes_validated_derivable_and_suppresses_raw_category(substrate):
    """An identity-anchored draft with a VALID business_type promotes {business_type, about, city,
    website} to canonical + into answers; the raw GBP ``category`` is SUPPRESSED (VT-475) when a
    business_type resolves. The reserved ``__populated__`` sentinel records what was asserted."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="vt575 populate valid")
    _seed_draft(
        substrate.dsn, tenant,
        _anchored_attrs(
            business_name="RKeCom", business_type="services", category="Telecommunications service provider",
            about="We build e-commerce and business tooling.", city="Mumbai", website="https://rkecom.in",
        ),
    )
    journey.start_journey(tenant, [])

    populated = journey.populate_profile_from_draft(tenant)

    assert set(populated) == {"business_type", "about", "city", "website"}, populated
    assert "category" not in populated, "raw GBP category must be suppressed when business_type resolves"

    profile = _canonical_profile(substrate.dsn, tenant)
    assert profile is not None
    assert profile.get("business_type") == "services"
    assert profile.get("about") == "We build e-commerce and business tooling."
    assert profile.get("city") == "Mumbai"
    assert profile.get("website") == "https://rkecom.in"
    assert "category" not in profile, "the suppressed raw category must NOT be asserted to canonical"

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    for f in ("business_type", "about", "city", "website"):
        assert row["answers"].get(f) == populated[f]
    assert set(row["answers"].get("__populated__", {})) == {"business_type", "about", "city", "website"}


def test_populate_offtaxonomy_type_is_not_asserted_but_category_is(substrate):
    """The taxonomy gate holds under populate: an OFF-taxonomy business_type is NEVER promoted; the raw
    category is then the derivable fallback (no business_type to suppress it)."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="vt575 offtaxonomy", business_type="other")
    _seed_draft(
        substrate.dsn, tenant,
        _anchored_attrs(business_type="totally-made-up-type", category="Cafe", city="Pune"),
    )
    journey.start_journey(tenant, [])

    populated = journey.populate_profile_from_draft(tenant)

    assert "business_type" not in populated, "an off-taxonomy business_type must never be asserted"
    assert populated.get("category") == "Cafe", "category is the derivable fallback when no type resolves"
    profile = _canonical_profile(substrate.dsn, tenant)
    assert profile is not None
    assert profile.get("category") == "Cafe"
    assert "business_type" not in profile


def test_populate_is_idempotent_no_recard(substrate):
    """A second populate with no discovery change returns {} — the card-once contract (no re-present)."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="vt575 idempotent")
    _seed_draft(substrate.dsn, tenant, _anchored_attrs(business_type="services", city="Delhi"))
    journey.start_journey(tenant, [])

    first = journey.populate_profile_from_draft(tenant)
    assert first, "first populate must return the populated set"
    second = journey.populate_profile_from_draft(tenant)
    assert second == {}, "an unchanged re-populate must return {} (no re-card)"


def test_populate_owner_stated_value_is_never_downgraded(substrate):
    """owner_stated wins on conflict: a field the owner already answered is NOT overwritten by discovery
    (and is absent from the populate delta)."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="vt575 owner wins")
    _seed_draft(substrate.dsn, tenant, _anchored_attrs(business_type="services", city="Mumbai"))
    journey.start_journey(tenant, [])
    # The owner already stated their city (a real answer, not a populate sentinel).
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE onboarding_journey SET answers = %s WHERE tenant_id = %s",
            (psycopg.types.json.Jsonb({"city": "Delhi"}), str(tenant)),
        )

    populated = journey.populate_profile_from_draft(tenant)

    assert "city" not in populated, "an owner-stated city must not be downgraded to the discovered value"
    assert "business_type" in populated
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None and row["answers"].get("city") == "Delhi", "the owner's value stands"


def test_populate_refresh_updates_changed_field(substrate):
    """A populate-owned field whose DISCOVERY value later changes (a website refresh landed) is
    re-promoted on the next populate — the compact-not-frozen refresh path."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="vt575 refresh")
    _seed_draft(substrate.dsn, tenant, _anchored_attrs(business_type="services", about="Old description."))
    journey.start_journey(tenant, [])
    journey.populate_profile_from_draft(tenant)

    # A refresh lands a better description.
    _seed_draft(substrate.dsn, tenant, _anchored_attrs(business_type="services", about="New, richer description."))
    changed = journey.populate_profile_from_draft(tenant)

    assert changed.get("about") == "New, richer description.", "a changed discovery value must refresh"
    assert "business_type" not in changed, "an unchanged field must not re-fire"
    profile = _canonical_profile(substrate.dsn, tenant)
    assert profile is not None and profile.get("about") == "New, richer description."


def test_populate_requires_identity_anchor(substrate):
    """A weaker, NON-anchored public-guess draft is NOT auto-populated — it stays confirm-gated."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="vt575 no anchor")
    # No entity_resolution accept, and the website provenance is a plain web guess (not owner_stated).
    _seed_draft(
        substrate.dsn, tenant,
        {"business_type": "services", "city": "Mumbai", "website": "https://guess.example"},
        provenance={"website": {"source": "web"}},
    )
    journey.start_journey(tenant, [])

    populated = journey.populate_profile_from_draft(tenant)

    assert populated == {}, "a non-anchored draft must not auto-populate"
    assert _canonical_profile(substrate.dsn, tenant) is None, "nothing promoted without an identity anchor"


def test_populate_anchors_on_owner_linked_website(substrate):
    """The owner-LINKED website provenance (``owner_stated``) is an identity anchor on its own."""
    from orchestrator.onboarding import journey

    tenant = _new_tenant(substrate.dsn, name="vt575 website anchor")
    _seed_draft(
        substrate.dsn, tenant,
        {"business_type": "services", "website": "https://rkecom.in"},
        provenance={"website": {"source": "owner_stated"}},
    )
    journey.start_journey(tenant, [])

    populated = journey.populate_profile_from_draft(tenant)
    assert "business_type" in populated and populated.get("website") == "https://rkecom.in"


# --- turn-brain path: catch-up card + double-ask regression + completion + edits -----------------


def test_midflight_catchup_presents_card_and_suppresses_double_ask(substrate, monkeypatch, _stub_sends):
    """MID-FLIGHT: a derivable queue entry (a business_type confirm) is auto-resolved into answers; the
    NEXT objective the brain composes EXCLUDES it (double-ask regression) while the profile CARD is
    presented. The captured ``profile_card`` carries the populated facts; the cursor jumps past the
    now-answered confirm."""
    from orchestrator.onboarding import journey, turn_brain

    captured: dict[str, Any] = {}

    def _fake(journey_state, draft_attrs, owner_message, *, locale="en", provenance=None,
              is_start=False, tenant_id=None, profile_card=None):
        captured["profile_card"] = profile_card
        captured["journey_state"] = journey_state
        return turn_brain.TurnPlan(reply_text="Here's your profile — tell me anything to change.",
                                   buttons=("Looks good",))

    _enable_turn_brain(monkeypatch, _fake)
    tenant = _new_tenant(substrate.dsn, name="vt575 catchup card")
    _seed_draft(
        substrate.dsn, tenant,
        _anchored_attrs(business_name="RKeCom", business_type="services",
                        about="We build e-commerce tooling.", city="Mumbai"),
    )
    # A pre-populate queue that still holds the derivable business_type confirm + a genuine necessity.
    journey.start_journey(tenant, [_confirm_q("business_type", "services"), _gap_q("operating_hours")])

    r = journey.maybe_handle_journey_reply(tenant, "hi", "SM-vt575-catchup", recipient="+919999000101")
    assert r is not None and r.get("turn_brain") is True

    # The card was presented with the populated derivable facts.
    assert captured["profile_card"], "the profile card must be presented on the catch-up turn"
    assert "business_type" in captured["profile_card"] and "about" in captured["profile_card"]

    # DOUBLE-ASK REGRESSION: the objective the brain composed against no longer contains business_type
    # or about (they are answered) — only the genuine necessity remains.
    _, objective = turn_brain._objective_from_state(captured["journey_state"])
    fields = {q.get("field") for q in objective}
    assert "business_type" not in fields and "about" not in fields, (
        "a populated derivable field must NEVER be re-asked (the double-ask defect)"
    )
    assert "operating_hours" in fields, "the genuine necessity is still outstanding"

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["answers"].get("business_type") == "services"
    assert row["cursor"] == 1, "the cursor jumps past the auto-resolved business_type confirm"
    profile = _canonical_profile(substrate.dsn, tenant)
    assert profile is not None and profile.get("business_type") == "services", "populated → canonical"


def test_empty_necessities_completes_after_card(substrate, monkeypatch, _stub_sends):
    """When populate resolves everything and NO necessity remains, the start turn presents the card and
    the journey COMPLETES through the existing seam (card first)."""
    from orchestrator.onboarding import journey, question_brain, shopify_onboarding, turn_brain

    # No un-derivable gaps for this business (force the gap source empty so the queue is necessities-only).
    monkeypatch.setattr(question_brain, "_llm_compose_gaps", lambda *a, **k: [])

    seam_calls: list[Any] = []
    monkeypatch.setattr(shopify_onboarding, "begin_shopify_onboarding",
                        lambda tid, rcp, *a, **k: seam_calls.append(tid))

    def _fake(journey_state, draft_attrs, owner_message, *, locale="en", provenance=None,
              is_start=False, tenant_id=None, profile_card=None):
        assert profile_card, "the completion turn must still carry the profile card"
        return turn_brain.TurnPlan(reply_text="Here's your profile — all set. Tell me anything to change.")

    _enable_turn_brain(monkeypatch, _fake)
    tenant = _new_tenant(substrate.dsn, name="vt575 empty necessities")
    _seed_draft(
        substrate.dsn, tenant,
        _anchored_attrs(business_name="RKeCom", business_type="services",
                        about="We build tooling.", city="Mumbai", website="https://rkecom.in"),
    )
    journey.start_journey(tenant, [])  # pending lazy-start

    r = journey.maybe_handle_journey_reply(tenant, "hello", "SM-vt575-empty", recipient="+919999000102")
    assert r is not None and r.get("done") is True

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None and row["status"] == "complete", "empty necessities → journey completes"
    assert len(seam_calls) == 1, "the integration seam fires on completion"
    assert _stub_sends and "profile" in _stub_sends[-1].lower(), "the card was sent as the closing message"


def test_edit_after_populate_repromotes_to_canonical(substrate, monkeypatch, _stub_sends):
    """EDITS FOREVER: after a field is populated, an owner message changing it flows extraction →
    recorders → RE-PROMOTION, overwriting the canonical value."""
    from orchestrator.onboarding import journey, turn_brain

    plans: list[Any] = []

    def _fake(journey_state, draft_attrs, owner_message, *, locale="en", provenance=None,
              is_start=False, tenant_id=None, profile_card=None):
        return plans.pop(0)

    _enable_turn_brain(monkeypatch, _fake)
    tenant = _new_tenant(substrate.dsn, name="vt575 edit repromote")
    _seed_draft(substrate.dsn, tenant, _anchored_attrs(business_type="services", city="Mumbai"))
    journey.start_journey(tenant, [_gap_q("operating_hours")])

    # Turn 1: populate promotes the discovered city=Mumbai; the brain just acks.
    plans.append(turn_brain.TurnPlan(reply_text="Here's your profile."))
    journey.maybe_handle_journey_reply(tenant, "hi", "SM-vt575-edit-1", recipient="+919999000103")
    assert (_canonical_profile(substrate.dsn, tenant) or {}).get("city") == "Mumbai"

    # Turn 2: the owner corrects their city → extracted_answers re-promotes it to canonical.
    plans.append(turn_brain.TurnPlan(reply_text="Updated to Delhi!", extracted_answers={"city": "Delhi"}))
    journey.maybe_handle_journey_reply(tenant, "actually we're in Delhi", "SM-vt575-edit-2",
                                       recipient="+919999000103")

    profile = _canonical_profile(substrate.dsn, tenant)
    assert profile is not None and profile.get("city") == "Delhi", "the owner's edit must re-promote"
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None and row["answers"].get("city") == "Delhi"


# --- pure prompt-assembly unit (no substrate) ---------------------------------------------------


def test_build_prompts_renders_card_and_strips_sentinel() -> None:
    """The turn-brain prompt carries the populate-first reframe + the card block, and the reserved
    ``__populated__`` sentinel is stripped from ALREADY COLLECTED (never leaked to the LLM)."""
    from orchestrator.onboarding.turn_brain import _build_prompts

    state = {
        "question_queue": [{"field": "operating_hours", "kind": "gap", "prompt_en": "Hours?"}],
        "cursor": 0,
        "answers": {"business_type": "services", "__populated__": {"business_type": "services"}},
        "skipped": [], "recent_turns": [],
    }
    card = {"business_type": "services", "city": "Mumbai", "website": "https://rkecom.in"}
    system, user = _build_prompts(
        state, {"business_name": "RKeCom"}, "hi", locale="en", provenance=None,
        is_start=True, profile_card=card,
    )
    assert "POPULATE-FIRST" in system, "the system prompt must carry the populate-first reframe"
    assert "PROFILE JUST POPULATED" in user and "what you do: services" in user
    assert "__populated__" not in user, "the reserved sentinel must never leak into the prompt"
    # business_type is a real collected answer and still appears (only the __-prefixed key is stripped).
    assert "services" in user
