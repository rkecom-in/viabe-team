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


# --------------------------------------------------------------------------- #
# VT-330 — webhook hardening
# --------------------------------------------------------------------------- #
def _wh_payload(dbpool, event_id: str):
    """The razorpay_webhook_events payload (dict) for event_id, or None."""
    with dbpool.connection() as conn:
        r = conn.execute(
            "SELECT payload FROM razorpay_webhook_events WHERE event_id = %s", (event_id,)
        ).fetchone()
    if r is None:
        return None
    return r["payload"] if isinstance(r, dict) else r[0]


@pytest.mark.integration
def test_charged_non_int_amount_records_drop_not_500(_dbpool, _transitions, monkeypatch) -> None:
    """VT-330 poison-pill: a charged event with a NON-INT amount is RECORDED
    (dropped_parse_error, RAW committed) + Fazal-alerted + 200(drop), NOT a 500-loop."""
    alerts: list[str] = []
    # F3: un-mock _alert_fazal_safe — mock the INNER _alert_fazal so the REAL wrapper runs
    # (exercises the persist-in-txn → alert-after ordering).
    monkeypatch.setattr(
        "orchestrator.billing.refund_executor._alert_fazal", lambda m: alerts.append(m)
    )
    tid, sub = uuid4(), "sub_pp"
    _seed(_dbpool, tid, sub)

    out = _post(
        "evt_pp",
        "subscription.charged",
        {"subscription": {"entity": {"id": sub}}, "payment": {"entity": {"amount": "abc"}}},
    )
    assert out["status"] == "dropped_parse_error"  # 200, not a raise/500
    assert alerts, "Fazal was not alerted on the parse-drop"
    # persist-on-drop (Cowork hard req): the RAW event is durably committed for reconciliation.
    pay = _wh_payload(_dbpool, f"{_RUN}_evt_pp")
    assert pay is not None, "raw event LOST on the drop path"
    assert pay["_status"] == "dropped_parse_error"
    assert pay["raw"]["payment"]["entity"]["amount"] == "abc"  # raw preserved for reconciliation
    assert _sub_state(_dbpool, tid)[0] == 0  # NO fee applied


def test_charged_parse_drop_infra_failure_still_500(monkeypatch) -> None:
    """VT-330 F3: an INFRA failure on the drop-record path → HTTPException(500) (team-web 502 →
    Razorpay RETRIES), NOT swallowed into the parse-drop 200. The split's whole point — a
    transient error during the drop must not be lost. No DB needed (get_pool patched to raise)."""
    from fastapi import HTTPException

    import orchestrator.api.razorpay_ingress as ri

    def _boom():  # noqa: ANN202
        raise RuntimeError("db down")

    monkeypatch.setattr(ri, "get_pool", _boom)
    with pytest.raises(HTTPException) as exc:
        _post(
            "evt_infra",
            "subscription.charged",
            {"subscription": {"entity": {"id": "s"}}, "payment": {"entity": {"amount": "abc"}}},
        )
    assert exc.value.status_code == 500  # infra → 5xx, never the parse-drop 200


@pytest.mark.integration
def test_charged_amount_zero_alerts_under_count(_dbpool, _transitions, monkeypatch) -> None:
    """VT-330: a charged event resolving amount==0 alerts Fazal (a real charge is never 0 →
    under-count → under-refund). Fees unchanged (+0), event still processed."""
    import orchestrator.api.razorpay_ingress as ri

    alerts: list[str] = []
    monkeypatch.setattr(ri, "_alert_fazal_safe", lambda m: alerts.append(m))
    tid, sub = uuid4(), "sub_zero"
    _seed(_dbpool, tid, sub)

    out = _post("evt_zero", "subscription.charged", _charged(sub, 0))
    assert out["status"] == "processed"
    assert any("amount==0" in a for a in alerts), "no amount==0 alert"
    assert _sub_state(_dbpool, tid)[0] == 0  # +0


