"""VT-89 — Razorpay webhook ingress (dedup-as-durable-inbox + fees writer + gated
phase transitions). Real-PG canary (gated on DATABASE_URL). The keystone assertion:
a redelivered subscription.charged does NOT double-count fees.

apply_transition (@DBOS.step) is monkeypatched to a recorder — no DBOS context under
a direct call; the canary asserts the SQL fee/counter state + the requested phase
event. No live Razorpay; the event payloads are synthetic.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.api.razorpay_ingress import (  # noqa: E402
    RazorpayIngressBody,
    razorpay_ingress,
)

_SECRET = "test-internal-secret-vt89"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", _SECRET)
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "test-salt-vt89")


@pytest.fixture
def _transitions(monkeypatch):
    """Record apply_transition(event) calls instead of running the @DBOS.step."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "orchestrator.transitions.apply_transition",
        lambda state, event, ctx: calls.append((str(state["tenant_id"]), event))
        or {**state, "phase": state["phase"]},
    )
    return calls


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
            db_url, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    yield get_pool()


def _seed(pool, tid: UUID, sub_id: str, *, phase: str = "paid_active") -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', %s) ON CONFLICT (id) DO NOTHING",
            (str(tid), f"vt89-{tid}", phase),
        )
        cur.execute(
            "INSERT INTO subscriptions (tenant_id, razorpay_subscription_id, status) "
            "VALUES (%s, %s, 'active')",
            (str(tid), sub_id),
        )


def _sub_state(pool, tid: UUID) -> tuple[int, int]:
    with pool.connection() as conn:
        r = conn.execute(
            "SELECT cumulative_fees_paid_paise AS f, consecutive_payment_failures AS c "
            "FROM subscriptions WHERE tenant_id=%s",
            (str(tid),),
        ).fetchone()
    return int(r["f"]), int(r["c"])


def _phase(pool, tid: UUID) -> str:
    with pool.connection() as conn:
        return conn.execute("SELECT phase FROM tenants WHERE id=%s", (str(tid),)).fetchone()[
            "phase"
        ]


def _charged(sub_id, amount):
    return {"subscription": {"entity": {"id": sub_id}}, "payment": {"entity": {"amount": amount}}}


def _by_sub(sub_id):
    return {"payment": {"entity": {"subscription_id": sub_id}}}


# Per-process run prefix so the fixed logical event_ids don't collide across local
# re-runs (the dedup PK is event_id; CI uses a fresh DB, local runs reuse one).
_RUN = uuid4().hex[:8]


def _post(event_id, event_type, payload, secret=_SECRET):
    return razorpay_ingress(
        RazorpayIngressBody(
            event_id=f"{_RUN}_{event_id}", event_type=event_type, payload=payload
        ),
        x_internal_secret=secret,
    )


def test_bad_secret_403() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _post("ev", "subscription.charged", _charged("sub_x", 100), secret="wrong")
    assert exc.value.status_code == 403


@pytest.mark.integration
def test_charged_increments_fees_dedup_no_double_count(_dbpool, _transitions) -> None:
    """KEYSTONE: a redelivered subscription.charged does NOT double-count fees."""
    tid = uuid4()
    sub = f"sub_{tid.hex[:12]}"
    _seed(_dbpool, tid, sub)

    out1 = _post("evt_charged_1", "subscription.charged", _charged(sub, 499900))
    assert out1["status"] == "processed" and out1["action"] == "fees_incremented"
    assert _sub_state(_dbpool, tid)[0] == 499900

    # REDELIVERY — same event_id -> duplicate, fees unchanged
    out2 = _post("evt_charged_1", "subscription.charged", _charged(sub, 499900))
    assert out2["status"] == "duplicate"
    assert _sub_state(_dbpool, tid)[0] == 499900  # NOT 999800

    # a NEW charge event_id -> increments again
    _post("evt_charged_2", "subscription.charged", _charged(sub, 499900))
    assert _sub_state(_dbpool, tid)[0] == 999800


