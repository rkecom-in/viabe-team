"""VT-683 P2b — drain_one: session-gated, one-item, freeform delivery + point-A mark-delivered.

Fully monkeypatched (session_open / queue / freeform sender) so it's dep-less and deterministic.
"""

from __future__ import annotations

from typing import Any

import pytest

# _wire monkeypatches session_window, which imports tenant_connection -> psycopg at module top;
# guard so the dep-less smoke skips cleanly (behavior is proven with the full deps present).
pytest.importorskip("psycopg")

from orchestrator.owner_surface import owner_comms_drainer as d  # noqa: E402


_TID = "22222222-2222-2222-2222-222222222222"


def _wire(
    monkeypatch,
    *,
    open_session: bool,
    item: dict[str, Any] | None,
    send_result: dict[str, Any],
    open_approval: dict[str, Any] | None = None,
):
    calls: dict[str, Any] = {"marked": None, "sent": None, "dropped": None}
    import orchestrator.owner_surface.session_window as sw
    import orchestrator.owner_surface.owner_comms_queue as q
    import orchestrator.direct_handlers._freeform_first as ff
    from orchestrator.db.wrappers import PendingApprovalsWrapper

    monkeypatch.setattr(sw, "session_open", lambda _t: open_session)
    monkeypatch.setattr(q, "next_deliverable", lambda _t, **k: item)
    # VT-683 P2c: the resolved-elsewhere guard reads the underlying approval row.
    monkeypatch.setattr(
        PendingApprovalsWrapper, "get_open_by_id", lambda self, _t, _a, **k: open_approval
    )

    def _mark(tenant_id, item_id, *, kind, message_sid, **k):
        calls["marked"] = {"item_id": item_id, "kind": kind, "message_sid": message_sid}

    monkeypatch.setattr(q, "mark_delivered", _mark)

    def _drop(tenant_id, item_id, *, reason, **k):
        calls["dropped"] = {"item_id": item_id, "reason": reason}
        return 1

    monkeypatch.setattr(q, "drop_item", _drop)

    def _send(tenant_id, body, recipient, *, fallback_template, fallback_params=None):
        calls["sent"] = {"body": body, "recipient": recipient, "fallback_template": fallback_template}
        return send_result

    monkeypatch.setattr(ff, "send_freeform_first", _send)
    return calls


def test_drains_one_approval_and_marks_delivered(monkeypatch) -> None:
    item = {"id": "q1", "kind": "approval",
            "decision_ref": {"kind": "pending_approval", "id": "aaaa-approval-id"},
            "payload": {"text_en": "Approve the festival campaign to 8 customers?",
                        "fallback_template": "team_agent_draft_approval"}}
    calls = _wire(monkeypatch, open_session=True, item=item,
                  send_result={"success": True, "channel": "freeform_session", "message_sid": "SM9"},
                  open_approval={"id": "aaaa-approval-id", "approval_type": "campaign_send"})
    out = d.drain_one(_TID, "+919999999999", lang="en")
    assert out and out["delivered"] and out["item_id"] == "q1"
    # POINT A: mark_delivered fired with the approval kind → its decision clock started.
    assert calls["marked"] == {"item_id": "q1", "kind": "approval", "message_sid": "SM9"}
    assert "festival campaign" in calls["sent"]["body"]


def test_approval_item_dropped_when_underlying_resolved(monkeypatch) -> None:
    """VT-683 P2c — an approval item whose pending_approvals row is no longer open must be
    DROPPED, never delivered (the owner would tap a button against an unresolvable row — the
    VT-615 dropped-campaign shape)."""
    item = {"id": "q5", "kind": "approval",
            "decision_ref": {"kind": "pending_approval", "id": "gone-approval-id"},
            "payload": {"text_en": "Approve?"}}
    calls = _wire(monkeypatch, open_session=True, item=item,
                  send_result={"success": True, "message_sid": "SM1"},
                  open_approval=None)  # underlying row resolved/deleted
    assert d.drain_one(_TID, "+919999999999") is None
    assert calls["sent"] is None, "a stale approval ask must never reach the owner"
    assert calls["marked"] is None
    assert calls["dropped"] == {"item_id": "q5", "reason": "resolved_elsewhere"}


def test_approval_item_without_decision_ref_dropped(monkeypatch) -> None:
    """A malformed approval item (no decision_ref) can never resolve — drop, don't deliver."""
    item = {"id": "q6", "kind": "approval", "payload": {"text_en": "Approve?"}}
    calls = _wire(monkeypatch, open_session=True, item=item,
                  send_result={"success": True, "message_sid": "SM1"},
                  open_approval={"id": "whatever"})  # would be open, but the item has no ref
    assert d.drain_one(_TID, "+919999999999") is None
    assert calls["sent"] is None
    assert calls["dropped"] == {"item_id": "q6", "reason": "resolved_elsewhere"}