@pytest.mark.integration
def test_apply_event_charged_missing_subscription_alerts(_dbpool, monkeypatch) -> None:
    """VT-330 rowcount guard: _apply_event_sql for a tenant_id whose subscription row does NOT
    exist → the UPDATE affects 0 rows → Fazal-alert + 'subscription_missing' (the
    subscription-deleted race, staged directly since the handler's resolve uses the same row)."""
    import orchestrator.api.razorpay_ingress as ri

    alerts: list[str] = []
    monkeypatch.setattr(ri, "_alert_fazal_safe", lambda m: alerts.append(m))
    tid = uuid4()  # NO subscriptions row for this tenant
    with _dbpool.connection() as conn, conn.cursor() as cur:
        action, _ = ri._apply_event_sql(
            cur, str(tid), "subscription.charged", _charged("sub_x", 5000), event_id="evt_x"
        )
    assert action == "subscription_missing"
    assert alerts, "no missing-subscription alert"


# --- VT-352 — dead-letter + F1 replay-past-dedup -------------------------------------------------
def _dead_letter(dbpool, event_id: str):
    """The dead-letter (status, retry_count) for event_id, or None."""
    with dbpool.connection() as conn:
        r = conn.execute(
            "SELECT status, retry_count FROM razorpay_webhook_dead_letter WHERE event_id = %s",
            (event_id,),
        ).fetchone()
    return (dict(r) if isinstance(r, dict) else {"status": r[0], "retry_count": r[1]}) if r else None


@pytest.mark.integration
def test_drop_then_replay_applies_fee(_dbpool, _transitions, monkeypatch) -> None:
    """VT-352 F1: a dropped charged event (non-int amount), REPLAYED with the corrected amount
    (same event_id), re-processes PAST the dedup → the fee IS applied (pre-VT-352 it hit the dedup
    and was silently lost) + the dead-letter row flips pending→replayed."""
    monkeypatch.setattr("orchestrator.billing.refund_executor._alert_fazal", lambda m: None)
    tid, sub = uuid4(), "sub_dl_replay"
    _seed(_dbpool, tid, sub)

    # 1. drop (non-int amount) → dead-letter pending, NO fee.
    assert _post("evt_dl_replay", "subscription.charged", _charged(sub, "abc"))["status"] == "dropped_parse_error"
    assert _sub_state(_dbpool, tid)[0] == 0
    dl = _dead_letter(_dbpool, f"{_RUN}_evt_dl_replay")
    assert dl is not None and dl["status"] == "pending" and dl["retry_count"] == 0

    # 2. replay the CORRECTED event (same event_id, valid amount) → fee applied.
    out = _post("evt_dl_replay", "subscription.charged", _charged(sub, 499900))
    assert out["status"] == "processed" and out["action"] == "fees_incremented"
    assert _sub_state(_dbpool, tid)[0] == 499900  # FEE APPLIED (was lost pre-VT-352)
    dl2 = _dead_letter(_dbpool, f"{_RUN}_evt_dl_replay")
    assert dl2["status"] == "replayed" and dl2["retry_count"] == 1

    # 3. re-send the corrected event AGAIN → genuine duplicate, NO double-apply.
    assert _post("evt_dl_replay", "subscription.charged", _charged(sub, 499900))["status"] == "duplicate"
    assert _sub_state(_dbpool, tid)[0] == 499900  # still single


@pytest.mark.integration
def test_atomic_replay_failure_leaves_unprocessed(_dbpool, _transitions, monkeypatch) -> None:
    """VT-352 F1 ATOMIC (Cowork sharpening): if the apply RAISES mid-replay, the whole txn rolls
    back to the un-applied drop — marker intact, fee not applied, dead-letter still pending →
    re-replayable, never half-applied."""
    monkeypatch.setattr("orchestrator.billing.refund_executor._alert_fazal", lambda m: None)
    tid, sub = uuid4(), "sub_dl_atomic"
    _seed(_dbpool, tid, sub)
    assert _post("evt_dl_atomic", "subscription.charged", _charged(sub, "abc"))["status"] == "dropped_parse_error"

    import orchestrator.api.razorpay_ingress as ing

    def _boom(*a, **k):
        raise RuntimeError("apply failed mid-replay")

    monkeypatch.setattr(ing, "_apply_event_sql", _boom)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _post("evt_dl_atomic", "subscription.charged", _charged(sub, 499900))
    assert exc.value.status_code == 500  # infra-fail → Razorpay retries

    pay = _wh_payload(_dbpool, f"{_RUN}_evt_dl_atomic")
    assert pay["_status"] == "dropped_parse_error"  # marker NOT overwritten (rolled back)
    assert _sub_state(_dbpool, tid)[0] == 0  # NO fee
    assert _dead_letter(_dbpool, f"{_RUN}_evt_dl_atomic")["status"] == "pending"  # NOT flipped


