"""VT-47 — unit tests for the request_owner_approval pause primitive + node.

These exercise the side-effect ordering + idempotency + the Pillar-7
no-orphan-pause guarantee WITHOUT a live DB (a fake connection captures the
SQL) and WITHOUT langgraph's pregel loop (the node is exercised via the
arm_pause_request seam + a fake interrupt). The real pause->resume cycle over
a live checkpointer + Postgres lives in the canary + the integration test.

langgraph is a heavy dep; importorskip guards the dep-less smoke job.
"""

from __future__ import annotations

from contextlib import nullcontext
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

    def __init__(self, *, open_row=None, owner_phone="+919811112222", insert_raises=None):
        self._open_row = open_row
        self._owner_phone = owner_phone
        self._insert_raises = insert_raises
        self.inserts: list[tuple] = []
        self.deletes: list[tuple] = []
        self.queries: list[str] = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        s = " ".join(sql.split())
        if "FROM pending_approvals" in s and "resolved_at IS NULL" in s:
            return _FakeCursorResult(self._open_row)
        if "owner_phone" in s and "FROM tenants" in s:
            return _FakeCursorResult({"owner_phone": self._owner_phone, "whatsapp_number": None})
        if s.startswith("INSERT INTO pending_approvals"):
            if self._insert_raises is not None:  # migration-128 race-loser
                raise self._insert_raises
            self.inserts.append((sql, params))
            return _FakeCursorResult(None)
        if s.startswith("DELETE FROM pending_approvals"):
            self.deletes.append((sql, params))
            return _FakeCursorResult(None)
        return _FakeCursorResult(None)

    def rollback(self):  # autocommit conn: no-op, mirrors psycopg on nothing-pending
        pass

    def cursor(self):
        # VT-514 emit_tm_audit's fail-closed insert uses `with conn.cursor() as
        # cur: cur.execute(...)` (real psycopg style), distinct from this fake's
        # direct `conn.execute(...)` callers above — a real psycopg.Connection
        # supports both. nullcontext(self) makes `cur` == this conn.
        return nullcontext(self)


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
    """Pillar 7 (VT-615 arm-then-send): the row is INSERTed first, then the template
    send fails -> the armed row is DELETEd, so NO OPEN orphan remains (the caller
    will NOT interrupt — no stuck pause). Verify insert-then-rollback, not no-insert."""
    conn = _FakeConn()

    def failing_send(tenant_id, template_name, params, *, recipient_phone=None):
        from types import SimpleNamespace

        return SimpleNamespace(success=False, error_code="boom", error_message="x")

    res = arm_pause_request(
        _input(), conn_factory=_ConnFactory(conn), send_fn=failing_send
    )
    assert res.status == "error"
    assert res.error is not None
    assert len(conn.inserts) == 1, "arm-then-send: the row IS inserted first"
    assert len(conn.deletes) == 1, "send failure must DELETE the armed row (no orphan)"


def test_send_raises_writes_no_orphan_row():
    """A raised exception from the sender is caught -> error envelope; the armed row
    is rolled back (DELETEd) so no open orphan remains."""
    conn = _FakeConn()

    def raising_send(tenant_id, template_name, params, *, recipient_phone=None):
        raise RuntimeError("twilio down")

    res = arm_pause_request(
        _input(), conn_factory=_ConnFactory(conn), send_fn=raising_send
    )
    assert res.status == "error"
    assert len(conn.inserts) == 1
    assert len(conn.deletes) == 1, "raised send must DELETE the armed row"


def test_race_loser_refuses_before_any_send():
    """VT-615 arm-then-send core: a migration-128 one-open-per-tenant race lost at the
    INSERT must refuse BEFORE any owner-facing send — no phantom template, no summary,
    so no campaign is silently dropped. This is the whole point of INSERT-first."""
    from psycopg.errors import UniqueViolation

    conn = _FakeConn(insert_raises=UniqueViolation("one open per tenant"))
    send = _OkSend()
    summary_sent: list[str] = []

    import orchestrator.owner_surface.freeform_acks as fa
    orig = fa.send_freeform_ack
    fa.send_freeform_ack = lambda *a, **k: summary_sent.append("summary")
    try:
        res = arm_pause_request(
            _input(chat_summary={"en": "plan"}),
            conn_factory=_ConnFactory(conn),
            send_fn=send,
        )
    finally:
        fa.send_freeform_ack = orig

    assert res.status == "refused"
    assert res.error is not None and res.error.code == "approval_queue_busy"
    assert send.calls == [], "race-loser must NOT send the approval template"
    assert summary_sent == [], "race-loser must NOT send the plan summary either"


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


