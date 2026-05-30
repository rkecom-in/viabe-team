"""VT-47 — unit tests for the owner-approval resume path.

Classification -> decision mapping + the Pillar-7 "never guess approval"
guarantee (other / low-confidence -> no resume). mark_approval_resolved SQL
shape. No live DB, no live Anthropic (classify_fn is stubbed).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("langgraph")

from orchestrator.agent.approval_resume import (  # noqa: E402
    mark_approval_resolved,
    resolve_decision_from_reply,
)


def _classify(classification, confidence=0.95):
    def _fn(text):
        return SimpleNamespace(classification=classification, confidence=confidence)

    return _fn


def test_approval_maps_to_approved():
    assert resolve_decision_from_reply("haan", classify_fn=_classify("approval")) == "approved"


def test_rejection_maps_to_rejected():
    assert resolve_decision_from_reply("nahi", classify_fn=_classify("rejection")) == "rejected"


def test_question_and_feedback_map_to_needs_changes():
    assert resolve_decision_from_reply("?", classify_fn=_classify("question")) == "needs_changes"
    assert resolve_decision_from_reply("hmm", classify_fn=_classify("feedback")) == "needs_changes"


def test_other_does_not_resume():
    """Pillar 7: a non-decision reply does NOT resolve the gate (no guessing)."""
    assert resolve_decision_from_reply("good morning", classify_fn=_classify("other")) is None


def test_low_confidence_does_not_resume():
    """Pillar 7: a low-confidence approval is not authoritative -> no resume."""
    assert resolve_decision_from_reply(
        "maybe ok", classify_fn=_classify("approval", confidence=0.3)
    ) is None


class _CaptureConn:
    def __init__(self):
        self.calls: list[tuple] = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))


def test_mark_resolved_sets_decision_status_and_guards_unresolved():
    conn = _CaptureConn()
    aid = uuid4()
    mark_approval_resolved(conn, aid, "approved", owner_message_sid="SMabc")
    sql, params = conn.calls[0]
    assert "UPDATE pending_approvals" in sql
    assert "resolved_at = now()" in sql
    assert "WHERE id = %s AND resolved_at IS NULL" in sql  # idempotent guard
    assert params[0] == "approved"  # decision
    assert params[1] == "approved"  # status (approved -> approved)
    assert params[2] == "SMabc"
    assert params[3] == str(aid)


def test_needs_changes_collapses_status_to_rejected():
    conn = _CaptureConn()
    mark_approval_resolved(conn, uuid4(), "needs_changes")
    _, params = conn.calls[0]
    assert params[0] == "needs_changes"  # raw decision verb retained
    assert params[1] == "rejected"       # status collapses to non-approval


def test_timeout_decision_maps_to_timed_out_status():
    conn = _CaptureConn()
    mark_approval_resolved(conn, uuid4(), "timeout")
    _, params = conn.calls[0]
    assert params[0] == "timeout"
    assert params[1] == "timed_out"
