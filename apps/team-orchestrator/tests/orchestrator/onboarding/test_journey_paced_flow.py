"""VT-576 / CL-2026-07-03 — the PACED post-profile flow in ``orchestrator.onboarding.journey``.

Replaces the profile-confirm 4-message BURST (card + Shopify pitch + summary + a data-less month
plan) with one beat per owner message, tracked by the ``answers['__flow__']`` sentinel on the
COMPLETED journey row:

  profile_previewed  → (owner acks) →  readiness ask         (__flow__ = ready_asked)
  ready_asked        → (yes)        →  ONE integration offer  (__flow__ = integration:<name>)
                     → (later/no)   →  honest summary-only     (__flow__ = deferred, resumable)
  integration:<name> → (data lands) →  business summary + month plan kickoff (__flow__ = plan_kicked)

Substrate mirrors ``test_journey_turn_brain.py`` (migrations once, DBOS launched, tenants seeded
service-role). Sends are stubbed (no wire); ``_kickoff_business_plan`` is spied so the plan trigger is
asserted without running the DBOS generator.
"""

from __future__ import annotations

import json
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
    reason="DATABASE_URL not set — VT-576 paced-flow substrate tests skipped",
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


def _seed_completed_journey(dsn: str, tenant_id: UUID, flow: str, *, last_sid: str | None = None) -> None:
    """A COMPLETED journey already in a given post-profile flow beat."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO onboarding_journey "
            "(tenant_id, status, question_queue, cursor, answers, skipped, last_message_sid, completed_at) "
            "VALUES (%s, 'complete', '[]'::jsonb, 0, %s::jsonb, '[]'::jsonb, %s, now())",
            (str(tenant_id), json.dumps({"__flow__": flow}), last_sid),
        )


def _seed_integration_confirmed(dsn: str, tenant_id: UUID, connector: str = "shopify") -> None:
    """Simulate a connector whose data has LANDED (phase_5_confirmed — rows ingested)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_integration_state (tenant_id, phase, current_connector_id) "
            "VALUES (%s, 'phase_5_confirmed', %s) "
            "ON CONFLICT (tenant_id) DO UPDATE SET phase = EXCLUDED.phase, "
            "current_connector_id = EXCLUDED.current_connector_id",
            (str(tenant_id), connector),
        )


def _seed_integration_auth(dsn: str, tenant_id: UUID, connector: str = "shopify") -> None:
    """A connector mid-onboarding (phase_2_auth) — OAuth not yet ingested (data NOT landed). A real
    phase_2_auth ALWAYS carries a live ``pending_owner_input`` (the oauth_completion the resume gate
    waits on); VT-583's has_live_resume keys off exactly that, so the seed writes it."""
    pending = json.dumps(
        {
            "awaiting": "oauth_completion",
            "connector_id": connector,
            "walkthrough_url": "https://vt576.myshopify.com/admin/oauth/authorize",
        }
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_integration_state "
            "(tenant_id, phase, current_connector_id, pending_owner_input) "
            "VALUES (%s, 'phase_2_auth', %s, %s::jsonb) "
            "ON CONFLICT (tenant_id) DO UPDATE SET phase = EXCLUDED.phase, "
            "current_connector_id = EXCLUDED.current_connector_id, "
            "pending_owner_input = EXCLUDED.pending_owner_input",
            (str(tenant_id), connector, pending),
        )


def _flow(dsn: str, tenant_id: UUID) -> str | None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT answers FROM onboarding_journey WHERE tenant_id = %s", (str(tenant_id),)
        ).fetchone()
    return (dict(row[0] or {}).get("__flow__")) if row is not None else None


@pytest.fixture()
def _stub_sends(monkeypatch):  # type: ignore[no-untyped-def]
    from orchestrator.utils import twilio_send

    sent: list[str] = []
    monkeypatch.setattr(twilio_send, "send_freeform_message", lambda body, *a, **k: sent.append(body) or "SM0")
    monkeypatch.setattr(twilio_send, "send_interactive_message", lambda *a, **k: sent.append("<interactive>") or "SM0")
    return sent


