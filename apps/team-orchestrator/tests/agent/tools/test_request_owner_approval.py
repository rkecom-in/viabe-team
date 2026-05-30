"""VT-47 — unit tests for the request_owner_approval pause primitive + node.

These exercise the side-effect ordering + idempotency + the Pillar-7
no-orphan-pause guarantee WITHOUT a live DB (a fake connection captures the
SQL) and WITHOUT langgraph's pregel loop (the node is exercised via the
arm_pause_request seam + a fake interrupt). The real pause->resume cycle over
a live checkpointer + Postgres lives in the canary + the integration test.

langgraph is a heavy dep; importorskip guards the dep-less smoke job.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("langgraph")

from orchestrator.agent.tools.request_owner_approval import (  # noqa: E402
    APPROVAL_TEMPLATE_NAME,
    PauseRequestResult,
    RequestOwnerApprovalInput,
    arm_pause_request,
)


class _FakeCursorResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    """Captures execute() calls. ``open_row`` is what _find_open_approval sees."""

    def __init__(self, *, open_row=None, owner_phone="+919811112222"):
        self._open_row = open_row
        self._owner_phone = owner_phone
        self.inserts: list[tuple] = []
        self.queries: list[str] = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        s = " ".join(sql.split())
        if "FROM pending_approvals" in s and "resolved_at IS NULL" in s:
            return _FakeCursorResult(self._open_row)
        if "owner_phone" in s and "FROM tenants" in s:
            return _FakeCursorResult({"owner_phone": self._owner_phone, "whatsapp_number": None})
        if s.startswith("INSERT INTO pending_approvals"):
            self.inserts.append((sql, params))
            return _FakeCursorResult(None)
        return _FakeCursorResult(None)


class _ConnFactory:
    def __init__(self, conn):
        self._conn = conn

    def __call__(self, tenant_id):
        return self

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


def _input(**over):
    base = dict(
        tenant_id=uuid4(),
        run_id=uuid4(),
        approval_type="campaign_send",
        summary="Approve send to 3 customers?",
        details={"cohort_size": 3},
        template_params={},
        timeout_hours=48,
    )
    base.update(over)
    return RequestOwnerApprovalInput(**base)


class _OkSend:
    def __init__(self):
        self.calls = []

    def __call__(self, tenant_id, template_name, params, *, recipient_phone=None):
        self.calls.append((template_name, recipient_phone))
        from types import SimpleNamespace

        return SimpleNamespace(success=True, message_sid="SM" + "0" * 32)


def test_arm_sends_template_then_inserts_row():
    """Happy path: template send then INSERT pending_approvals (decision NULL)."""
    conn = _FakeConn()
    send = _OkSend()
    res = arm_pause_request(
        _input(), conn_factory=_ConnFactory(conn), send_fn=send
    )
    assert res.status == "armed"
    assert res.approval_id is not None
    # Template sent to the OWNER phone, by the canonical template name.
    assert send.calls == [(APPROVAL_TEMPLATE_NAME, "+919811112222")]
    # Exactly one INSERT into pending_approvals.
    assert len(conn.inserts) == 1


def test_send_failure_writes_no_orphan_row():
    """Pillar 7: template send failure -> error envelope, NO pending_approvals
    row (so the caller will NOT interrupt — no stuck/orphan pause)."""
    conn = _FakeConn()

    def failing_send(tenant_id, template_name, params, *, recipient_phone=None):
        from types import SimpleNamespace

        return SimpleNamespace(success=False, error_code="boom", error_message="x")

    res = arm_pause_request(
        _input(), conn_factory=_ConnFactory(conn), send_fn=failing_send
    )
    assert res.status == "error"
    assert res.error is not None
    assert len(conn.inserts) == 0, "no pending_approvals row on send failure"


def test_send_raises_writes_no_orphan_row():
    """A raised exception from the sender is caught -> error envelope, no row."""
    conn = _FakeConn()

    def raising_send(tenant_id, template_name, params, *, recipient_phone=None):
        raise RuntimeError("twilio down")

    res = arm_pause_request(
        _input(), conn_factory=_ConnFactory(conn), send_fn=raising_send
    )
    assert res.status == "error"
    assert len(conn.inserts) == 0


def test_idempotent_when_open_approval_exists():
    """Resume re-executes the node from its start. If an OPEN approval already
    exists, arm_pause_request must NOT re-send and NOT re-insert."""
    existing = {"id": str(uuid4()), "decision": None, "status": "pending", "resolved_at": None}
    conn = _FakeConn(open_row=existing)
    send = _OkSend()
    res = arm_pause_request(
        _input(), conn_factory=_ConnFactory(conn), send_fn=send
    )
    assert res.status == "armed"
    assert str(res.approval_id) == existing["id"]
    assert send.calls == [], "must NOT re-send on resume re-execution"
    assert len(conn.inserts) == 0, "must NOT re-insert on resume re-execution"


def test_dry_run_skips_send_but_inserts():
    """dry_run (canary/CI) skips the Twilio call but still writes the row."""
    conn = _FakeConn()
    send = _OkSend()
    res = arm_pause_request(
        _input(), conn_factory=_ConnFactory(conn), send_fn=send, dry_run=True
    )
    assert res.status == "armed"
    assert send.calls == [], "dry_run must not call the sender"
    assert len(conn.inserts) == 1


def test_pause_result_typing():
    r = PauseRequestResult(status="armed", approval_id=uuid4())
    assert r.status == "armed"
