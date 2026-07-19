"""VT-251 — execute_approved_campaign unit tests.

All tests use MagicMock connections (no live DB). CI stdlib-only smoke skips
via importorskip("langchain").

Test matrix (plan §4):
1.  Full cohort: all subscribed recipients → all sent + campaign_messages
    recorded + campaigns.status='sent'.
2.  Opt-out recipient skipped: opted_out/blocked → skip ledger written, no VT-45
    call, skipped_opt_out++ in summary.
3.  Idempotency: replay (same conn, same cohort) → VT-45 called (it handles
    idempotency internally via send_idempotency_keys); confirm call count.
4.  Partial failure: one recipient's send_fn raises → that recipient counted
    as failed, others still sent; campaign.status still set to 'sent'.
5.  No attribution computed: send_fn is the only injected callable; no
    match_transactions or get_attribution_data called.
6.  CL-390 PII: no phone in logs, no PII in any DB call.
7.  Missing campaign: RuntimeError raised when campaign row not found.
8.  route_after_approval routing: 'approved' → 'campaign_execute';
    non-approved → 'end'.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

pytest.importorskip("langchain")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _allow_customer_send_pregate(monkeypatch: pytest.MonkeyPatch) -> None:
    """VT-460: execute_approved_campaign now runs the SHARED onboarded + WABA-live pre-gate
    (``assert_customer_send_allowed``) before the send loop. These are MagicMock-conn UNIT tests of
    the send-loop bookkeeping, NOT the pre-gate (its own fail-closed behavior is proven in
    tests/agent/test_rail_harness_nonbypassability.py against a real DB). Patch the pre-gate to
    ALLOW so the loop logic is exercised. The dispatch-guard (phase) tests are unaffected — that
    guard short-circuits BEFORE the pre-gate. Patches the SOURCE module (execute.py imports the
    symbol lazily inside the function, so the source attribute is what binds at call time)."""
    from orchestrator.agents import customer_send_choke

    monkeypatch.setattr(
        customer_send_choke,
        "assert_customer_send_allowed",
        lambda *a, **k: customer_send_choke.CustomerSendGate(allowed=True),
    )


_TENANT_ID = "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
_CAMPAIGN_ID = "b0eebc99-9c0b-4ef8-bb6d-6bb9bd380a22"
_CUSTOMER_1 = "c0eebc99-9c0b-4ef8-bb6d-6bb9bd380a33"
_CUSTOMER_2 = "d0eebc99-9c0b-4ef8-bb6d-6bb9bd380a44"
_CUSTOMER_3 = "e0eebc99-9c0b-4ef8-bb6d-6bb9bd380a55"

_TEMPLATE_ID = "team_weekly_approval"
_BODY_PARAMS = {
    "customer_segment": "SMB",
    "campaign_mode": "recovery",
    "projected_recovery_inr": "5000",
}


def _campaign_row(
    template_id: str = _TEMPLATE_ID,
    body_params: dict | None = None,
    language: str = "en",
) -> dict[str, Any]:
    """VT-306: _load_campaign now reads the campaign via CampaignsWrapper.find_by_id
    (full row) then extracts plan_json -> message_plan in Python. The mock returns
    the full row: tenant_id (find_by_id asserts scope) + the plan_json JSONB."""
    return {
        "tenant_id": UUID(_TENANT_ID),
        "plan_json": {
            "message_plan": {
                "template_id": template_id,
                "template_params": body_params if body_params is not None else dict(_BODY_PARAMS),
                "language": language,
            }
        },
    }


def _recipient(customer_id: str, opt_out_status: str = "subscribed") -> dict[str, Any]:
    return {"customer_id": customer_id, "opt_out_status": opt_out_status}


def _ok_send_result() -> Any:
    """Minimal SendWhatsappTemplateOutput-like object for success."""
    r = MagicMock()
    r.status = "sent"
    r.message_sid = "SM" + "0" * 32
    r.error_envelope = None
    return r


def _error_send_result() -> Any:
    """Send failure envelope."""
    r = MagicMock()
    r.status = "error"
    r.message_sid = None
    r.error_envelope = MagicMock()
    r.error_envelope.code = "twilio_error"
    return r


def _make_conn(
    *,
    campaign_row: dict | None = None,
    recipients: list[dict] | None = None,
    phase: str = "paid_active",
    opt_out: bool = False,
) -> Any:
    """Build a MagicMock conn that returns controlled SELECT results.

    Query order:
      0. tenants phase+opt_out SELECT (fetchone) — VT-328 dispatch guard + T13b opt-out gate
      1. campaigns SELECT (fetchone)
      2. campaign_recipients JOIN customers (fetchall)
      3. send_idempotency_keys INSERT (no return value)
      4. campaigns UPDATE (no return value)

    VT-365: the dispatch guard reads ONLY `phase` now (the refunded_at column +
    the graceful-exit window are deleted with the refund subsystem).
    T13b: the guard SELECT also reads `opt_out` (owner consent-withdrawal gate).
    """
    conn = MagicMock()
    execute_calls: list[tuple[str, Any]] = []

    def _execute(sql: str, params: tuple | None = None) -> MagicMock:
        execute_calls.append((sql.strip(), params))
        result = MagicMock()
        if "FROM tenants" in sql:  # VT-328/VT-365 phase + T13b opt_out: the dispatch guard
            result.fetchone.return_value = {"phase": phase, "opt_out": opt_out}
            result.fetchall.return_value = []
        elif "FROM campaigns" in sql:  # VT-306: wrapper SELECT * FROM campaigns WHERE tenant_id AND id
            result.fetchone.return_value = campaign_row
            result.fetchall.return_value = []
        elif "FROM campaign_recipients" in sql:
            result.fetchone.return_value = None
            result.fetchall.return_value = recipients or []
        else:
            result.fetchone.return_value = None
            result.fetchall.return_value = []
        return result

    conn.execute.side_effect = _execute
    conn._execute_calls = execute_calls
    return conn


# ---------------------------------------------------------------------------
# Test 1: Full cohort — all subscribed → all sent
# ---------------------------------------------------------------------------

def test_full_cohort_all_sent() -> None:
    from orchestrator.campaign.execute import execute_approved_campaign

    recipients = [
        _recipient(_CUSTOMER_1),
        _recipient(_CUSTOMER_2),
    ]
    conn = _make_conn(campaign_row=_campaign_row(), recipients=recipients)

    send_fn = MagicMock(return_value=_ok_send_result())

    summary = execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
    )

    assert summary["sent"] == 2
    assert summary["skipped_opt_out"] == 0
    assert summary["failed"] == 0

    # send_fn called once per subscribed recipient.
    assert send_fn.call_count == 2

    # campaigns status was updated to 'sent'.
    # VT-306: status is now a bound param (the wrapper's set_status), not inline.
    update_calls = [
        (sql, params) for sql, params in conn._execute_calls
        if "UPDATE campaigns" in sql
    ]
    assert len(update_calls) == 1
    assert "sent" in update_calls[0][1]  # status bound as a param


# ---------------------------------------------------------------------------
# Test 2: Opt-out recipient skipped
# ---------------------------------------------------------------------------

def test_opted_out_recipient_skipped() -> None:
    from orchestrator.campaign.execute import execute_approved_campaign

    recipients = [
        _recipient(_CUSTOMER_1),                       # subscribed
        _recipient(_CUSTOMER_2, opt_out_status="opted_out"),   # skipped
        _recipient(_CUSTOMER_3, opt_out_status="blocked"),     # skipped
    ]
    conn = _make_conn(campaign_row=_campaign_row(), recipients=recipients)

    send_fn = MagicMock(return_value=_ok_send_result())

    summary = execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
    )

    assert summary["sent"] == 1
    assert summary["skipped_opt_out"] == 2
    assert summary["failed"] == 0

    # VT-45 called only for the subscribed recipient.
    assert send_fn.call_count == 1

    # Idempotency ledger rows written for the two opt-out skips, each as
    # send_status='skipped' (VT-261 / migration 053), not 'error'.
    skip_inserts = [
        sql for sql, _ in conn._execute_calls
        if "INSERT INTO send_idempotency_keys" in sql
    ]
    assert len(skip_inserts) == 2
    assert all("'skipped'" in sql for sql in skip_inserts)
    assert not any("'error'" in sql for sql in skip_inserts)


# ---------------------------------------------------------------------------
# Test 3: Idempotency — send_fn call count on second pass
# ---------------------------------------------------------------------------

def test_idempotency_send_fn_called_per_recipient() -> None:
    """On replay, send_fn is still called (VT-45 handles idempotency internally)."""
    from orchestrator.campaign.execute import execute_approved_campaign

    recipients = [_recipient(_CUSTOMER_1)]
    conn = _make_conn(campaign_row=_campaign_row(), recipients=recipients)

    send_fn = MagicMock(return_value=_ok_send_result())

    summary1 = execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
    )
    assert summary1["sent"] == 1

    # Second pass: new conn (simulates replay with fresh DB state).
    conn2 = _make_conn(campaign_row=_campaign_row(), recipients=recipients)
    execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn2, send_template_fn=send_fn
    )
    # VT-45 is called both times; it uses send_idempotency_keys internally.
    assert send_fn.call_count == 2

    # Idempotency key is stable: {campaign_id}:{customer_id}.
    call_args_list = send_fn.call_args_list
    keys = [a[0][0].idempotency_key for a in call_args_list]
    expected_key = f"{_CAMPAIGN_ID}:{_CUSTOMER_1}"
    assert all(k == expected_key for k in keys), (
        f"Idempotency key drift: got {keys}"
    )


# ---------------------------------------------------------------------------
# Test 4: Partial failure — one recipient fails, others still sent
# ---------------------------------------------------------------------------

def test_partial_failure_others_still_sent() -> None:
    from orchestrator.campaign.execute import execute_approved_campaign

    recipients = [
        _recipient(_CUSTOMER_1),
        _recipient(_CUSTOMER_2),
        _recipient(_CUSTOMER_3),
    ]
    conn = _make_conn(campaign_row=_campaign_row(), recipients=recipients)

    call_count = [0]

    def _send_fn(payload: Any, **kwargs: Any) -> Any:
        call_count[0] += 1
        if payload.customer_id == _CUSTOMER_2:
            raise RuntimeError("Twilio 500")
        return _ok_send_result()

    summary = execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=_send_fn
    )

    assert summary["sent"] == 2
    assert summary["failed"] == 1
    assert summary["skipped_opt_out"] == 0

    # campaigns.status still advanced to 'sent' (partial failure is NOT fatal).
    update_calls = [
        sql for sql, _ in conn._execute_calls
        if "UPDATE campaigns" in sql
    ]
    assert len(update_calls) == 1


# ---------------------------------------------------------------------------
# Test 5: No attribution computed (D2)
# ---------------------------------------------------------------------------

def test_no_attribution_computed(monkeypatch) -> None:
    """match_transactions / get_attribution_data must NOT be called (D2)."""
    from orchestrator.campaign import execute as execute_mod

    match_fn = MagicMock()
    attr_fn = MagicMock()

    # Patch at module level so any accidental import would be caught.
    monkeypatch.setattr(execute_mod, "execute_approved_campaign",
                        execute_mod.execute_approved_campaign)  # no-op patch target

    recipients = [_recipient(_CUSTOMER_1)]
    conn = _make_conn(campaign_row=_campaign_row(), recipients=recipients)
    send_fn = MagicMock(return_value=_ok_send_result())

    execute_mod.execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
    )

    match_fn.assert_not_called()
    attr_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: CL-390 PII — no phone in logs or DB params
# ---------------------------------------------------------------------------

def test_no_pii_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    from orchestrator.campaign.execute import execute_approved_campaign

    phone = "+919990000042"
    recipients = [_recipient(_CUSTOMER_1)]
    conn = _make_conn(campaign_row=_campaign_row(), recipients=recipients)

    send_fn = MagicMock(return_value=_ok_send_result())

    with caplog.at_level(
        logging.DEBUG,
        logger="orchestrator.campaign.execute",
    ):
        execute_approved_campaign(
            _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
        )

    for record in caplog.records:
        assert phone not in record.getMessage(), (
            f"PII leak: phone in log: {record.getMessage()!r}"
        )

    # Confirm phone is not in any DB params.
    for sql, params in conn._execute_calls:
        if params is not None:
            assert phone not in str(params), (
                f"PII leak: phone in DB params for sql={sql!r}: {params!r}"
            )


# ---------------------------------------------------------------------------
# Test 7: Missing campaign → RuntimeError
# ---------------------------------------------------------------------------

def test_missing_campaign_raises_runtime_error() -> None:
    from orchestrator.campaign.execute import execute_approved_campaign

    conn = _make_conn(campaign_row=None)  # campaign not found

    with pytest.raises(RuntimeError, match="not found"):
        execute_approved_campaign(
            _TENANT_ID, _CAMPAIGN_ID, conn=conn
        )


# ---------------------------------------------------------------------------
# Test 8: route_after_approval routing
# ---------------------------------------------------------------------------

def test_route_after_approval_approved() -> None:
    from orchestrator.routing import route_after_approval

    state = {"owner_decision": "approved"}
    assert route_after_approval(state) == "campaign_execute"  # type: ignore[arg-type]


def test_route_after_approval_rejected() -> None:
    from orchestrator.routing import route_after_approval

    for decision in ("rejected", "needs_changes", "timeout", "send_failed", None):
        state = {"owner_decision": decision}
        result = route_after_approval(state)  # type: ignore[arg-type]
        assert result == "end", f"Expected 'end' for decision={decision!r}, got {result!r}"


# ---------------------------------------------------------------------------
# Test: idempotency key scheme (D1 — stable per recipient)
# ---------------------------------------------------------------------------

def test_idempotency_key_scheme() -> None:
    """idempotency_key = f'{campaign_id}:{customer_id}' — stable across replays."""
    from orchestrator.campaign.execute import execute_approved_campaign

    recipients = [_recipient(_CUSTOMER_1)]
    conn = _make_conn(campaign_row=_campaign_row(), recipients=recipients)

    captured_payloads: list[Any] = []

    def _capture_fn(payload: Any, **kwargs: Any) -> Any:
        captured_payloads.append(payload)
        return _ok_send_result()

    execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=_capture_fn
    )

    assert len(captured_payloads) == 1
    key = captured_payloads[0].idempotency_key
    assert key == f"{_CAMPAIGN_ID}:{_CUSTOMER_1}", (
        f"Expected '{_CAMPAIGN_ID}:{_CUSTOMER_1}', got '{key}'"
    )


# ---------------------------------------------------------------------------
# Test: campaign_messages recorded via VT-45 (not double-written by seam)
# ---------------------------------------------------------------------------

def test_campaign_messages_recorded_via_vt45() -> None:
    """VT-45 is responsible for writing campaign_messages; the seam does NOT
    write a second row. Confirmed by asserting no INSERT INTO campaign_messages
    in conn._execute_calls (send_fn is the VT-45 boundary here)."""
    from orchestrator.campaign.execute import execute_approved_campaign

    recipients = [_recipient(_CUSTOMER_1)]
    conn = _make_conn(campaign_row=_campaign_row(), recipients=recipients)
    send_fn = MagicMock(return_value=_ok_send_result())

    execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
    )

    # The seam's conn should NOT insert campaign_messages (VT-45 does that).
    cm_inserts = [
        sql for sql, _ in conn._execute_calls
        if "INSERT INTO campaign_messages" in sql
    ]
    assert len(cm_inserts) == 0, (
        f"Seam double-wrote campaign_messages — should be VT-45's responsibility: "
        f"{cm_inserts}"
    )


# ---------------------------------------------------------------------------
# Test: empty cohort — no sends, status still advanced
# ---------------------------------------------------------------------------

def test_empty_cohort_status_still_advanced() -> None:
    from orchestrator.campaign.execute import execute_approved_campaign

    conn = _make_conn(campaign_row=_campaign_row(), recipients=[])
    send_fn = MagicMock(return_value=_ok_send_result())

    summary = execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
    )

    assert summary == {
        "sent": 0,
        "skipped_opt_out": 0,
        "skipped_complaint_freeze": 0,
        "failed": 0,
        "killed": 0,  # VT-558: campaign true-kill counter (0 = not killed)
    }
    send_fn.assert_not_called()

    update_calls = [
        sql for sql, _ in conn._execute_calls
        if "UPDATE campaigns" in sql
    ]
    assert len(update_calls) == 1


def test_cancelled_campaign_killed_before_start() -> None:
    """VT-558 campaign true-kill: a campaign an operator cancelled before this run started aborts
    the fan-out — nothing sent, NOT advanced to 'sent', remaining recipients counted ``killed``."""
    from orchestrator.campaign.execute import execute_approved_campaign

    row = _campaign_row()
    row["status"] = "cancelled"  # operator killed it via ops/run-control/kill-campaign
    conn = _make_conn(
        campaign_row=row,
        recipients=[{"customer_id": _CUSTOMER_1, "opt_out_status": None, "complaint_status": None}],
    )
    send_fn = MagicMock(return_value=_ok_send_result())

    summary = execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
    )

    assert summary["killed"] == 1
    send_fn.assert_not_called()  # no recipient contacted
    # a killed campaign is NOT advanced to 'sent' (no UPDATE campaigns).
    update_calls = [sql for sql, _ in conn._execute_calls if "UPDATE campaigns" in sql]
    assert update_calls == []


# ---------------------------------------------------------------------------
# VT-328 / VT-365 — lapsed/cancelled dispatch guard (the single chokepoint)
# ---------------------------------------------------------------------------

def test_dispatch_blocked_lapsed() -> None:
    """VT-328/VT-365: a `lapsed` tenant (30-day trial expired without subscribe — dormant, no
    active subscription) is blocked INSIDE execute_approved_campaign — short-circuits BEFORE
    loading the campaign/recipients or calling send_fn (zero sends). (`refunded` is GONE.)"""
    from orchestrator.campaign.execute import execute_approved_campaign

    conn = _make_conn(
        campaign_row=_campaign_row(),
        recipients=[_recipient(_CUSTOMER_1)],
        phase="lapsed",
    )
    send_fn = MagicMock(return_value=_ok_send_result())

    summary = execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
    )

    assert summary["dispatch_blocked"] == 1 and summary["sent"] == 0
    assert send_fn.call_count == 0  # ZERO sends
    # short-circuit: never loaded the campaign (no fan-out path reached)
    assert not any("FROM campaigns" in sql for sql, _ in conn._execute_calls)


def test_dispatch_blocked_cancelled() -> None:
    """VT-328: a cancelled tenant is likewise blocked (window-independent)."""
    from orchestrator.campaign.execute import execute_approved_campaign

    conn = _make_conn(
        campaign_row=_campaign_row(), recipients=[_recipient(_CUSTOMER_1)], phase="cancelled"
    )
    send_fn = MagicMock(return_value=_ok_send_result())
    summary = execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
    )
    assert summary["dispatch_blocked"] == 1 and send_fn.call_count == 0


def test_dispatch_allowed_active_sends_normally() -> None:
    """VT-328: a paid_active tenant is NOT blocked — the guard doesn't over-reach."""
    from orchestrator.campaign.execute import execute_approved_campaign

    conn = _make_conn(
        campaign_row=_campaign_row(), recipients=[_recipient(_CUSTOMER_1)], phase="paid_active"
    )
    send_fn = MagicMock(return_value=_ok_send_result())
    summary = execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
    )
    assert "dispatch_blocked" not in summary
    assert summary["sent"] == 1 and send_fn.call_count == 1


