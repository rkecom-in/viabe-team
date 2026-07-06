"""VT-368 Gap-4 — DB-backed behavioral tests for paced bilingual plan delivery
(``orchestrator.business_plan.delivery.deliver_plan``).

Load-bearing behaviours under test (no network — the Twilio freeform send is a
monkeypatched spy; pacing's ``sleep_fn`` is injected as a recorder):

  - all parts sent IN ORDER: summary headline, one part per DISTINCT roadmap
    month (objectives in seq order + owner_action prompt), the Gap-6 hint —
    EN and HI variants per the owner's resolved locale;
  - K scales with the plan: a thin 2-month plan sends fewer parts;
  - IDEMPOTENT replay: pre-set ``delivered_parts`` bits are skipped — only
    unsent parts go out; a fully-delivered replay sends nothing;
  - one failing middle part: delivery continues, the failed bit stays 0
    (resent on the next replay), deliver_plan never raises;
  - the final part stamps ``delivered_at``.

Requires a real Postgres + the dbos stack. Mirrors the substrate pattern in
``tests/orchestrator/onboarding/test_journey.py``: migrations applied once,
DBOS launched so the ``tenant_connection`` pool exists, tenants seeded via a
direct service-role (BYPASSRLS) psycopg connection; the delivery path writes
through ``tenant_connection`` (RLS'd app_role); assertions read back via
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
    reason="DATABASE_URL not set — VT-368 delivery substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the ``tenant_connection`` pool exists.
    Mirrors tests/orchestrator/onboarding/test_journey.py."""
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
    name: str = "VT-368 delivery test",
    preferred_language: str | None = None,
) -> tuple[UUID, str]:
    """Seed a tenant with a whatsapp_number (the delivery recipient) and an
    optional preferred_language (the locale resolve_owner_locale reads)."""
    whatsapp = f"+9198{uuid4().int % 10**8:08d}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, whatsapp_number, preferred_language) "
            "VALUES (%s, 'founding', 'trial', now(), 'restaurant', %s, %s) RETURNING id",
            (name, whatsapp, preferred_language),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0])), whatsapp


