"""VT-611 pre-work #1 — the Team-Manager loop's owner-notification composer.

Mirrors ``test_campaign_outcome.py``'s own shape: pure composer-honesty tests (no DB), then
mocked-seam tests for the wiring function (``task_store``/``twilio_send``/``owner_notification``/
``freeform_acks`` all monkeypatched at their defining module — no live network, no live DB; the
loop's OWN DB-backed settle-path tests in ``test_workflow.py`` already prove the CALL into this
module happens after a real settle, on real durable state).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.owner_surface import task_outcome as to  # noqa: E402


# ----------------------------- pure: composer honesty (EN) -------------------------------
def test_compose_completed_with_effect_states_done_en() -> None:
    body = to.compose_task_outcome_message("completed_with_effect", "send the winback message", locale="en")
    assert "Done" in body
    assert "send the winback message" in body
    assert "declined" not in body.lower()
    assert "no action" not in body.lower()


def test_compose_completed_with_effect_no_objective_degrades_gracefully_en() -> None:
    body = to.compose_task_outcome_message("completed_with_effect", "", locale="en")
    assert "Done" in body
    assert "None" not in body  # empty objective never renders as the string "None"


def test_compose_completed_no_action_never_claims_an_effect_en() -> None:
    body = to.compose_task_outcome_message("completed_no_action", "check the refund status", locale="en")
    assert "no action was needed" in body
    assert "Done" not in body
    assert "declined" not in body.lower()


def test_compose_cancelled_must_read_as_a_decline_en() -> None:
    body = to.compose_task_outcome_message("cancelled", "send a 20% discount campaign", locale="en")
    assert "declined" in body.lower()
    assert "Done" not in body
    assert "no action was needed" not in body


def test_compose_cancelled_no_objective_still_declines_en() -> None:
    body = to.compose_task_outcome_message("cancelled", "", locale="en")
    assert "declined" in body.lower()


# ----------------------------- pure: composer (HI) --------------------------------------
def test_compose_cancelled_hi_says_declined() -> None:
    body = to.compose_task_outcome_message("cancelled", "बिक्री अभियान", locale="hi")
    assert "अस्वीकृत" in body  # "declined" — the bilingual honesty pin
    assert "बिक्री अभियान" in body


def test_compose_completed_with_effect_hi() -> None:
    body = to.compose_task_outcome_message("completed_with_effect", "रिफंड भेजें", locale="hi")
    assert "हो गया" in body
    assert "रिफंड भेजें" in body


def test_compose_completed_no_action_hi() -> None:
    body = to.compose_task_outcome_message("completed_no_action", "", locale="hi")
    assert "कोई कार्रवाई की ज़रूरत नहीं" in body


def test_compose_unknown_locale_falls_back_to_en() -> None:
    assert to.compose_task_outcome_message(
        "completed_with_effect", "x", locale="xx"
    ) == to.compose_task_outcome_message("completed_with_effect", "x", locale="en")


# ----------------------------- pure: _extract_objective_text -----------------------------
def test_extract_objective_text_reads_the_redacted_text() -> None:
    task = {"objective": {"objective": "handle the refund request", "schema_version": 1}}
    assert to._extract_objective_text(task) == "handle the refund request"


@pytest.mark.parametrize("objective", [None, {}, {"objective": None}, "not-a-dict"])
def test_extract_objective_text_defensive_on_unexpected_shapes(objective) -> None:
    assert to._extract_objective_text({"objective": objective}) == ""


# ----------------------------- wiring: maybe_notify_owner_of_task_outcome ----------------
def _patch(monkeypatch, *, task: dict, send_result="SM_OUT", send_raises: Exception | None = None):
    """Patch every seam maybe_notify_owner_of_task_outcome touches; return a captures dict."""
    seen: dict = {"ledger": [], "flips": [], "alerts": []}

    monkeypatch.setattr("orchestrator.manager.task_store.get_task", lambda tid, taskid: dict(task))

    def _flip(tid, taskid, status, *, expected_from=None):
        seen["flips"].append((status, expected_from))
        task["owner_notification_status"] = status  # mutate the same dict the mock get_task closed over
        return True

    monkeypatch.setattr("orchestrator.manager.task_store.set_owner_notification_status", _flip)
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale", lambda t: "en"
    )

    def _send(body, phone, **kw):
        if send_raises is not None:
            raise send_raises
        seen["body"] = body
        seen["phone"] = phone
        seen["send_kwargs"] = kw
        return send_result

    monkeypatch.setattr("orchestrator.utils.twilio_send.send_freeform_message", _send)
    monkeypatch.setattr(
        "orchestrator.owner_surface.owner_notification.record_owner_notification",
        lambda tid, label, sid, **kw: seen["ledger"].append((label, sid, kw)),
    )
    monkeypatch.setattr(
        "orchestrator.alerts.dispatch.dispatch_alert", lambda trig: seen["alerts"].append(trig)
    )
    return seen


def _pending_task(outcome: str, objective: str = "handle it") -> dict:
    return {
        "id": str(uuid4()),
        "objective": {"objective": objective},
        "terminal_outcome": outcome,
        "owner_notification_status": "pending",
    }


def test_notify_completed_with_effect_sends_flips_delivered_and_records_ledger(monkeypatch) -> None:
    task = _pending_task("completed_with_effect", "send the winback campaign")
    seen = _patch(monkeypatch, task=task)
    tenant_id, task_id = uuid4(), uuid4()

    sent = to.maybe_notify_owner_of_task_outcome(tenant_id, task_id, recipient_phone="+919811111111")

    assert sent is True
    assert "Done" in seen["body"] and "send the winback campaign" in seen["body"]
    assert seen["phone"] == "+919811111111"
    assert seen["flips"] == [("delivered", ("pending",))]
    assert seen["ledger"] == [("task_outcome_report", "SM_OUT", {"run_id": task_id})]
    assert seen["alerts"] == []


def test_notify_cancelled_message_class_reads_as_declined(monkeypatch) -> None:
    task = _pending_task("cancelled", "20% discount campaign")
    seen = _patch(monkeypatch, task=task)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is True
    assert "declined" in seen["body"].lower()
    assert seen["flips"] == [("delivered", ("pending",))]


def test_notify_completed_no_action_message_class_never_claims_effect(monkeypatch) -> None:
    task = _pending_task("completed_no_action", "check refund status")
    seen = _patch(monkeypatch, task=task)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is True
    assert "no action was needed" in seen["body"]
    assert "Done" not in seen["body"]


def test_notify_is_idempotent_already_delivered_no_op(monkeypatch) -> None:
    """The dedup: owner_notification_status != 'pending' is a clean no-op — no send attempted,
    no second flip, no double-ledger row (a re-run of the same DBOS step on replay/retry)."""
    task = _pending_task("completed_with_effect")
    task["owner_notification_status"] = "delivered"  # already handled
    seen = _patch(monkeypatch, task=task)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is False
    assert "body" not in seen
    assert seen["flips"] == []
    assert seen["ledger"] == []


def test_notify_unhandled_terminal_outcome_is_out_of_scope_no_send(monkeypatch) -> None:
    """'failed'/'escalated' never actually reach 'pending' in production (they settle 'blocked'
    instead) — this is the defensive scope fence, never exercised for real, still pinned."""
    task = _pending_task("failed")
    seen = _patch(monkeypatch, task=task)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is False
    assert "body" not in seen
    assert seen["flips"] == []


def test_notify_no_phone_defers_leaves_pending(monkeypatch) -> None:
    task = _pending_task("completed_with_effect")
    seen = _patch(monkeypatch, task=task)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone=None)

    assert sent is False
    assert "body" not in seen
    assert seen["flips"] == []  # left 'pending' — deferred, not failed
    assert task["owner_notification_status"] == "pending"


def test_notify_window_closed_defers_leaves_pending_never_fabricates_a_send(monkeypatch) -> None:
    """The freeform-vs-template fork's window-closed branch: NEVER a fabricated content SID,
    NEVER a dishonest 'delivered' — deferred (left 'pending'), no alert (this is not a failure,
    it is an expected out-of-window defer)."""
    exc = RuntimeError("window closed")
    exc.code = 63016  # type: ignore[attr-defined]
    task = _pending_task("cancelled")
    seen = _patch(monkeypatch, task=task, send_raises=exc)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is False
    assert seen["flips"] == []
    assert seen["ledger"] == []
    assert seen["alerts"] == []
    assert task["owner_notification_status"] == "pending"


def test_notify_send_failure_flips_failed_and_alerts_fail_soft(monkeypatch) -> None:
    """A DEFINITIVE (non-window) send failure is NOT deferred — it flips 'failed' and fires the
    outbound_failure alert (an un-notified owner must be surfaced), but never raises (fail-soft:
    this runs right after the settle it reports on and must never unwind it)."""
    exc = RuntimeError("twilio 500")
    exc.code = 20500  # type: ignore[attr-defined]  # NOT the window-closed code
    task = _pending_task("completed_with_effect")
    seen = _patch(monkeypatch, task=task, send_raises=exc)

    sent = to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+919811111111")

    assert sent is False
    assert seen["flips"] == [("failed", ("pending",))]
    assert len(seen["alerts"]) == 1
    assert seen["alerts"][0].trigger_kind == "outbound_failure"


def test_notify_task_not_found_is_a_no_op(monkeypatch) -> None:
    monkeypatch.setattr("orchestrator.manager.task_store.get_task", lambda tid, taskid: None)
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.send_freeform_message",
        lambda *a, **kw: pytest.fail("must not send when the task cannot be loaded"),
    )
    assert to.maybe_notify_owner_of_task_outcome(uuid4(), uuid4(), recipient_phone="+91981") is False