# ---------------------------------------------------------------------------
# VT-683 P2c — in-session interactive ask + POINT A (clock at delivery)
# ---------------------------------------------------------------------------
# The pre-P2c tests above run with session_open fail-closed (no DB → False), so they
# prove the out-of-window TEMPLATE BELT byte-identically. These prove the in-window
# interactive path + the delivery-time decision clock.


def _wire_interactive(monkeypatch, *, open_session: bool, interactive_raises=None):
    """Patch the in-window seam: session_open / locale / registry / interactive sender."""
    import orchestrator.owner_surface.session_window as sw
    import orchestrator.owner_surface.freeform_acks as fa
    import orchestrator.templates_registry as tr
    import orchestrator.utils.twilio_send as ts

    sent: list[dict] = []
    monkeypatch.setattr(sw, "session_open", lambda _t: open_session)
    monkeypatch.setattr(fa, "resolve_owner_locale", lambda _t: "en")
    monkeypatch.setattr(tr, "content_sid_for", lambda name, lang="en": "HXfeedbeef" + lang)

    def _interactive(content_sid, recipient, *, content_variables=None, tenant_id=None, surface=None):
        if interactive_raises is not None:
            raise interactive_raises
        sent.append({"content_sid": content_sid, "recipient": recipient,
                     "content_variables": content_variables})
        return "SMinteractive" + "0" * 22

    monkeypatch.setattr(ts, "send_interactive_message", _interactive)
    return sent


def _clock_start_queries(conn) -> list[str]:
    return [q for q in conn.queries
            if "SET timeout_at = now() + make_interval" in " ".join(q.split())]


def test_in_window_sends_interactive_not_template(monkeypatch):
    """Session OPEN → the load-bearing ask goes out as the in-session interactive
    quick-reply (team_approval_buttons); the Meta template is NOT sent. POINT A: the
    decision clock starts after the delivery, and the arm INSERTed timeout_at NULL."""
    conn = _FakeConn()
    send = _OkSend()
    sent = _wire_interactive(monkeypatch, open_session=True)
    res = arm_pause_request(_input(), conn_factory=_ConnFactory(conn), send_fn=send)
    assert res.status == "armed"
    assert send.calls == [], "in-window: the Meta template must NOT be sent"
    assert len(sent) == 1 and sent[0]["content_sid"] == "HXfeedbeefen"
    assert sent[0]["content_variables"] == {"1": "Approve send to 3 customers?"}
    # POINT A: the INSERT carried timeout_at=None …
    insert_sql, insert_params = conn.inserts[0]
    cols = insert_sql.split("(")[1]
    assert "timeout_at" in cols
    # … and the clock started at delivery (the start_decision_clock UPDATE ran).
    assert _clock_start_queries(conn), "decision clock must start at delivery"


def test_in_window_interactive_failure_falls_back_to_template(monkeypatch):
    """Interactive send failure NEVER loses the ask — the Meta template belt fires."""
    conn = _FakeConn()
    send = _OkSend()
    _wire_interactive(monkeypatch, open_session=True, interactive_raises=RuntimeError("content 4xx"))
    res = arm_pause_request(_input(), conn_factory=_ConnFactory(conn), send_fn=send)
    assert res.status == "armed"
    assert send.calls == [(APPROVAL_TEMPLATE_NAME, "+919811112222")], "belt must fire"
    assert _clock_start_queries(conn), "clock still starts at (template) delivery"