@pytest.mark.integration
def test_captured_trial_converts(_dbpool, _transitions) -> None:
    tid = uuid4()
    sub = f"sub_{tid.hex[:12]}"
    _seed(_dbpool, tid, sub, phase="trial")
    out = _post("evt_cap_1", "payment.captured", _by_sub(sub))
    assert out["action"] == "converting_to_paid"
    assert (str(tid), "card_captured") in _transitions  # transition requested


@pytest.mark.integration
def test_captured_recurring_on_paid_is_noop(_dbpool, _transitions) -> None:
    """Q3: payment.captured for an already-paid_active tenant -> phase no-op, no error,
    fees unchanged (fees move only via subscription.charged)."""
    tid = uuid4()
    sub = f"sub_{tid.hex[:12]}"
    _seed(_dbpool, tid, sub, phase="paid_active")
    out = _post("evt_cap_recurring", "payment.captured", _by_sub(sub))
    assert out["action"] == "captured_noop"
    assert _transitions == []  # NO transition
    assert _sub_state(_dbpool, tid)[0] == 0  # captured never touches fees


@pytest.mark.integration
def test_three_strikes_to_paid_at_risk(_dbpool, _transitions) -> None:
    tid = uuid4()
    sub = f"sub_{tid.hex[:12]}"
    _seed(_dbpool, tid, sub, phase="paid_active")
    a1 = _post("evt_f1", "payment.failed", _by_sub(sub))
    a2 = _post("evt_f2", "payment.failed", _by_sub(sub))
    assert a1["action"] == "payment_failed_counted" and a2["action"] == "payment_failed_counted"
    assert _sub_state(_dbpool, tid)[1] == 2
    assert _transitions == []  # 1st/2nd do NOT transition
    a3 = _post("evt_f3", "payment.failed", _by_sub(sub))
    assert a3["action"] == "payment_failed_threshold"
    assert (str(tid), "payment_failed") in _transitions
    # a successful charge resets the counter
    _post("evt_reset", "subscription.charged", _charged(sub, 100))
    assert _sub_state(_dbpool, tid)[1] == 0


@pytest.mark.integration
def test_payment_failed_redelivery_no_double_increment(_dbpool, _transitions) -> None:
    """A redelivered payment.failed (same event_id) must NOT double-increment the
    counter — the event-level dedup gates the increment (Cowork Q2)."""
    tid = uuid4()
    sub = f"sub_{tid.hex[:12]}"
    _seed(_dbpool, tid, sub, phase="paid_active")
    _post("evt_fdup", "payment.failed", _by_sub(sub))
    assert _sub_state(_dbpool, tid)[1] == 1
    out = _post("evt_fdup", "payment.failed", _by_sub(sub))  # REDELIVERY
    assert out["status"] == "duplicate"
    assert _sub_state(_dbpool, tid)[1] == 1  # NOT 2


@pytest.mark.integration
def test_cross_tenant_isolation(_dbpool, _transitions) -> None:
    a, b = uuid4(), uuid4()
    sub_a = f"sub_{a.hex[:12]}"
    _seed(_dbpool, a, sub_a)
    _seed(_dbpool, b, f"sub_{b.hex[:12]}")
    _post("evt_x", "subscription.charged", _charged(sub_a, 250000))
    assert _sub_state(_dbpool, a)[0] == 250000
    assert _sub_state(_dbpool, b)[0] == 0  # tenant_b untouched


@pytest.mark.integration
def test_unknown_subscription_recorded_no_action(_dbpool, _transitions) -> None:
    out = _post("evt_unknown", "subscription.charged", _charged("sub_does_not_exist", 100))
    assert out["status"] == "processed" and out["action"] == "ignored"
    # event is durably recorded (dedup) even though no tenant matched
    with _dbpool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM razorpay_webhook_events WHERE event_id=%s",
            (f"{_RUN}_evt_unknown",),
        ).fetchone()["n"]
    assert n == 1