@pytest.fixture()
def _mock_flow_intent(monkeypatch):  # type: ignore[no-untyped-def]
    """Drive turn_brain.classify_flow_intent deterministically (no live LLM). Set ``holder['intent']``
    per test; None simulates a classifier failure (→ the caller keeps today's deterministic behavior)."""
    from orchestrator.onboarding import turn_brain

    holder: dict[str, str | None] = {"intent": None}
    monkeypatch.setattr(turn_brain, "_llm_classify_flow_intent", lambda _b: holder["intent"])
    return holder


# --- VT-583 A: paced-flow intent — floor short-circuits; the ambiguous middle asks the classifier ---


def test_readiness_ambiguous_classified_decline_defers(substrate, _stub_sends, _mock_flow_intent):
    """An ambiguous readiness reply the keyword floor can't call → the classifier; a 'decline' verdict
    defers (honest summary-only), exactly as a floor 'later' would."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    _mock_flow_intent["intent"] = "decline"
    tenant = _new_tenant(substrate.dsn, name="flow amb decline")
    _seed_completed_journey(substrate.dsn, tenant, "ready_asked")

    r = maybe_handle_journey_reply(tenant, "I might think about it", "SID-amb-1", "+919999003101")
    assert r is not None and r.get("routed") == "flow_deferred"
    assert _flow(substrate.dsn, tenant) == "deferred"


def test_readiness_ambiguous_classified_affirm_offers(substrate, _stub_sends, _mock_flow_intent):
    """An ambiguous readiness reply classified 'affirm' → proceed with the easiest integration."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    _mock_flow_intent["intent"] = "affirm"
    tenant = _new_tenant(substrate.dsn, name="flow amb affirm")
    _seed_completed_journey(substrate.dsn, tenant, "ready_asked")

    r = maybe_handle_journey_reply(tenant, "matlab dekhna padega thoda", "SID-amb-2", "+919999003102")
    assert r is not None and r.get("routed") == "flow_offer_integration"
    assert _flow(substrate.dsn, tenant) == "integration:shopify"


def test_readiness_ambiguous_classifier_failure_keeps_today_behavior(substrate, _stub_sends, _mock_flow_intent):
    """Classifier failure (None) on an ambiguous readiness reply → today's non-decline behavior = offer.
    Fail-soft = today's behavior, never a stall or a silent drop."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    _mock_flow_intent["intent"] = None  # classifier unavailable / errored
    tenant = _new_tenant(substrate.dsn, name="flow amb failsoft")
    _seed_completed_journey(substrate.dsn, tenant, "ready_asked")

    r = maybe_handle_journey_reply(tenant, "kuch bhi karlo", "SID-amb-3", "+919999003103")
    assert r is not None and r.get("routed") == "flow_offer_integration"


def test_readiness_floor_still_short_circuits_without_classifier(substrate, _stub_sends, monkeypatch):
    """An UNAMBIGUOUS floor hit ('later') must NOT call the classifier — the deterministic floor wins."""
    from orchestrator.onboarding import journey, turn_brain

    called = {"n": 0}

    def _boom(_b):
        called["n"] += 1
        raise AssertionError("classifier must not be called on an unambiguous floor hit")

    monkeypatch.setattr(turn_brain, "_llm_classify_flow_intent", _boom)
    tenant = _new_tenant(substrate.dsn, name="flow floor short")
    _seed_completed_journey(substrate.dsn, tenant, "ready_asked")

    r = journey.maybe_handle_journey_reply(tenant, "later", "SID-floor-1", "+919999003104")
    assert r is not None and r.get("routed") == "flow_deferred"
    assert called["n"] == 0


def test_deferred_ambiguous_connect_reengages(substrate, _stub_sends, _mock_flow_intent):
    """A deferred flow re-engages on a classifier 'connect' verdict the keyword floor missed."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    _mock_flow_intent["intent"] = "connect"
    tenant = _new_tenant(substrate.dsn, name="flow defer amb connect")
    _seed_completed_journey(substrate.dsn, tenant, "deferred")

    r = maybe_handle_journey_reply(tenant, "acha let us proceed with that now", "SID-def-amb-1", "+919999003105")
    assert r is not None and r.get("routed") == "flow_offer_integration"


