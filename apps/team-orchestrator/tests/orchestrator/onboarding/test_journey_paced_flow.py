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
    """A connector mid-onboarding (phase_2_auth) — OAuth not yet ingested (data NOT landed)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_integration_state (tenant_id, phase, current_connector_id) "
            "VALUES (%s, 'phase_2_auth', %s) "
            "ON CONFLICT (tenant_id) DO UPDATE SET phase = EXCLUDED.phase, "
            "current_connector_id = EXCLUDED.current_connector_id",
            (str(tenant_id), connector),
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


# --- beat (b): ack → readiness ask -------------------------------------------------------------


def test_ack_after_card_asks_readiness(substrate, _stub_sends):
    """After the profile card (profile_previewed), the owner's next message = an ack → a readiness ASK
    (set up connections now, one at a time, or later). It never steamrolls straight into an integration."""
    from orchestrator.onboarding.journey import maybe_handle_journey_reply

    tenant = _new_tenant(substrate.dsn, name="flow ack")
    _seed_completed_journey(substrate.dsn, tenant, "profile_previewed")

    r = maybe_handle_journey_reply(tenant, "ok", "SID-ack-1", "+919999003001")
    assert r is not None and r.get("routed") == "flow_readiness_ask"
    assert _flow(substrate.dsn, tenant) == "ready_asked"
    assert _stub_sends and "connect" in _stub_sends[-1].lower()
    # No integration onboarding started yet — just the ask.
    from orchestrator.onboarding.shopify_onboarding import read_integration_state
    assert read_integration_state(tenant) is None


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
