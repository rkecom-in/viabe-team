"""VT-85 — day-39 refund-conversation engine (offer + reply intake + 48h timeout).

Pure tests cover the deterministic EN+HI classifier. Integration tests (gated on
DATABASE_URL) drive the real-PG flow: the offer parks the tenant (no auto-refund);
REFUND routes to execute_refund; CONTINUE resumes paid_active + suppression;
DISCUSS alerts Fazal; the 48h timeout defaults to CONTINUE; the inbound gate routes
only refund_offered tenants. apply_transition (@DBOS.step) is monkeypatched with a
TRANSITIONS-driven fake (no DBOS context under a direct test call).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.owner_inputs.refund_reply import (  # noqa: E402
    classify_refund_reply,
    handle_refund_decision,
)


@pytest.fixture(autouse=True)
def _phone_salt(monkeypatch):
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "test-salt-vt85")


# ---------------------------------------------------------------------------
# Pure — deterministic classifier (EN + HI; Pillar 7 never-guess)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected",
    [
        ("REFUND", "refund"),
        ("i want a refund", "refund"),
        ("रिफंड", "refund"),
        ("continue", "continue"),
        ("जारी", "continue"),
        ("जारी रखें", "continue"),
        ("discuss", "discuss"),
        ("चर्चा", "discuss"),
        ("refund please", "refund"),  # short affirmative still classifies
        # False positives that must NOT fire a financial decision (Pillar 7):
        ("refundable", None),  # substring must NOT fire
        ("maybe later", None),
        ("refund or continue?", None),  # ambiguous + question
        ("can i understand the refund policy?", None),  # question
        ("i will not refund", None),  # negation
        ("no refund", None),  # negation
        ("क्या बात है", None),  # benign HI ("what's up") — बात dropped + क्या interrogative
        # B1 (Cowork blocker): contraction negations must NOT auto-refund:
        ("don't refund me", None),
        ("won't refund", None),
        ("please don't refund me!", None),
        ("i won't take the refund", None),
        ("can't refund", None),
        # B2 (Cowork blocker): opt-out / DSR intent must NOT auto-refund:
        ("delete my data and refund me", None),
        ("stop sending me refund messages", None),
        ("please refund me also unsubscribe", None),
        ("cancel and refund", None),
        ("", None),
    ],
)
def test_classify_refund_reply(body, expected) -> None:
    assert classify_refund_reply(body) == expected


# ---------------------------------------------------------------------------
# Integration — gated on DATABASE_URL
# ---------------------------------------------------------------------------


@pytest.fixture
def _dbpool():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL not set; integration test requires real DB")
    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            db_url,
            min_size=1,
            max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    yield get_pool()


@pytest.fixture
def _patch_transition(monkeypatch):
    """Fake apply_transition that applies the REAL TRANSITIONS effect to the DB
    (so canaries assert phase) without a DBOS context."""

    def _fake(state, event, context):
        from orchestrator.graph import get_pool
        from orchestrator.transitions import TRANSITIONS

        to = TRANSITIONS.get((state["phase"], event))
        if to is not None:
            with get_pool().connection() as conn:
                conn.execute(
                    "UPDATE tenants SET phase=%s, phase_entered_at=now() WHERE id=%s",
                    (to, str(state["tenant_id"])),
                )
        return {**state, "phase": to or state["phase"]}

    monkeypatch.setattr("orchestrator.transitions.apply_transition", _fake)


def _seed_paid(pool, tid: UUID, *, fees_paise: int = 50000) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, paid_conversion_at, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'paid_active', now() - interval '40 days', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (str(tid), f"vt85-{tid}", f"+9199{str(tid.int)[:8]}"),
        )
        cur.execute(
            "INSERT INTO subscriptions (tenant_id, razorpay_subscription_id, status, started_at, cumulative_fees_paid_paise) "
            "VALUES (%s, %s, 'active', now() - interval '40 days', %s)",
            (str(tid), f"sub_{tid.hex[:12]}", fees_paise),
        )


def _phase(pool, tid: UUID) -> str:
    with pool.connection() as conn:
        return conn.execute("SELECT phase FROM tenants WHERE id=%s", (str(tid),)).fetchone()[
            "phase"
        ]


def _set_phase(pool, tid: UUID, phase: str, *, entered_ago_hours: int = 0) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE tenants SET phase=%s, phase_entered_at = now() - make_interval(hours => %s) WHERE id=%s",
            (phase, entered_ago_hours, str(tid)),
        )


@pytest.mark.integration
def test_offer_send_parks_not_refunds(_dbpool, _patch_transition) -> None:
    """The day-39 refund verdict OFFERS (phase=refund_offered) — it does NOT
    auto-refund (Pillar 7)."""
    import orchestrator.scheduled_triggers as st

    tid = uuid4()
    _seed_paid(_dbpool, tid)
    verdict = SimpleNamespace(
        tenant_id=tid,
        verdict="refund_triggered",
        cumulative_fees_paise=50000,
        arrr_paise=100,
        already_decided=False,
    )
    st._send_day39_refund_offer(tid, verdict)
    assert _phase(_dbpool, tid) == "refund_offered"  # parked, NOT refunded


@pytest.mark.integration
def test_refund_reply_routes_to_execute_refund(_dbpool, _patch_transition, monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        "orchestrator.billing.refund_executor.execute_refund",
        lambda tid, reason: calls.append((tid, reason)),
    )
    tid = uuid4()
    _seed_paid(_dbpool, tid)
    _set_phase(_dbpool, tid, "refund_offered")
    handle_refund_decision(tid, "refund", "SM123")
    assert calls == [(tid, "day39_eligibility")]


@pytest.mark.integration
def test_continue_reply_resumes_paid_active(_dbpool, _patch_transition) -> None:
    tid = uuid4()
    _seed_paid(_dbpool, tid)
    _set_phase(_dbpool, tid, "refund_offered")
    handle_refund_decision(tid, "continue", "SM124")
    assert _phase(_dbpool, tid) == "paid_active"
    # day39_continue emitted (the 90-day suppression marker)
    import time

    time.sleep(0.4)
    with _dbpool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM pipeline_log WHERE tenant_id=%s AND event_type='day39_continue'",
            (str(tid),),
        ).fetchone()["n"]
    assert n >= 1


@pytest.mark.integration
def test_discuss_reply_alerts_fazal_stays_offered(_dbpool, _patch_transition, monkeypatch) -> None:
    alerts: list[str] = []
    monkeypatch.setattr(
        "orchestrator.billing.refund_executor._alert_fazal", lambda text: alerts.append(text)
    )
    tid = uuid4()
    _seed_paid(_dbpool, tid)
    _set_phase(_dbpool, tid, "refund_offered")
    handle_refund_decision(tid, "discuss", "SM125")
    assert len(alerts) == 1
    assert _phase(_dbpool, tid) == "refund_offered"  # Fazal resolves manually


@pytest.mark.integration
def test_48h_timeout_defaults_to_continue(_dbpool, _patch_transition) -> None:
    import orchestrator.scheduled_triggers as st

    tid = uuid4()
    _seed_paid(_dbpool, tid)
    _set_phase(_dbpool, tid, "refund_offered", entered_ago_hours=49)
    # a fresh offer (1h old) must NOT be swept
    fresh = uuid4()
    _seed_paid(_dbpool, fresh)
    _set_phase(_dbpool, fresh, "refund_offered", entered_ago_hours=1)

    defaulted = st.run_refund_offer_timeout_sweep_body(now=datetime.now(timezone.utc))
    assert tid in defaulted
    assert fresh not in defaulted
    assert _phase(_dbpool, tid) == "paid_active"
    assert _phase(_dbpool, fresh) == "refund_offered"


@pytest.mark.integration
def test_inbound_gate_routes_only_refund_offered(_dbpool, _patch_transition, monkeypatch) -> None:
    from orchestrator.runner import try_resume_pending_refund_offer

    monkeypatch.setattr(
        "orchestrator.billing.refund_executor.execute_refund", lambda tid, reason: None
    )
    # refund_offered tenant + clear REFUND -> consumed
    offered = uuid4()
    _seed_paid(_dbpool, offered)
    _set_phase(_dbpool, offered, "refund_offered")
    assert try_resume_pending_refund_offer(str(offered), "REFUND", "SM1") == "refund"

    # paid_active tenant -> falls through (None), DSR/opt-out unaffected
    active = uuid4()
    _seed_paid(_dbpool, active)
    assert try_resume_pending_refund_offer(str(active), "REFUND", "SM2") is None


@pytest.mark.integration
def test_unclear_reply_falls_through(_dbpool, _patch_transition, monkeypatch) -> None:
    """A refund_offered tenant's UNCLEAR reply (benign/DSR/opt-out) must NOT be
    consumed — returns None so pre_filter (DSR/opt-out) still handles it. Opt-out/DSR
    ALWAYS win over a co-occurring refund keyword (Cowork B2); contraction negations
    do not auto-refund (Cowork B1)."""
    from orchestrator.runner import try_resume_pending_refund_offer

    # if the gate ever consumed one of these, execute_refund would fire — fail loud
    monkeypatch.setattr(
        "orchestrator.billing.refund_executor.execute_refund",
        lambda tid, reason: pytest.fail("refund must NOT fire on a DSR/opt-out/negation reply"),
    )
    tid = uuid4()
    _seed_paid(_dbpool, tid)
    _set_phase(_dbpool, tid, "refund_offered")
    for body in (
        "please delete my data",
        "क्या बात है",
        "delete my data and refund me",  # B2: DSR + refund co-occur -> DSR wins
        "stop sending me refund messages",  # B2: opt-out intent + refund -> None
        "don't refund me",  # B1: contraction negation
    ):
        assert try_resume_pending_refund_offer(str(tid), body, "SMx") is None
    assert _phase(_dbpool, tid) == "refund_offered"  # untouched — Fazal/timeout resolves


@pytest.mark.integration
def test_refund_reply_execute_failure_does_not_crash(
    _dbpool, _patch_transition, monkeypatch
) -> None:
    """An UNEXPECTED execute_refund failure must not crash inbound handling — it is
    caught + Fazal-alerted (the wrapper backstop)."""

    def _boom(tid, reason):
        raise RuntimeError("razorpay exploded")

    alerts: list[str] = []
    monkeypatch.setattr("orchestrator.billing.refund_executor.execute_refund", _boom)
    monkeypatch.setattr(
        "orchestrator.billing.refund_executor._alert_fazal", lambda text: alerts.append(text)
    )
    tid = uuid4()
    _seed_paid(_dbpool, tid)
    _set_phase(_dbpool, tid, "refund_offered")
    handle_refund_decision(tid, "refund", "SM5")  # must NOT raise
    assert len(alerts) == 1