def test_deferred_ambiguous_other_falls_through(substrate, _stub_sends, _mock_flow_intent):
    """A deferred flow with a classifier 'other' verdict falls through (None) — today's behavior; the
    brain owns ordinary chat, the flow never hijacks it."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    _mock_flow_intent["intent"] = "other"
    tenant = _new_tenant(substrate.dsn, name="flow defer amb other")
    _seed_completed_journey(substrate.dsn, tenant, "deferred")

    r = maybe_handle_journey_reply(tenant, "do you have festive offers", "SID-def-amb-2", "+919999003106")
    assert r is None
    assert _flow(substrate.dsn, tenant) == "deferred"


# --- VT-583 D2: integration-in-flight ORPHAN never falls to the cold brain silently ----------------


def test_integration_orphan_reoffers_instead_of_silent_brain(substrate, _stub_sends):
    """flow=integration:<x>, data NOT landed, and NO live connector resume step to consume the message
    (live-run-23 orphan). The beat must RE-OFFER (guaranteed reply), never return None to the cold
    brain. We seed the integration flow with NO tenant_integration_state row → has_live_resume False."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply
    from orchestrator.onboarding.shopify_onboarding import read_integration_state

    tenant = _new_tenant(substrate.dsn, name="flow integ orphan")
    _seed_completed_journey(substrate.dsn, tenant, "integration:shopify")
    assert read_integration_state(tenant) is None  # no live resume step exists

    r = maybe_handle_journey_reply(tenant, "sorry got busy earlier", "SID-orphan-1", "+919999003107")
    assert r is not None and r.get("routed") == "flow_offer_integration", (
        "an integration orphan must re-offer, never fall silently to the brain"
    )
    assert _stub_sends, "the orphan re-offer must send a reply (no silent drop)"


