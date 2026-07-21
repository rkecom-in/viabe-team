"""VT-683 P2a — owner_comms_queue CRUD + POINT A (decision clock starts at delivery).

Dep-less: a FakeConn/FakePool captures (sql, params) so we pin the SQL shape + the point-A rule
(mark_delivered sets a decision deadline for kind='approval', NEVER for notice/report) without a
live Postgres.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("psycopg")

from orchestrator.owner_surface import owner_comms_queue as q  # noqa: E402


class _FakeCur:
    def __init__(self, rows: list[Any] | None = None, rowcount: int = 0) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[Any] | None = None, rowcount: int = 0) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._rows = rows
        self._rowcount = rowcount

    def execute(self, sql: str, params: Any = None) -> _FakeCur:
        self.calls.append((sql, params))
        return _FakeCur(self._rows, self._rowcount)


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connection(self):  # noqa: ANN201 — context manager
        conn = self._conn

        class _CM:
            def __enter__(self_inner):  # noqa: N805
                return conn

            def __exit__(self_inner, *exc):  # noqa: N805
                return False

        return _CM()


_TID = "22222222-2222-2222-2222-222222222222"


def test_enqueue_default_priority_by_kind() -> None:
    conn = _FakeConn()
    q.enqueue(_TID, kind="approval", payload={"text": "approve?"}, conn=conn)
    sql, params = conn.calls[0]
    assert "INSERT INTO owner_comms_queue" in sql
    assert params[2] == "approval"
    assert params[4] == q.DEFAULT_PRIORITY["approval"] == 100  # approvals rank highest


def test_enqueue_report_ranks_below_approval() -> None:
    conn = _FakeConn()
    q.enqueue(_TID, kind="report", payload={}, conn=conn)
    assert conn.calls[0][1][4] == q.DEFAULT_PRIORITY["report"] == 50


def test_next_deliverable_returns_row_dict() -> None:
    conn = _FakeConn(rows=[("id-1", "approval", {"text": "x"}, {"id": "a"}, 100, "t")])
    out = q.next_deliverable(_TID, conn=conn)
    assert out is not None and out["kind"] == "approval" and out["id"] == "id-1"
    # highest priority first, oldest first, only queued
    assert "ORDER BY priority DESC, queued_at ASC" in conn.calls[0][0]
    assert "status = 'queued'" in conn.calls[0][0]


def test_next_deliverable_none_when_empty() -> None:
    assert q.next_deliverable(_TID, conn=_FakeConn(rows=[])) is None


def test_mark_delivered_approval_SETS_deadline() -> None:
    """POINT A: a DELIVERED approval gets decision_deadline_at = now + TTL (clock starts here)."""
    conn = _FakeConn()
    q.mark_delivered(_TID, "item-1", kind="approval", message_sid="SM1",
                     decision_ttl=timedelta(hours=48), conn=conn)
    sql, params = conn.calls[0]
    assert "decision_deadline_at = CASE" in sql
    # the ttl-seconds param is NON-null for an approval → the CASE sets a real deadline.
    assert params[1] == 48 * 3600
    assert params[2] == 48 * 3600


def test_mark_delivered_notice_NO_deadline() -> None:
    """A non-approval item is delivered but carries NO decision deadline (nothing to time out)."""
    conn = _FakeConn()
    q.mark_delivered(_TID, "item-2", kind="notice", message_sid="SM2", conn=conn)
    params = conn.calls[0][1]
    assert params[1] is None and params[2] is None  # ttl null → CASE yields NULL deadline


def test_drop_stale_marks_dropped_not_deleted() -> None:
    conn = _FakeConn(rowcount=3)
    n = q.drop_stale(pool=_FakePool(conn))
    assert n == 3
    sql = conn.calls[0][0]
    assert "status = 'dropped'" in sql and "dropped_reason = 'max_age'" in sql
    assert "status = 'queued'" in sql  # only drops still-undelivered items


def test_overdue_delivered_approvals_reads_past_deadline() -> None:
    conn = _FakeConn(rows=[("q1", _TID, {"kind": "pending_approval", "id": "pa1"})])
    out = q.overdue_delivered_approvals(pool=_FakePool(conn))
    assert out and out[0]["decision_ref"]["id"] == "pa1"
    sql = conn.calls[0][0]
    assert "status = 'delivered'" in sql and "kind = 'approval'" in sql
    assert "decision_deadline_at < now()" in sql
