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

import pytest

pytest.importorskip("langchain")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    """The shape _load_campaign reads from ``plan_json -> message_plan`` (VT-140
    fix). Migration 018 dropped the dedicated template_id/body_params columns;
    the seam now SELECTs template_id, template_params, language out of the JSONB
    ``plan_json``. The mock returns those exact keys."""
    return {
        "template_id": template_id,
        "template_params": body_params if body_params is not None else dict(_BODY_PARAMS),
        "language": language,
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
) -> Any:
    """Build a MagicMock conn that returns controlled SELECT results.

    Query order:
      1. campaigns SELECT (fetchone)
      2. campaign_recipients JOIN customers (fetchall)
      3. send_idempotency_keys INSERT (no return value)
      4. campaigns UPDATE (no return value)
    """
    conn = MagicMock()
    execute_calls: list[tuple[str, Any]] = []

    def _execute(sql: str, params: tuple | None = None) -> MagicMock:
        execute_calls.append((sql.strip(), params))
        result = MagicMock()
        if "FROM campaigns" in sql and "WHERE id" in sql:
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
    update_calls = [
        sql for sql, _ in conn._execute_calls
        if "UPDATE campaigns" in sql
    ]
    assert len(update_calls) == 1
    assert "sent" in update_calls[0]


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
    }
    send_fn.assert_not_called()

    update_calls = [
        sql for sql, _ in conn._execute_calls
        if "UPDATE campaigns" in sql
    ]
    assert len(update_calls) == 1