def test_integration_live_resume_defers_to_gate(substrate, _stub_sends):
    """flow=integration:shopify with a LIVE oauth pending → the journey beat returns None so the
    downstream shopify resume gate drives the connect step (no double-handling, no premature re-offer)."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    tenant = _new_tenant(substrate.dsn, name="flow integ live")
    _seed_completed_journey(substrate.dsn, tenant, "integration:shopify")
    _seed_integration_auth(substrate.dsn, tenant, "shopify")  # live oauth_completion pending

    r = maybe_handle_journey_reply(tenant, "some message", "SID-live-1", "+919999003108")
    assert r is None, "a live resume step must let the downstream gate handle the inbound"


# --- beat (b): ack → readiness ask -------------------------------------------------------------


def test_ack_after_card_leads_with_team_intro(substrate, _stub_sends):
    """VT-698 (Fazal: the Manager is HIRED — the owner can't be left clueless): after the profile
    card, the owner's next message = an ack → the how-your-team-works INTRO leads. It never
    steamrolls into readiness or an integration."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    tenant = _new_tenant(substrate.dsn, name="flow ack")
    _seed_completed_journey(substrate.dsn, tenant, "profile_previewed")

    r = maybe_handle_journey_reply(tenant, "ok", "SID-ack-1", "+919999003001")
    assert r is not None and r.get("routed") == "flow_team_intro"
    assert _flow(substrate.dsn, tenant) == "team_intro"
    # Button beats ride the interactive object (the stub records a marker); the copy itself
    # is pinned from the module constant.
    assert _stub_sends and _stub_sends[-1] == "<interactive>"
    from orchestrator.onboarding.journey import _TEAM_INTRO
    assert "how your Viabe Team works" in _TEAM_INTRO["en"]
    # No integration onboarding started yet — just the intro.
    from orchestrator.onboarding.shopify_onboarding import read_integration_state
    assert read_integration_state(tenant) is None


def test_intro_affirm_presents_trial_terms(substrate, _stub_sends):
    """VT-698 beat (a3): an affirm on the intro → the agent + trial terms, stated PLAINLY (free
    1-month per agent, start anytime, paid after, continue only if valuable, never charged
    without an explicit go-ahead)."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    tenant = _new_tenant(substrate.dsn, name="flow intro affirm")
    _seed_completed_journey(substrate.dsn, tenant, "team_intro")

    r = maybe_handle_journey_reply(tenant, "Yes, show me", "SID-intro-1", "+919999003011")
    assert r is not None and r.get("routed") == "flow_agent_trial"
    assert _flow(substrate.dsn, tenant) == "agent_trial"
    assert _stub_sends and _stub_sends[-1] == "<interactive>"
    from orchestrator.onboarding.journey import _AGENT_TRIAL
    terms = _AGENT_TRIAL["en"]
    assert "1-MONTH TRIAL" in terms and "paid" in terms
    assert "without your explicit go-ahead" in terms


def test_intro_later_defers_resumably(substrate, _stub_sends):
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    tenant = _new_tenant(substrate.dsn, name="flow intro later")
    _seed_completed_journey(substrate.dsn, tenant, "team_intro")

    r = maybe_handle_journey_reply(tenant, "later", "SID-intro-2", "+919999003012")
    assert r is not None and _flow(substrate.dsn, tenant) == "deferred"


def test_trial_ack_bridges_into_integration_offer(substrate, _stub_sends):
    """VT-698: trial terms acknowledged → the EXISTING one-at-a-time connection offer (unchanged
    onward machinery — shopify first, state written for the resume gate)."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply
    from orchestrator.onboarding.shopify_onboarding import read_integration_state

    tenant = _new_tenant(substrate.dsn, name="flow trial ack")
    _seed_completed_journey(substrate.dsn, tenant, "agent_trial")

    r = maybe_handle_journey_reply(tenant, "Connect my data", "SID-trial-1", "+919999003013")
    assert r is not None and r.get("routed") == "flow_offer_integration"
    assert _flow(substrate.dsn, tenant) == "integration:shopify"
    state = read_integration_state(tenant)
    assert state is not None and state["current_connector_id"] == "shopify"


def test_trial_later_defers_resumably(substrate, _stub_sends):
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    tenant = _new_tenant(substrate.dsn, name="flow trial later")
    _seed_completed_journey(substrate.dsn, tenant, "agent_trial")

    r = maybe_handle_journey_reply(tenant, "later", "SID-trial-2", "+919999003014")
    assert r is not None and _flow(substrate.dsn, tenant) == "deferred"


# --- beat (c): yes → ONE integration, easiest-first, with instructions --------------------------


def test_yes_offers_single_best_integration_with_instructions(substrate, _stub_sends):
    """A yes to the readiness ask → ONE integration (Shopify, easiest for a data-less tenant), with the
    registry 'why' + plain instructions, and the Shopify onboarding state is written for the resume gate."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply
    from orchestrator.onboarding.shopify_onboarding import read_integration_state

    tenant = _new_tenant(substrate.dsn, name="flow yes")
    _seed_completed_journey(substrate.dsn, tenant, "ready_asked")

    r = maybe_handle_journey_reply(tenant, "yes please", "SID-yes-1", "+919999003002")
    assert r is not None and r.get("routed") == "flow_offer_integration"
    assert r.get("integration") == "shopify"
    assert _flow(substrate.dsn, tenant) == "integration:shopify"
    # The offer carried the registry instructions (the "where to find it" copy), not a bare pitch.
    assert _stub_sends and "myshopify.com" in _stub_sends[-1]
    # The Shopify onboarding state is written so the downstream resume gate takes the next reply.
    state = read_integration_state(tenant)
    assert state is not None and state["phase"] == "phase_1_discovery"
    assert state["current_connector_id"] == "shopify"


def test_only_one_message_per_beat_no_burst(substrate, _stub_sends):
    """Each beat sends exactly ONE message — the anti-burst contract (the drill dumped four at once)."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    tenant = _new_tenant(substrate.dsn, name="flow one-per-beat")
    _seed_completed_journey(substrate.dsn, tenant, "ready_asked")

    maybe_handle_journey_reply(tenant, "sure", "SID-1msg-1", "+919999003003")
    assert len(_stub_sends) == 1, f"a beat must send exactly one message; got {_stub_sends}"


# --- beat: defer → honest summary-only, resumable ----------------------------------------------


def test_defer_offers_honest_summary_only_and_is_resumable(substrate, _stub_sends):
    """'later' → the journey stays complete, an HONEST message says the month plan needs data (no
    hollow plan), and a LATER 'connect' resumes the flow into the integration offer."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply
    from orchestrator.onboarding.shopify_onboarding import read_integration_state

    tenant = _new_tenant(substrate.dsn, name="flow defer")
    _seed_completed_journey(substrate.dsn, tenant, "ready_asked")

    r = maybe_handle_journey_reply(tenant, "later", "SID-defer-1", "+919999003004")
    assert r is not None and r.get("routed") == "flow_deferred"
    assert _flow(substrate.dsn, tenant) == "deferred"
    # The honest 'what's missing' line comes from the registry plan_blocked_reason (single source of
    # truth): it names the missing data + the connect options, and never presents a hollow plan.
    last = _stub_sends[-1].lower()
    assert "sales history" in last and "connect" in last
    assert read_integration_state(tenant) is None, "declining connects nothing"

    # Resumable: a clear connect-intent message later re-engages the integration offer.
    r2 = maybe_handle_journey_reply(tenant, "ok connect it now", "SID-defer-2", "+919999003004")
    assert r2 is not None and r2.get("routed") == "flow_offer_integration"
    assert _flow(substrate.dsn, tenant) == "integration:shopify"


def test_deferred_unrelated_message_falls_through(substrate, _stub_sends):
    """While deferred, a non-connect message falls through (returns None) so the normal brain handles
    ordinary chat — the flow does not hijack every message after a defer."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    tenant = _new_tenant(substrate.dsn, name="flow defer passthrough")
    _seed_completed_journey(substrate.dsn, tenant, "deferred")

    r = maybe_handle_journey_reply(tenant, "what are your fees?", "SID-defer-pt", "+919999003005")
    assert r is None, "a non-connect message while deferred must fall through to the normal pipeline"
    assert _flow(substrate.dsn, tenant) == "deferred", "flow state unchanged"


# --- beat (d): business summary + month plan fire ONLY after data lands --------------------------


def test_plan_fires_only_after_data_lands(substrate, monkeypatch, _stub_sends):
    """The summary + month plan kickoff fires ONLY once the first data-supplying integration has landed
    (readiness(sales_recovery).can_plan) — never at completion. We spy _kickoff_business_plan."""
    from orchestrator.onboarding import journey

    kicks: list[Any] = []
    monkeypatch.setattr(journey, "_kickoff_business_plan", lambda tid: kicks.append(tid))

    tenant = _new_tenant(substrate.dsn, name="flow plan-after-data")
    _seed_completed_journey(substrate.dsn, tenant, "integration:shopify")
    _seed_integration_confirmed(substrate.dsn, tenant, "shopify")  # data LANDED

    r = journey.maybe_handle_journey_reply(tenant, "what's next?", "SID-plan-1", "+919999003006")
    assert r is not None and r.get("routed") == "flow_plan_kicked"
    assert len(kicks) == 1, "the business summary + month plan fires exactly once after data lands"
    assert _flow(substrate.dsn, tenant) == "plan_kicked"


def test_plan_does_not_fire_before_data_lands(substrate, monkeypatch, _stub_sends):
    """While an integration is mid-onboarding (OAuth pending, NOT ingested), the plan must NOT fire and
    the journey gate falls through (None) so the downstream Shopify resume gate drives the connect step."""
    from orchestrator.onboarding import journey

    kicks: list[Any] = []
    monkeypatch.setattr(journey, "_kickoff_business_plan", lambda tid: kicks.append(tid))

    tenant = _new_tenant(substrate.dsn, name="flow plan-before-data")
    _seed_completed_journey(substrate.dsn, tenant, "integration:shopify")
    _seed_integration_auth(substrate.dsn, tenant, "shopify")  # OAuth pending, data NOT landed

    r = journey.maybe_handle_journey_reply(tenant, "vt576.myshopify.com", "SID-plan-2", "+919999003007")
    assert r is None, "data not landed → fall through to the integration resume gate"
    assert len(kicks) == 0, "no plan before data lands"
    assert _flow(substrate.dsn, tenant) == "integration:shopify", "flow unchanged"


def test_plan_kicked_terminal_falls_through(substrate, _stub_sends):
    """After the plan is kicked (plan_kicked), the flow is terminal — subsequent messages fall through
    to the normal pipeline (the tenant is in normal operation now)."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    tenant = _new_tenant(substrate.dsn, name="flow terminal")
    _seed_completed_journey(substrate.dsn, tenant, "plan_kicked")

    r = maybe_handle_journey_reply(tenant, "hello again", "SID-term-1", "+919999003008")
    assert r is None


# --- idempotency ---------------------------------------------------------------------------------


def test_redelivered_flow_message_does_not_redrive(substrate, _stub_sends):
    """A redelivered inbound (same sid == last_message_sid) must NOT re-drive the beat nor re-send."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    tenant = _new_tenant(substrate.dsn, name="flow idempotent", )
    _seed_completed_journey(substrate.dsn, tenant, "profile_previewed", last_sid="SID-dup-1")

    r = maybe_handle_journey_reply(tenant, "ok", "SID-dup-1", "+919999003009")
    assert r is not None and r.get("already_presented") is True
    assert _flow(substrate.dsn, tenant) == "profile_previewed", "state must not advance on redelivery"
    assert not _stub_sends, "a redelivery re-sends nothing"


# ---------------------------------------------------------------------------
# Live-drill defect: the owner had ALREADY sent the store address (consumed as
# an ack on another beat) and the Shopify offer asked them to retype it.
# Record-and-move-on: the offer picks it up from the conversation window.
# ---------------------------------------------------------------------------

def test_recent_shop_domain_found_and_normalized(substrate) -> None:
    from orchestrator.onboarding.journey import (
        _append_recent_turns, _recent_shop_domain, start_journey,
    )

    tenant = _new_tenant(substrate.dsn, name="pickup-found")
    start_journey(tenant, [{"field": "about", "kind": "gap", "prompt_en": "x"}])
    _append_recent_turns(
        tenant,
        {"role": "owner", "text": "KK4XVA-DI.myshopify.com"},
        {"role": "bot", "text": "Want me to set up your data connections now?"},
    )
    assert _recent_shop_domain(tenant) == "kk4xva-di.myshopify.com"


def test_recent_shop_domain_ignores_bot_lines_and_absence(substrate) -> None:
    from orchestrator.onboarding.journey import (
        _append_recent_turns, _recent_shop_domain, start_journey,
    )

    tenant = _new_tenant(substrate.dsn, name="pickup-absent")
    start_journey(tenant, [{"field": "about", "kind": "gap", "prompt_en": "x"}])
    _append_recent_turns(
        tenant,
        {"role": "bot", "text": "It should look like yourstore.myshopify.com"},
        {"role": "owner", "text": "Lets do it now"},
    )
    assert _recent_shop_domain(tenant) is None  # the bot's example must never count


# ---------------------------------------------------------------------------
# VT-583 ADDENDUM (CL-2026-07-03): unify context reads onto conversation_log so a turn dropped by the
# journey path — but captured at the runner seam (D2.2) — is STILL retrievable. This is the run-23
# reproduction: the store URL must be found even when journey.recent_turns never got it. The two fixes
# (D2.2 record-early + read-unified) are tested together; that is the invariant that kills the
# 3×-store-link re-ask.
# ---------------------------------------------------------------------------


def test_recent_shop_domain_from_unified_log_when_journey_dropped_it(substrate) -> None:
    """Run-23: the owner sent the store URL on a path that never appended to journey.recent_turns, but
    the runner seam recorded it in the lifetime conversation_log. _recent_shop_domain MUST still find it
    (reads conversation_log first) — so the owner is never asked to retype it."""
    from orchestrator.conversation_log import record_turn
    from orchestrator.onboarding.journey import _recent_shop_domain

    tenant = _new_tenant(substrate.dsn, name="unified-pickup")
    # A completed journey whose recent_turns is EMPTY (the journey path dropped the turn)…
    _seed_completed_journey(substrate.dsn, tenant, "integration:shopify")
    # …but the runner seam captured the owner's store URL in the unified lifetime log.
    record_turn(tenant, "owner", "here it is: MyStore-01.myshopify.com", message_sid="SID-run23", surface="manager")

    assert _recent_shop_domain(tenant) == "mystore-01.myshopify.com"


def test_recent_owner_texts_helper_is_owner_only_newest_first(substrate) -> None:
    """The shared helper returns OWNER texts only, newest-first — the single substrate every
    'did the owner already tell us X' scan reads."""
    from orchestrator.conversation_log import record_turn, recent_owner_texts

    tenant = _new_tenant(substrate.dsn, name="unified-helper")
    record_turn(tenant, "owner", "first owner msg", surface="manager")
    record_turn(tenant, "assistant", "a bot reply in between", surface="manager")
    record_turn(tenant, "owner", "second owner msg", surface="manager")

    texts = recent_owner_texts(tenant)
    assert "a bot reply in between" not in texts  # assistant turns excluded
    assert texts[0] == "second owner msg"          # newest-first
    assert "first owner msg" in texts


# --- VT-584: paced-flow SENDS enter the lifetime conversation_log -----------------------------------


def _assistant_log_texts(dsn: str, tenant_id: UUID) -> list[str]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT text FROM conversation_log WHERE tenant_id = %s AND role = 'assistant' "
            "AND surface = 'journey' ORDER BY created_at",
            (str(tenant_id),),
        ).fetchall()
    return [str(r[0]) for r in rows]