def test_out_of_window_uses_template_belt(monkeypatch):
    """Session CLOSED → template exactly as pre-P2c; no interactive attempt."""
    conn = _FakeConn()
    send = _OkSend()
    sent = _wire_interactive(monkeypatch, open_session=False)
    res = arm_pause_request(_input(), conn_factory=_ConnFactory(conn), send_fn=send)
    assert res.status == "armed"
    assert sent == [], "closed window must never attempt an interactive session send"
    assert send.calls == [(APPROVAL_TEMPLATE_NAME, "+919811112222")]
    assert _clock_start_queries(conn), "clock starts at delivery on the belt too"


def test_send_failure_drops_ledger_record(monkeypatch):
    """Total send failure → arm rollback ALSO drops the owner_comms_queue ledger record
    (status='dropped', reason='send_failed') — no phantom queued approval survives."""
    conn = _FakeConn()
    _wire_interactive(monkeypatch, open_session=False)

    def failing_send(tenant_id, template_name, params, *, recipient_phone=None):
        from types import SimpleNamespace

        return SimpleNamespace(success=False, error_code="boom", error_message="x")

    res = arm_pause_request(_input(), conn_factory=_ConnFactory(conn), send_fn=failing_send)
    assert res.status == "error"
    assert len(conn.deletes) == 1
    drop_updates = [q for q in conn.queries
                    if "owner_comms_queue SET status = 'dropped'" in " ".join(q.split())]
    assert drop_updates, "the ledger record must be dropped on rollback"


def test_dry_run_starts_clock_at_arm(monkeypatch):
    """dry_run (canary/CI): no send, but the clock starts at arm so a synthetic row can
    never sit NULL-clocked (the sweep belt would flag it as a crash orphan)."""
    conn = _FakeConn()
    send = _OkSend()
    res = arm_pause_request(_input(), conn_factory=_ConnFactory(conn), send_fn=send, dry_run=True)
    assert res.status == "armed"
    assert send.calls == []
    assert _clock_start_queries(conn), "dry_run must start the clock at arm"


def test_approval_buttons_registry_entry_resolves():
    """The team_approval_buttons registry entry is REAL (canary-created HX pair) — the
    in-window path resolves it for both owner locales, no hardcoded SID in code."""
    from orchestrator.templates_registry import content_sid_for

    en = content_sid_for("team_approval_buttons", "en")
    hi = content_sid_for("team_approval_buttons", "hi")
    assert en and en.startswith("HX")
    assert hi and hi.startswith("HX")
    assert en != hi


def test_button_titles_resolve_deterministically_in_every_send_intent_mode(monkeypatch):
    """VT-683 P2c — the button fast-path: an EXACT team_approval_buttons title resolves
    deterministically even under TEAM_SEND_INTENT_LLM=enforce (a tap is not free text; the
    LLM gate must never own it). Free text containing a title still takes the normal paths."""
    from orchestrator.agent.approval_resume import resolve_decision_from_reply
    from orchestrator.owner_inputs import send_intent as si

    monkeypatch.setattr(si, "get_send_intent_mode", lambda: "enforce")
    monkeypatch.setattr(
        si, "decide_send_intent_enforce",
        lambda text, tenant_id=None: (_ for _ in ()).throw(AssertionError("LLM gate must not own a button tap")),
    )
    tid = uuid4()
    assert resolve_decision_from_reply("Yes, approve", tenant_id=tid, approval_type="campaign_send") == "approved"
    assert resolve_decision_from_reply("No, reject", tenant_id=tid, approval_type="campaign_send") == "rejected"
    assert resolve_decision_from_reply("हाँ, मंज़ूर है", tenant_id=tid, approval_type="campaign_send") == "approved"
    assert resolve_decision_from_reply("नहीं, रहने दो", tenant_id=tid, approval_type="campaign_send") == "rejected"


def test_button_fast_path_never_matches_free_text():
    """Full-string only: a sentence CONTAINING a title is free text, not a tap."""
    from orchestrator.owner_inputs.approval_reply import classify_button_decision

    assert classify_button_decision("Yes, approve") == "approved"
    assert classify_button_decision("  yes, approve  ") == "approved"  # trim + casefold
    assert classify_button_decision("Yes, approve the diwali one but change the copy") is None
    assert classify_button_decision("well ok — no, reject I guess?") is None
    assert classify_button_decision("") is None