@pytest.mark.integration
def test_replay_apply_ignored_422_re_replayable(_dbpool, _transitions, monkeypatch) -> None:
    """VT-352 F1 (Cowork bounce): a replay whose apply does NOT apply a fee (unknown/typo'd
    subscription_id → 'ignored') is rejected 422 and the drop stays RE-REPLAYABLE with its
    original payload — an operator typo must never record phantom success or destroy evidence."""
    monkeypatch.setattr("orchestrator.billing.refund_executor._alert_fazal", lambda m: None)
    tid, sub = uuid4(), "sub_dl_ignored"
    _seed(_dbpool, tid, sub)
    assert _post("evt_dl_ignored", "subscription.charged", _charged(sub, "abc"))["status"] == "dropped_parse_error"

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:  # corrected amount BUT an UNKNOWN subscription_id
        _post("evt_dl_ignored", "subscription.charged", _charged("sub_UNKNOWN", 499900))
    assert exc.value.status_code == 422

    pay = _wh_payload(_dbpool, f"{_RUN}_evt_dl_ignored")
    assert pay["_status"] == "dropped_parse_error"  # original marker intact (rolled back)
    assert _sub_state(_dbpool, tid)[0] == 0  # no fee on the wrong tenant either
    assert _dead_letter(_dbpool, f"{_RUN}_evt_dl_ignored")["status"] == "pending"  # still re-replayable


@pytest.mark.integration
def test_dead_letter_replay_wrong_sub_rejected(_dbpool, _transitions, monkeypatch) -> None:
    """VT-352 F2 (Cowork bounce): dead_letter.replay() refuses a corrected payload whose
    subscription_id != the dead-letter row's original — no cross-tenant fee application. The DL row
    stays pending; the original payload is intact."""
    monkeypatch.setattr("orchestrator.billing.refund_executor._alert_fazal", lambda m: None)
    from orchestrator.billing import dead_letter

    tid, sub = uuid4(), "sub_dl_wrongsub"
    _seed(_dbpool, tid, sub)
    assert _post("evt_dl_wrongsub", "subscription.charged", _charged(sub, "abc"))["status"] == "dropped_parse_error"
    eid = f"{_RUN}_evt_dl_wrongsub"

    with pytest.raises(ValueError, match="cross-tenant"):
        dead_letter.replay(eid, _charged("sub_DIFFERENT", 499900))
    assert _dead_letter(_dbpool, eid)["status"] == "pending"
    assert _wh_payload(_dbpool, eid)["_status"] == "dropped_parse_error"  # original intact


@pytest.mark.integration
def test_dead_letter_replay_correct_sub_applies_fee(_dbpool, _transitions, monkeypatch) -> None:
    """VT-352: dead_letter.replay() with the CORRECT subscription_id re-applies the fee (the F2
    cross-check passes) — proves the guard doesn't block a legitimate replay."""
    monkeypatch.setattr("orchestrator.billing.refund_executor._alert_fazal", lambda m: None)
    from orchestrator.billing import dead_letter

    tid, sub = uuid4(), "sub_dl_correctsub"
    _seed(_dbpool, tid, sub)
    assert _post("evt_dl_correctsub", "subscription.charged", _charged(sub, "abc"))["status"] == "dropped_parse_error"
    out = dead_letter.replay(f"{_RUN}_evt_dl_correctsub", _charged(sub, 499900))
    assert out["status"] == "processed" and out["action"] == "fees_incremented"
    assert _sub_state(_dbpool, tid)[0] == 499900
    assert _dead_letter(_dbpool, f"{_RUN}_evt_dl_correctsub")["status"] == "replayed"