def test_flow_beat_reply_recorded_to_conversation_log(substrate, _stub_sends, _mock_flow_intent):
    """VT-584: a paced-flow beat reply reached the owner's phone but (pre-fix) never hit
    conversation_log — so the 24h manager window and the harness both lost it. The defer beat must now
    persist its assistant line to the lifetime log (surface='journey'), closing the substrate gap."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    _mock_flow_intent["intent"] = "decline"
    tenant = _new_tenant(substrate.dsn, name="vt584 flow-log")
    _seed_completed_journey(substrate.dsn, tenant, "ready_asked")

    r = maybe_handle_journey_reply(tenant, "not right now", "SID-vt584-1", "+919999005841")
    assert r is not None and r.get("routed") == "flow_deferred"

    logged = _assistant_log_texts(substrate.dsn, tenant)
    assert logged, "flow-beat reply must be recorded to conversation_log (24h window + harness substrate)"


# --- VT-586: the DETERMINISTIC walker _send threads tenant_id into the record choke ----------------


def test_walker_send_threads_tenant_id_to_conversation_log(monkeypatch):
    """VT-586: journey._send (the deterministic walker/opener path — used when the turn-brain is off or
    errors) must pass tenant_id + surface='journey' into send_freeform_message, so its reply records to
    the lifetime conversation_log. Before VT-586 the walker reply reached the owner but not the log —
    re-fragmenting the 24h window (the disease VT-584 fixed only for the paced-flow beats)."""
    from orchestrator.onboarding import journey
    from orchestrator.utils import twilio_send

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        twilio_send, "send_freeform_message",
        lambda body, recipient, **kw: captured.update({"body": body, **kw}) or "SM0",
    )
    tid = uuid4()
    journey._send("+919999005862", {"prompt_en": "next question?", "prompt_hi": ""}, "en", tenant_id=tid)
    assert captured.get("tenant_id") == tid, "walker _send must thread tenant_id into the record choke"
    assert captured.get("surface") == "journey"


# --- P1a (VT-587): the offer beat reads the store URL from the CURRENT message, never re-asks ---------


def test_recent_shop_domain_reads_current_body_first() -> None:
    """P1a: the owner who gives the store URL in the SAME message as the readiness affirm must have it
    used — the current inbound is scanned before the (fragile, same-run-uncommitted) conversation_log
    lookback. Pure: the current_body match returns before any DB call, so no substrate is needed."""
    from orchestrator.onboarding.journey import _recent_shop_domain

    tid = uuid4()
    assert _recent_shop_domain(tid, current_body="Yes lets connect. My store is Probe-Store-A.myshopify.com by the way") == "probe-store-a.myshopify.com"
    # No domain in the current body → falls through to the log lookback (None here, no seeded log).
    assert _recent_shop_domain(tid, current_body="what do you charge?") is None


# --- VT-700: post-activation agent-choice beat --------------------------------------------------


def test_agent_choice_tap_records_and_asks_readiness(substrate, _stub_sends, monkeypatch):
    """An exact catalog tap records the agent durably, confirms the trial in ONE message, and
    hands to the existing readiness machinery."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    recorded = {}
    import orchestrator.onboarding.draft_profile as dp

    monkeypatch.setattr(dp, "get_draft", lambda t: {"attributes": {}})
    monkeypatch.setattr(
        dp, "write_draft",
        lambda t, fields, *, source, **k: recorded.update({"fields": dict(fields), "source": source}),
    )
    tenant = _new_tenant(substrate.dsn, name="flow agent pick")
    _seed_completed_journey(substrate.dsn, tenant, "agent_choice")

    r = maybe_handle_journey_reply(tenant, "Sales Recovery", "SID-agent-1", "+919999003021")
    assert r is not None and r.get("routed") == "flow_agent_chosen"
    assert r.get("agent") == "sales_recovery"
    assert recorded["fields"] == {"activated_agents": ["sales_recovery"]}
    assert recorded["source"] == "owner"
    assert _flow(substrate.dsn, tenant) == "ready_asked"
    assert len(_stub_sends) == 1, "one message per beat"


def test_agent_choice_later_defers(substrate, _stub_sends):
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    tenant = _new_tenant(substrate.dsn, name="flow agent later")
    _seed_completed_journey(substrate.dsn, tenant, "agent_choice")
    r = maybe_handle_journey_reply(tenant, "later", "SID-agent-2", "+919999003022")
    assert r is not None and _flow(substrate.dsn, tenant) == "deferred"


def test_agent_choice_free_text_falls_through_to_brain(substrate, _stub_sends):
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    tenant = _new_tenant(substrate.dsn, name="flow agent freetext")
    _seed_completed_journey(substrate.dsn, tenant, "agent_choice")
    r = maybe_handle_journey_reply(
        tenant, "what does the recovery agent actually do?", "SID-agent-3", "+919999003023"
    )
    assert r is None, "free text is the brain's — the chooser never hijacks it"
    assert _flow(substrate.dsn, tenant) == "agent_choice", "the pick stays armed for a later tap"