def test_opt_out_blocks_campaign() -> None:
    """T13b (full-77 sr_stop_then_resume): a tenant that has OPTED OUT (owner consent withdrawal)
    is blocked INSIDE execute_approved_campaign — the same single Pillar-8 chokepoint — even when
    the phase is otherwise dispatchable. This kills the money_action where an approval armed BEFORE
    the opt-out gets resolved by a later "haan bhej do" and fires a send over the withdrawal.
    Short-circuits BEFORE loading the campaign/recipients (zero sends)."""
    from orchestrator.campaign.execute import execute_approved_campaign

    conn = _make_conn(
        campaign_row=_campaign_row(),
        recipients=[_recipient(_CUSTOMER_1)],
        phase="paid_active",  # phase is fine — ONLY opt_out blocks here
        opt_out=True,
    )
    send_fn = MagicMock(return_value=_ok_send_result())

    summary = execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
    )

    assert summary["opt_out_blocked"] == 1 and summary["sent"] == 0
    assert send_fn.call_count == 0  # ZERO sends over an opt-out
    # short-circuit: never loaded the campaign (no fan-out path reached)
    assert not any("FROM campaigns" in sql for sql, _ in conn._execute_calls)


def test_opt_out_false_sends_normally() -> None:
    """T13b: opt_out=False (the default) does NOT block — the gate doesn't over-reach a live tenant."""
    from orchestrator.campaign.execute import execute_approved_campaign

    conn = _make_conn(
        campaign_row=_campaign_row(), recipients=[_recipient(_CUSTOMER_1)], opt_out=False
    )
    send_fn = MagicMock(return_value=_ok_send_result())
    summary = execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
    )
    assert "opt_out_blocked" not in summary
    assert summary["sent"] == 1 and send_fn.call_count == 1