def test_deferred_task_outcome_flips_task_on_delivery(monkeypatch) -> None:
    """VT-683 P2c — delivering a queued deferred task-outcome ('report' with manager_task_id)
    flips the manager_task via mark_deferred_outcome_delivered (fail-soft)."""
    item = {"id": "q7", "kind": "report",
            "payload": {"text": "Done: winback sent to 5 customers.",
                        "manager_task_id": "33333333-3333-3333-3333-333333333333"}}
    calls = _wire(monkeypatch, open_session=True, item=item,
                  send_result={"success": True, "message_sid": "SM77"})
    flipped: dict[str, Any] = {}
    import orchestrator.owner_surface.task_outcome as to

    def _flip(tenant_id, task_id, message_sid):
        flipped.update({"task_id": task_id, "sid": message_sid})

    monkeypatch.setattr(to, "mark_deferred_outcome_delivered", _flip)
    out = d.drain_one(_TID, "+919999999999", lang="en")
    assert out and out["delivered"]
    assert flipped == {"task_id": "33333333-3333-3333-3333-333333333333", "sid": "SM77"}
    assert calls["marked"]["item_id"] == "q7"


def test_noop_when_session_closed(monkeypatch) -> None:
    calls = _wire(monkeypatch, open_session=False, item={"id": "q1", "kind": "notice", "payload": {"text_en": "hi"}},
                  send_result={"success": True})
    assert d.drain_one(_TID, "+919999999999") is None
    assert calls["sent"] is None and calls["marked"] is None  # never sends outside the window


def test_noop_when_queue_empty(monkeypatch) -> None:
    calls = _wire(monkeypatch, open_session=True, item=None, send_result={"success": True})
    assert d.drain_one(_TID, "+919999999999") is None
    assert calls["sent"] is None


def test_prefers_owner_language(monkeypatch) -> None:
    item = {"id": "q2", "kind": "notice",
            "payload": {"text_en": "English body", "text_hi": "हिंदी बॉडी"}}
    calls = _wire(monkeypatch, open_session=True, item=item,
                  send_result={"success": True, "message_sid": "SM1"})
    d.drain_one(_TID, "+919999999999", lang="hi")
    assert calls["sent"]["body"] == "हिंदी बॉडी"


def test_empty_body_marked_delivered_not_sent(monkeypatch) -> None:
    item = {"id": "q3", "kind": "notice", "payload": {}}  # no renderable body
    calls = _wire(monkeypatch, open_session=True, item=item, send_result={"success": True})
    assert d.drain_one(_TID, "+919999999999") is None
    assert calls["sent"] is None  # never sends an empty message
    assert calls["marked"]["item_id"] == "q3"  # but clears the queue head (no wedge)


def test_never_raises_on_send_failure(monkeypatch) -> None:
    item = {"id": "q4", "kind": "notice", "payload": {"text_en": "x"}}
    import orchestrator.owner_surface.session_window as sw
    import orchestrator.owner_surface.owner_comms_queue as q
    import orchestrator.direct_handlers._freeform_first as ff

    monkeypatch.setattr(sw, "session_open", lambda _t: True)
    monkeypatch.setattr(q, "next_deliverable", lambda _t, **k: item)
    monkeypatch.setattr(q, "mark_delivered", lambda *a, **k: None)

    def _boom(*a, **k):
        raise RuntimeError("twilio down")

    monkeypatch.setattr(ff, "send_freeform_first", _boom)
    assert d.drain_one(_TID, "+919999999999") is None  # swallowed, no raise


def test_approval_item_dropped_when_clock_already_running(monkeypatch) -> None:
    """POINT A duplicate guard: a running decision clock (timeout_at non-NULL) proves the ask
    already reached the owner — a straggler ledger item (fail-soft mark_delivered missed) must
    be dropped, never re-sent as a duplicate ask."""
    from datetime import datetime, timezone

    item = {"id": "q8", "kind": "approval",
            "decision_ref": {"kind": "pending_approval", "id": "delivered-approval-id"},
            "payload": {"text_en": "Approve?"}}
    calls = _wire(monkeypatch, open_session=True, item=item,
                  send_result={"success": True, "message_sid": "SM1"},
                  open_approval={"id": "delivered-approval-id", "approval_type": "campaign_send",
                                 "timeout_at": datetime(2026, 7, 24, tzinfo=timezone.utc)})
    assert d.drain_one(_TID, "+919999999999") is None
    assert calls["sent"] is None, "a delivered ask must never go out twice"
    assert calls["dropped"] == {"item_id": "q8", "reason": "already_delivered"}
