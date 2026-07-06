"""VT-562 — per-run campaign outcome report to the owner (composer + resume-wiring seam)."""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.owner_surface import campaign_outcome as co  # noqa: E402


# ----------------------------- pure: composer honesty (EN) -------------------------------
def test_compose_sent_only_en() -> None:
    body = co.compose_campaign_outcome_message({"sent": 40}, locale="en")
    assert "40 customers" in body
    assert "delivered" not in body.lower()  # sent == dispatched, never "delivered"


def test_compose_sent_singular_en() -> None:
    body = co.compose_campaign_outcome_message({"sent": 1}, locale="en")
    assert "1 customer." in body  # singular agreement, not "1 customers"


def test_compose_all_axes_stated_en() -> None:
    body = co.compose_campaign_outcome_message(
        {
            "sent": 10,
            "skipped_opt_out": 3,
            "skipped_complaint_freeze": 2,
            "failed": 1,
            "killed": 4,
        },
        locale="en",
    )
    # Every non-zero axis is surfaced, never hidden.
    assert "10 customers" in body
    assert "3 customers were" in body and "opted out" in body
    assert "2 customers were" in body and "complaint hold" in body
    assert "1 message" in body and "error" in body
    assert "4 customers were" in body and "stopped" in body


def test_compose_zero_sent_stated_en() -> None:
    body = co.compose_campaign_outcome_message(
        {"sent": 0, "skipped_opt_out": 5}, locale="en"
    )
    assert "couldn't send it to any customers" in body
    assert "5 customers were" in body and "opted out" in body


def test_compose_killed_only_en() -> None:
    # A campaign killed before any send (pre-gate) reports killed count with zero sent.
    body = co.compose_campaign_outcome_message(
        {"killed": 12, "pre_gate_blocked": 1}, locale="en"
    )
    assert "any customers" in body  # zero-sent framing
    assert "12 customers were" in body and "stopped" in body


def test_compose_zero_hidden_when_zero_en() -> None:
    body = co.compose_campaign_outcome_message(
        {"sent": 7, "skipped_opt_out": 0, "failed": 0, "killed": 0}, locale="en"
    )
    assert "7 customers" in body
    assert "skipped" not in body and "error" not in body and "stopped" not in body


# ----------------------------- pure: composer (HI) --------------------------------------
def test_compose_bilingual_hi() -> None:
    body = co.compose_campaign_outcome_message(
        {"sent": 8, "skipped_opt_out": 2}, locale="hi"
    )
    assert "8 ग्राहकों" in body  # Latin numerals kept in HI copy
    assert "2" in body and "छोड़ा गया" in body


def test_compose_unknown_locale_falls_back_to_en() -> None:
    assert co.compose_campaign_outcome_message(
        {"sent": 3}, locale="xx"
    ) == co.compose_campaign_outcome_message({"sent": 3}, locale="en")


# ----------------------------- pure: summary_has_activity gate --------------------------
@pytest.mark.parametrize(
    "summary,expected",
    [
        (None, False),
        ({}, False),
        ({"sent": 0, "failed": 0, "killed": 0}, False),
        ({"status": "held_by_run_control", "control_type": "pause"}, False),  # HOLD, no send
        ({"sent": 1}, True),
        ({"killed": 3}, True),
        ({"skipped_opt_out": 2}, True),
    ],
)
def test_summary_has_activity(summary, expected) -> None:
    assert co.summary_has_activity(summary) is expected


# ----------------------------- wiring: maybe_report_campaign_outcome ---------------------
def _patch_send(monkeypatch) -> dict:
    """Patch the send seam + ledger; return a captures dict."""
    seen: dict = {"ledger": []}
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.send_freeform_message",
        lambda body, phone, **kw: (seen.update(body=body, phone=phone, **kw), "SM_OUT")[1],
    )
    monkeypatch.setattr(
        "orchestrator.owner_surface.owner_notification.record_owner_notification",
        lambda tid, label, sid, **kw: seen["ledger"].append((label, sid, kw)),
    )
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale", lambda t: "en"
    )
    return seen


def test_maybe_report_sends_honest_counts_and_records_ledger(monkeypatch) -> None:
    seen = _patch_send(monkeypatch)
    state = {"campaign_execution_summary": {"sent": 20, "skipped_opt_out": 4}}
    run_id = uuid4()
    tenant_id = uuid4()

    sent = co.maybe_report_campaign_outcome(
        tenant_id, state, run_id=run_id, recipient_phone="+919811111111"
    )

    assert sent is True
    assert "20 customers" in seen["body"] and "4 customers were" in seen["body"]
    assert "delivered" not in seen["body"].lower()  # honesty: dispatched, not delivered
    assert seen["phone"] == "+919811111111"
    # Auditable: recorded in the owner_notifications ledger under the report label + run_id.
    assert seen["ledger"] == [("campaign_outcome_report", "SM_OUT", {"run_id": run_id})]
    # VT-611 Package H0: tenant_id/surface must reach send_freeform_message so this outcome report
    # lands in the lifetime conversation_log (was bare -> _record_owner_conversation_turn no-op'd).
    assert seen["tenant_id"] == tenant_id
    assert seen["surface"] == "manager"


def test_maybe_report_no_summary_skips(monkeypatch) -> None:
    seen = _patch_send(monkeypatch)
    # A rejected / needs_changes resume ran no campaign → no summary → no send.
    assert co.maybe_report_campaign_outcome(uuid4(), {}, recipient_phone="+91981") is False
    assert "body" not in seen and seen["ledger"] == []


def test_maybe_report_held_by_run_control_skips(monkeypatch) -> None:
    seen = _patch_send(monkeypatch)
    state = {"campaign_execution_summary": {"status": "held_by_run_control", "control_type": "pause"}}
    assert co.maybe_report_campaign_outcome(uuid4(), state, recipient_phone="+91981") is False
    assert "body" not in seen


def test_maybe_report_no_phone_skips(monkeypatch) -> None:
    seen = _patch_send(monkeypatch)
    monkeypatch.setattr(co, "_resolve_owner_phone", lambda t: None)
    state = {"campaign_execution_summary": {"sent": 5}}
    assert co.maybe_report_campaign_outcome(uuid4(), state) is False
    assert "body" not in seen


def test_maybe_report_send_failure_is_fail_soft_and_alerts(monkeypatch) -> None:
    """A send failure must NEVER raise (so the caller's close_webhook_run still runs) and MUST
    fire the outbound_failure alert (an un-notified owner is surfaced)."""
    alerts: list = []

    def _boom(body, phone, **kw):  # noqa: ANN001
        exc = RuntimeError("window closed")
        exc.code = 63016
        raise exc

    monkeypatch.setattr("orchestrator.utils.twilio_send.send_freeform_message", _boom)
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale", lambda t: "en"
    )
    monkeypatch.setattr(
        "orchestrator.alerts.dispatch.dispatch_alert", lambda trig: alerts.append(trig)
    )
    state = {"campaign_execution_summary": {"sent": 9}}

    # No raise (fail-soft), returns False.
    assert co.maybe_report_campaign_outcome(
        uuid4(), state, recipient_phone="+919811111111"
    ) is False
    # The outbound_failure alert fired.
    assert len(alerts) == 1
    assert alerts[0].trigger_kind == "outbound_failure"