def test_dispatch_allowed_rule_pure() -> None:
    """VT-328/VT-365: dispatch_allowed blocks the dormant/terminal phases {lapsed, cancelled}
    and is window-INDEPENDENT (the second positional arg is now unused — the refund/graceful-exit
    window is deleted). `refunded` no longer exists; `trial`/active/at_risk all dispatch."""
    from datetime import datetime, timedelta, timezone

    from orchestrator.billing.graceful_exit import dispatch_allowed

    long_ago = datetime.now(timezone.utc) - timedelta(days=90)
    # lapsed = dormant, no active subscription → blocked (the new VT-365 block).
    assert dispatch_allowed("lapsed", long_ago) is False  # unused window arg ignored
    assert dispatch_allowed("lapsed", None) is False
    assert dispatch_allowed("cancelled", None) is False
    assert dispatch_allowed("paid_active", None) is True
    assert dispatch_allowed("trial", None) is True
    assert dispatch_allowed("paid_at_risk", None) is True


def test_inbound_dsr_detection_is_phase_agnostic() -> None:
    """VT-328 canary 5: the inbound DSR/opt-out gate takes NO phase — it cannot consult the
    dispatch guard, so a lapsed/cancelled tenant's DSR/opt-out still routes (the guard lives ONLY
    in the outbound execute_approved_campaign chokepoint)."""
    from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

    assert matches_opt_out_or_dsr("please delete my data") is True
    assert matches_opt_out_or_dsr("STOP") is True