def _delivery_state(dsn: str, tenant_id: UUID, version: int) -> dict[str, Any]:
    """delivered_parts bitmap + delivered_at for one version, via service role."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT delivered_parts, delivered_at FROM business_plan "
            "WHERE tenant_id = %s AND version = %s",
            (str(tenant_id), version),
        ).fetchone()
    assert row is not None
    return {"delivered_parts": int(row[0]), "delivered_at": row[1]}


# --- Plan fixtures (the per-item JSON shape store persists) -----------------


def _item(
    seq: int,
    month: int,
    objective: str,
    *,
    owner_action: str | None = None,
    owner_action_hi: str | None = None,
) -> dict[str, Any]:
    return {
        "item_id": str(uuid4()),
        "seq": seq,
        "month": month,
        "objective": objective,
        "why": "grounded on F1",
        "cited_facts": ["F1"],
        "owning_agent": "unassigned",
        "owner_action_needed": owner_action is not None,
        "owner_action": owner_action,
        "owner_action_hi": owner_action_hi,
        "status": "proposed",
        "provenance": {
            "origin": "llm_v1",
            "editor": None,
            "prev_version": None,
            "diff_from_prev": None,
        },
    }


_SUMMARY = {
    "text": "Sales dipped 12% in May; reviews held at 4.2 stars.",
    "text_hi": "मई में बिक्री 12% घटी; रिव्यू 4.2 स्टार पर स्थिर रहे।",
    "cited_facts": ["F1"],
    "headline_metrics": {"sales_delta_pct": -12},
}
_BUNDLE = {"F1": {"key": "sales_delta_pct", "value": -12, "source": "l2_metrics"}}


def _seed_plan(tenant_id: UUID, roadmap: list[dict[str, Any]]) -> int:
    from orchestrator.business_plan import store

    return store.write_new_version(
        tenant_id,
        summary=_SUMMARY,
        roadmap=roadmap,
        fact_bundle=_BUNDLE,
        generated_by="gap4_generator",
        model_id="test-model",
    )


def _three_month_roadmap() -> list[dict[str, Any]]:
    """Distinct months 1, 2, 4 (non-dense, asserts distinct-month grouping);
    month 1 has TWO items inserted seq-out-of-order (asserts seq sorting)."""
    return [
        _item(2, 1, "Launch the weekend combo offer"),
        _item(
            1,
            1,
            "Reply to all pending reviews",
            owner_action="Share your Google review link",
            owner_action_hi="अपना Google review लिंक भेजें",
        ),
        _item(3, 2, "Start the repeat-customer nudge"),
        _item(4, 4, "Review menu pricing for top sellers"),
    ]


# --- Spies -------------------------------------------------------------------


@pytest.fixture()
def send_spy(monkeypatch):  # type: ignore[no-untyped-def]
    """Spy on the Twilio freeform send (lazy-imported by delivery, so patching
    the source module attribute intercepts every part). No network."""
    import orchestrator.utils.twilio_send as twilio_send

    calls: list[dict[str, str]] = []

    def _spy(body: str, recipient_phone: str, **kw: Any) -> str:
        calls.append({"body": body, "recipient": recipient_phone, **kw})
        return f"SM-spy-{len(calls)}"

    monkeypatch.setattr(twilio_send, "send_freeform_message", _spy)
    return calls


# --- Tests -------------------------------------------------------------------


def test_compose_parts_strips_citation_markers_from_owner_render():
    """VT-576/CL-2026-07-03: owner-facing render strips [F#] receipts from the summary headline,
    every roadmap objective, and owner_action copy — the live drill leaked "... [F1][F5]" to the owner.
    The strip is RENDER-only (compose_parts); the stored plan artifact keeps its citations for VTR."""
    from orchestrator.business_plan import delivery, store

    plan = store.BusinessPlan(
        tenant_id=uuid4(),
        version=1,
        summary={"text": "Sales dipped 12% in May [F1]; reviews held at 4.2 stars [F5]."},
        roadmap=[
            _item(1, 1, "Reply to all pending reviews [F2]",
                  owner_action="Share your Google review link [F3]"),
            _item(2, 1, "Launch the weekend combo offer [F4][F5]"),
        ],
        fact_bundle=_BUNDLE,
        generated_by="gap4_generator",
        model_id="test-model",
        delivered_parts=0,
    )
    parts = delivery.compose_parts(plan, "en")
    blob = "\n".join(parts)
    assert "[F" not in blob, f"citation markers leaked into owner render: {blob!r}"
    # Content is preserved, just without the receipts (and no doubled spaces left behind).
    assert "Sales dipped 12% in May; reviews held at 4.2 stars." in parts[0]
    assert "Reply to all pending reviews" in blob and "Launch the weekend combo offer" in blob
    assert "Share your Google review link" in blob
    # The STORED artifact is untouched — the citations stay on the plan object the store persists.
    assert "[F1]" in plan.summary["text"], "stored artifact must retain citation receipts"


def test_full_delivery_en_order_pacing_bitmap(substrate, send_spy):  # type: ignore[no-untyped-def]
    """3 distinct months → 5 parts in order (summary, M1, M2, M4, hint), EN copy,
    seq-sorted objectives, owner_action prompt, 2.0s pacing between attempts,
    full bitmap + delivered_at stamped on the final part."""
    from orchestrator.business_plan import delivery

    tenant, whatsapp = _new_tenant(substrate.dsn, name="full EN delivery")
    version = _seed_plan(tenant, _three_month_roadmap())
    sleeps: list[float] = []

    delivery.deliver_plan(tenant, version, sleep_fn=sleeps.append)

    assert len(send_spy) == 5, "1 summary + 3 distinct months + 1 hint"
    assert all(c["recipient"] == whatsapp for c in send_spy)
    # VT-611 Package H0: tenant_id/surface threaded on every part (was bare -> conversation_log gap).
    assert all(c.get("tenant_id") == tenant for c in send_spy)
    assert all(c.get("surface") == "manager" for c in send_spy)

    bodies = [c["body"] for c in send_spy]
    assert bodies[0] == _SUMMARY["text"]
    assert bodies[1].startswith("Month 1")
    # seq order within the month: seq 1 (reviews) before seq 2 (combo offer)
    assert bodies[1].index("pending reviews") < bodies[1].index("weekend combo")
    assert "Your action: Share your Google review link" in bodies[1]
    assert bodies[2].startswith("Month 2") and "repeat-customer" in bodies[2]
    assert bodies[3].startswith("Month 4") and "menu pricing" in bodies[3]
    assert bodies[4] == "Reply to adjust any step."

    assert sleeps == [2.0] * 4, "pacing between consecutive parts, none before the first"

    state = _delivery_state(substrate.dsn, tenant, version)
    assert state["delivered_parts"] == 0b11111
    assert state["delivered_at"] is not None, "final part stamps delivered_at"


def test_delivery_records_every_part_to_conversation_log(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """VT-611 Package H0: deliver_plan's send_freeform_message call was bare (no tenant_id) —
    _record_owner_conversation_turn no-op'd, so a delivered plan never hit the lifetime
    conversation_log. Runs the REAL send_freeform_message (only the Twilio wire-level client is
    stubbed — no send_freeform_message mock here) so _record_owner_conversation_turn actually
    persists, then reads conversation_log back for real (proves the row LANDS, not just that the
    kwarg was threaded). The package's autouse Twilio stub returns a FIXED sid for every call
    (fine for other tests, which don't care), but conversation_log's (tenant_id, message_sid)
    unique index would collapse all 5 parts to 1 row on a fixed sid — so this test gives each
    call its own sid, matching how the real Twilio API behaves."""
    from unittest.mock import MagicMock

    from orchestrator.business_plan import delivery
    from orchestrator.utils import twilio_send

    sid_counter = iter(range(1, 1000))

    def _fake_client():
        client = MagicMock()
        client.messages.create = MagicMock(
            side_effect=lambda **kw: MagicMock(sid=f"SM{next(sid_counter):032d}")
        )
        return client

    monkeypatch.setattr(twilio_send, "_client", _fake_client)

    tenant, _ = _new_tenant(substrate.dsn, name="conversation-log proof")
    version = _seed_plan(tenant, _three_month_roadmap())

    delivery.deliver_plan(tenant, version, sleep_fn=lambda _s: None)

    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT text, surface, role FROM conversation_log WHERE tenant_id = %s "
            "ORDER BY created_at ASC, id ASC",
            (str(tenant),),
        ).fetchall()
    assert len(rows) == 5, "1 summary + 3 distinct months + 1 hint, every part logged"
    assert all(r[1] == "manager" and r[2] == "assistant" for r in rows), rows
    assert rows[0][0] == _SUMMARY["text"]


def test_full_delivery_hi_variant(substrate, send_spy):  # type: ignore[no-untyped-def]
    """preferred_language='hi' → text_hi summary, Hindi month header, the
    owner_action_hi prompt, and the Hindi Gap-6 hint."""
    from orchestrator.business_plan import delivery

    tenant, _ = _new_tenant(substrate.dsn, name="full HI delivery", preferred_language="hi")
    version = _seed_plan(tenant, _three_month_roadmap())

    delivery.deliver_plan(tenant, version, sleep_fn=lambda _s: None)

    bodies = [c["body"] for c in send_spy]
    assert len(bodies) == 5
    assert bodies[0] == _SUMMARY["text_hi"]
    assert bodies[1].startswith("महीना 1")
    assert "अपना Google review लिंक भेजें" in bodies[1]
    assert bodies[4] == "किसी भी कदम को बदलने के लिए जवाब दें।"

    state = _delivery_state(substrate.dsn, tenant, version)
    assert state["delivered_parts"] == 0b11111
    assert state["delivered_at"] is not None


def test_replay_sends_only_unsent_parts(substrate, send_spy):  # type: ignore[no-untyped-def]
    """Pre-set bits 0+1 (summary + Month 1 already landed) → replay sends ONLY
    parts 2..4; a second replay after full delivery sends NOTHING."""
    from orchestrator.business_plan import delivery, store

    tenant, _ = _new_tenant(substrate.dsn, name="replay resume")
    version = _seed_plan(tenant, _three_month_roadmap())
    store.mark_part_delivered(tenant, version, 0, final=False)
    store.mark_part_delivered(tenant, version, 1, final=False)
    sleeps: list[float] = []

    delivery.deliver_plan(tenant, version, sleep_fn=sleeps.append)

    bodies = [c["body"] for c in send_spy]
    assert len(bodies) == 3, "only the 3 unsent parts go out"
    assert bodies[0].startswith("Month 2")
    assert bodies[1].startswith("Month 4")
    assert bodies[2] == "Reply to adjust any step."
    assert sleeps == [2.0] * 2, "pacing only between the parts actually attempted"

    state = _delivery_state(substrate.dsn, tenant, version)
    assert state["delivered_parts"] == 0b11111
    assert state["delivered_at"] is not None

    send_spy.clear()
    delivery.deliver_plan(tenant, version, sleep_fn=sleeps.append)
    assert send_spy == [], "fully-delivered replay is a no-op"


def test_failing_middle_part_continues_and_replays(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """A failing Month-1 send: delivery continues through the remaining parts,
    the failed bit stays 0, deliver_plan does NOT raise; the next replay sends
    exactly the failed part."""
    import orchestrator.utils.twilio_send as twilio_send
    from orchestrator.business_plan import delivery

    tenant, _ = _new_tenant(substrate.dsn, name="failing middle part")
    version = _seed_plan(tenant, _three_month_roadmap())

    calls: list[str] = []

    def _flaky(body: str, recipient_phone: str, **kw: Any) -> str:
        calls.append(body)
        if body.startswith("Month 1"):
            raise RuntimeError("Twilio 5xx (simulated)")
        return f"SM-flaky-{len(calls)}"

    monkeypatch.setattr(twilio_send, "send_freeform_message", _flaky)
    delivery.deliver_plan(tenant, version, sleep_fn=lambda _s: None)  # must not raise

    assert len(calls) == 5, "every part attempted despite the middle failure"
    state = _delivery_state(substrate.dsn, tenant, version)
    assert state["delivered_parts"] == 0b11101, "bit 1 (the failed part) stays unset"
    assert state["delivered_at"] is not None, "the final part still stamped delivered_at"

    sent: list[str] = []
    monkeypatch.setattr(
        twilio_send, "send_freeform_message",
        lambda body, recipient_phone, **kw: sent.append(body),
    )
    delivery.deliver_plan(tenant, version, sleep_fn=lambda _s: None)

    assert len(sent) == 1 and sent[0].startswith("Month 1"), "replay resends ONLY the failed part"
    assert _delivery_state(substrate.dsn, tenant, version)["delivered_parts"] == 0b11111


def test_thin_plan_fewer_parts(substrate, send_spy):  # type: ignore[no-untyped-def]
    """K scales with the plan: 2 distinct months → exactly 4 parts."""
    from orchestrator.business_plan import delivery

    tenant, _ = _new_tenant(substrate.dsn, name="thin 2-month plan")
    version = _seed_plan(
        tenant,
        [_item(1, 1, "Fix the listing hours"), _item(2, 2, "Collect 10 reviews")],
    )

    delivery.deliver_plan(tenant, version, sleep_fn=lambda _s: None)

    bodies = [c["body"] for c in send_spy]
    assert len(bodies) == 4, "1 summary + 2 months + 1 hint"
    assert bodies[0] == _SUMMARY["text"]
    assert bodies[1].startswith("Month 1")
    assert bodies[2].startswith("Month 2")
    assert bodies[3] == "Reply to adjust any step."
    assert _delivery_state(substrate.dsn, tenant, version)["delivered_parts"] == 0b1111


def test_stale_version_and_missing_plan_send_nothing(substrate, send_spy):  # type: ignore[no-untyped-def]
    """Delivery only targets the LATEST version — a stale version no-ops; a
    tenant with no plan at all no-ops. Neither raises."""
    from orchestrator.business_plan import delivery

    tenant, _ = _new_tenant(substrate.dsn, name="stale version")
    v1 = _seed_plan(tenant, [_item(1, 1, "Old objective")])
    _seed_plan(tenant, [_item(1, 1, "New objective")])  # v2 supersedes

    delivery.deliver_plan(tenant, v1, sleep_fn=lambda _s: None)
    assert send_spy == [], "stale version must not deliver"
    assert _delivery_state(substrate.dsn, tenant, v1)["delivered_parts"] == 0

    planless, _ = _new_tenant(substrate.dsn, name="no plan yet")
    delivery.deliver_plan(planless, 1, sleep_fn=lambda _s: None)
    assert send_spy == [], "no plan → nothing to send, no raise"