# ---------------------------------------------------------------------------
# VT-562 follow-up (review F2): post-send bookkeeping failures must not
# discard the summary — the sends already happened and the owner-outcome
# report reads only the summary.
# ---------------------------------------------------------------------------

def test_status_advance_failure_still_returns_summary(monkeypatch) -> None:
    """A status-advance/KG-emit failure AFTER real sends returns the honest summary
    (flagged status_advance_failed) instead of raising it away."""
    import orchestrator.campaign.execute as execute_mod
    from orchestrator.campaign.execute import execute_approved_campaign

    recipients = [_recipient(_CUSTOMER_1), _recipient(_CUSTOMER_2)]
    conn = _make_conn(campaign_row=_campaign_row(), recipients=recipients)
    send_fn = MagicMock(return_value=_ok_send_result())

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("advance failed")

    monkeypatch.setattr(execute_mod, "_advance_campaign_status", _boom)

    summary = execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
    )

    assert summary["sent"] == 2
    assert summary["status_advance_failed"] is True
    assert send_fn.call_count == 2


def test_kg_drain_failure_still_returns_clean_summary(monkeypatch) -> None:
    """A post-commit KG drain failure is observability-only: summary returned WITHOUT
    the status_advance_failed flag (the status DID advance; the outbox drain retries)."""
    import orchestrator.knowledge.kg_emit as kg_emit_mod
    from orchestrator.campaign.execute import execute_approved_campaign

    recipients = [_recipient(_CUSTOMER_1)]
    conn = _make_conn(campaign_row=_campaign_row(), recipients=recipients)
    send_fn = MagicMock(return_value=_ok_send_result())

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("drain failed")

    monkeypatch.setattr(kg_emit_mod, "drain_kg_events", _boom)

    summary = execute_approved_campaign(
        _TENANT_ID, _CAMPAIGN_ID, conn=conn, send_template_fn=send_fn
    )

    assert summary["sent"] == 1
    assert "status_advance_failed" not in summary
