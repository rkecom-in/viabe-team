"""VT-47 — unit tests for the owner-approval resume path.

Classification -> decision mapping + the Pillar-7 "never guess approval"
guarantee (other / low-confidence -> no resume). mark_approval_resolved SQL
shape. No live DB, no live Anthropic (classify_fn is stubbed).
"""

from __future__ import annotations

from contextlib import nullcontext
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
    assert resolve_decision_from_reply("haan", tenant_id="t-vt270", classify_fn=_classify("approval")) == "approved"


def test_rejection_maps_to_rejected():
    assert resolve_decision_from_reply("nahi", tenant_id="t-vt270", classify_fn=_classify("rejection")) == "rejected"


def test_feedback_maps_to_needs_changes():
    """An EXPLICIT change request resumes the run for a re-draft."""
    assert resolve_decision_from_reply("make it 20% off", tenant_id="t-vt270", classify_fn=_classify("feedback")) == "needs_changes"


def test_question_is_not_a_decision_no_resume():
    """VT-632: a QUESTION is not a decision — it must NOT resolve/reject the pending approval. Mapping
    'question' -> needs_changes let an UNRELATED owner FAQ / topic-switch during a pending campaign
    approval REJECT + re-arm + re-send the approval ask verbatim instead of being answered. Now it
    returns None -> the message falls through to normal dispatch (the brain answers it) and the
    approval stays PENDING. Aligns with classify_approval_reply's own 'any ? -> None' rule."""
    assert resolve_decision_from_reply(
        "does Viabe work on both Android and iPhone?",
        tenant_id="t-vt270", classify_fn=_classify("question"),
    ) is None


def test_other_does_not_resume():
    """Pillar 7: a non-decision reply does NOT resolve the gate (no guessing)."""
    assert resolve_decision_from_reply("good morning", tenant_id="t-vt270", classify_fn=_classify("other")) is None


def test_low_confidence_does_not_resume():
    """Pillar 7: a low-confidence approval is not authoritative -> no resume."""
    assert resolve_decision_from_reply(
        "maybe ok", tenant_id="t-vt270", classify_fn=_classify("approval", confidence=0.3)
    ) is None


def test_customer_send_ambiguous_reply_never_rides_the_llm():
    """Money-safety (official §2 2026-07-10): for a customer-SEND approval, an ambiguous reply
    (deterministic classifier -> None) must NEVER be resolved to 'approved' by the LLM — even a
    high-confidence Haiku 'approval'. The send needs an UNAMBIGUOUS explicit approval; ambiguity
    means re-ask (None), never an unconsented send. The vague resume 'chalo jo pehle bol raha tha
    wahi karo' is exactly the breaker text."""
    for atype in ("campaign_send", "agent_customer_send"):
        assert resolve_decision_from_reply(
            "chalo jo pehle bol raha tha wahi karo",
            tenant_id="t-vt270", approval_type=atype,
            classify_fn=_classify("approval"),  # Haiku WOULD approve — must be ignored
        ) is None


def test_non_send_approval_still_uses_haiku_fallback():
    """A NON-send approval type (or unknown) keeps the Haiku fallback for genuinely ambiguous
    text — the money gate is scoped to customer sends only, no regression elsewhere."""
    assert resolve_decision_from_reply(
        "chalo jo pehle bol raha tha wahi karo",
        tenant_id="t-vt270", approval_type="business_policy_grant",
        classify_fn=_classify("approval"),
    ) == "approved"


def test_customer_send_explicit_deterministic_approval_still_approves():
    """The money gate only blocks the LLM fallback — an UNAMBIGUOUS deterministic approval still
    resolves to 'approved' for a customer send (the deterministic fast-path wins first)."""
    assert resolve_decision_from_reply(
        "haan bhej do", tenant_id="t-vt270", approval_type="campaign_send",
        classify_fn=_classify("rejection"),  # would be ignored — deterministic already approved
    ) == "approved"


class _CaptureConn:
    def __init__(self):
        self.calls: list[tuple] = []

    def execute(self, sql, params=None):
        from types import SimpleNamespace

        # VT-306: the wrapper's _assert_app_role probes `SELECT current_user`.
        # It's not a real query — don't record it (keeps calls[] = the real SQL),
        # and return fetchone()->None so the role check skips (None != app_role-str).
        if "current_user" in sql:
            return SimpleNamespace(fetchone=lambda: None, rowcount=0)
        self.calls.append((" ".join(sql.split()), params))
        # The wrapper reads cur.rowcount (real psycopg returns a cursor); the
        # VT-369 agent-glue hook's row re-read (find_by_id SELECT) gets None →
        # apply_agent_decision no-ops (not an agent approval).
        return SimpleNamespace(rowcount=1, fetchone=lambda: None)

    def cursor(self):
        # VT-514 emit_tm_audit's fail-closed insert uses `with conn.cursor() as
        # cur: cur.execute(...)` (real psycopg style) rather than the wrapper's
        # direct `conn.execute(...)` — a real psycopg.Connection supports both.
        # nullcontext(self) makes `cur` == this conn, so cur.execute() reuses
        # the same recording/skip logic above.
        return nullcontext(self)


def test_mark_resolved_sets_decision_status_and_guards_unresolved():
    conn = _CaptureConn()
    tid = uuid4()
    aid = uuid4()
    mark_approval_resolved(conn, tid, aid, "approved", owner_message_sid="SMabc")
    sql, params = conn.calls[0]
    assert "UPDATE pending_approvals" in sql
    assert "resolved_at = now()" in sql
    # VT-306: now tenant-predicated (was WHERE id only — the IDOR gap).
    assert "WHERE tenant_id = %s AND id = %s AND resolved_at IS NULL" in sql
    assert params[0] == "approved"  # decision
    assert params[1] == "approved"  # status (approved -> approved)
    assert params[2] == "SMabc"     # owner_message_sid (COALESCE'd)
    assert params[3] == str(tid)    # tenant predicate
    assert params[4] == str(aid)


def test_needs_changes_collapses_status_to_rejected():
    conn = _CaptureConn()
    mark_approval_resolved(conn, uuid4(), uuid4(), "needs_changes")
    _, params = conn.calls[0]
    assert params[0] == "needs_changes"  # raw decision verb retained
    assert params[1] == "rejected"       # status collapses to non-approval


def test_timeout_decision_maps_to_timed_out_status():
    conn = _CaptureConn()
    mark_approval_resolved(conn, uuid4(), uuid4(), "timeout")
    _, params = conn.calls[0]
    assert params[0] == "timeout"
    assert params[1] == "timed_out"


class _DeferConn:
    """Mock conn whose RETURNING defer_count yields a configurable value (the post-increment
    count from extend_on_defer). Plain SELECTs (the VT-369 agent-glue row re-read) get None
    so apply_agent_decision no-ops."""

    def __init__(self, defer_count_after: int):
        self._dc = defer_count_after
        self.calls: list[tuple] = []

    def execute(self, sql, params=None):
        from types import SimpleNamespace

        if "current_user" in sql:
            return SimpleNamespace(fetchone=lambda: None, rowcount=0)
        self.calls.append((" ".join(sql.split()), params))
        if "defer_count = defer_count + 1" in sql:
            return SimpleNamespace(
                fetchone=lambda: {"defer_count": self._dc}, rowcount=1
            )
        return SimpleNamespace(fetchone=lambda: None, rowcount=1)

    def cursor(self):
        # See _CaptureConn.cursor — same VT-514 emit_tm_audit gap.
        return nullcontext(self)


def test_defer_first_time_extends_and_does_not_resolve():
    """VT-334: the 1st defer (defer_count → 1 < max) EXTENDS — returns False (run stays paused),
    no resolve UPDATE."""
    conn = _DeferConn(defer_count_after=1)
    resolved = mark_approval_resolved(conn, uuid4(), uuid4(), "defer")
    assert resolved is False
    assert any("defer_count = defer_count + 1" in s for s, _ in conn.calls)
    assert any("timeout_at = now() + make_interval" in s for s, _ in conn.calls)
    assert not any("resolved_at = now()" in s for s, _ in conn.calls)  # NOT resolved


def test_defer_at_max_resolves_as_rejected():
    """VT-334: the 2nd defer (defer_count → 2 == max) resolves — decision='defer', status='rejected'
    (safe downstream; audit truth in decision). Returns True (the caller resumes)."""
    conn = _DeferConn(defer_count_after=2)
    resolved = mark_approval_resolved(conn, uuid4(), uuid4(), "defer")
    assert resolved is True
    resolve_calls = [(s, p) for s, p in conn.calls if "resolved_at = now()" in s]
    assert resolve_calls, "expected a resolve UPDATE at max defers"
    _, params = resolve_calls[0]
    assert params[0] == "defer"  # decision (audit truth)
    assert params[1] == "rejected"  # status (safe downstream)


# ── VT-668: the resolution-seam consumer guarantee (an approval must never resolve into silence) ──


class _ConsumerProbe:
    """Captures redrive_task + ack calls so a _guarantee_campaign_consumer test asserts behavior
    without a live DB / Twilio. ``bound`` is what find_task_for_resolved_approval returns."""

    def __init__(self, bound):
        self.bound = bound
        self.redriven: list = []
        self.acks: list = []

    def install(self, monkeypatch):
        import orchestrator.agent.approval_resume as ar
        from orchestrator.manager import task_store

        monkeypatch.setattr(
            task_store, "find_task_for_resolved_approval",
            lambda tenant_id, approval_id, conn=None: self.bound,
        )
        monkeypatch.setattr(
            task_store, "redrive_task",
            lambda tenant_id, task_id, *, conn: (self.redriven.append(task_id) or True),
        )
        # Stub the owner ack send so no Twilio / owner-phone read happens in the unit test.
        monkeypatch.setattr(
            ar, "_ack_owner_stalled_campaign",
            lambda conn, tenant_id, *, reset: self.acks.append(reset),
        )
        return ar


def _bound(status, approval_type="campaign_send"):
    return {"id": "task-1", "status": status, "approval_type": approval_type}


def test_consumer_guarantee_dead_letter_redrives_and_acks(monkeypatch):
    """The VT-668 incident: an APPROVED campaign_send whose executor was reaped to dead_letter must
    redrive the task (un-stick) AND send an honest owner ack — NEVER silence."""
    probe = _ConsumerProbe(_bound("dead_letter"))
    ar = probe.install(monkeypatch)
    ar._guarantee_campaign_consumer(_CaptureConn(), uuid4(), uuid4(), "approved")
    assert probe.redriven == ["task-1"]
    assert probe.acks == [True]  # reset=True: honest "stalled, couldn't send now" ack


def test_consumer_guarantee_blocked_redrives_and_acks(monkeypatch):
    """A 'blocked' (retry-ladder rung) executor is equally dead to a just-resolved approval —
    redrive + honest ack (the reaper would otherwise walk it to dead_letter with the approval now
    closed, i.e. silently)."""
    probe = _ConsumerProbe(_bound("blocked"))
    ar = probe.install(monkeypatch)
    ar._guarantee_campaign_consumer(_CaptureConn(), uuid4(), uuid4(), "approved")
    assert probe.redriven == ["task-1"]
    assert probe.acks == [True]


def test_consumer_guarantee_terminal_task_honest_expiry_no_redrive(monkeypatch):
    """The approved reply arrived after the task already closed (cancelled) — honest-expiry ack
    (reset=False), NO redrive (a terminal task is not redrivable)."""
    probe = _ConsumerProbe(_bound("cancelled"))
    ar = probe.install(monkeypatch)
    ar._guarantee_campaign_consumer(_CaptureConn(), uuid4(), uuid4(), "approved")
    assert probe.redriven == []
    assert probe.acks == [False]  # reset=False: honest "already closed" ack


def test_consumer_guarantee_live_loop_task_left_untouched(monkeypatch):
    """A task in an active/waiting state (waiting_owner) is presumed to have a LIVE loop that will
    react — the resolution seam must NOT redrive or ack (that is the loop's job; the reaper's
    orphaned-approval sweep is the backstop if the loop is actually dead)."""
    for status in ("waiting_owner", "running", "verifying", "clarifying"):
        probe = _ConsumerProbe(_bound(status))
        ar = probe.install(monkeypatch)
        ar._guarantee_campaign_consumer(_CaptureConn(), uuid4(), uuid4(), "approved")
        assert probe.redriven == [], status
        assert probe.acks == [], status


def test_consumer_guarantee_non_campaign_send_is_noop(monkeypatch):
    """Only a campaign_send routes through the manager loop; a dead_letter task bound to some other
    approval_type must NOT be touched by this money-path guarantee."""
    probe = _ConsumerProbe(_bound("dead_letter", approval_type="autonomy_upgrade"))
    ar = probe.install(monkeypatch)
    ar._guarantee_campaign_consumer(_CaptureConn(), uuid4(), uuid4(), "approved")
    assert probe.redriven == []
    assert probe.acks == []


def test_consumer_guarantee_no_bound_task_is_noop(monkeypatch):
    """No bound manager_task (the legacy graph-resume path owns its own run lifecycle) — clean
    no-op, no crash."""
    probe = _ConsumerProbe(None)
    ar = probe.install(monkeypatch)
    ar._guarantee_campaign_consumer(_CaptureConn(), uuid4(), uuid4(), "approved")
    assert probe.redriven == []
    assert probe.acks == []


def test_consumer_guarantee_only_on_approved(monkeypatch):
    """A rejected / needs_changes resolution has nothing to send — the guarantee is a no-op and
    must not even look up the task."""
    for decision in ("rejected", "needs_changes", "timeout"):
        probe = _ConsumerProbe(_bound("dead_letter"))
        ar = probe.install(monkeypatch)
        ar._guarantee_campaign_consumer(_CaptureConn(), uuid4(), uuid4(), decision)
        assert probe.redriven == [], decision
        assert probe.acks == [], decision


def test_consumer_guarantee_failsoft_never_raises(monkeypatch):
    """A bug in the consumer guarantee must NEVER unwind the owner's authoritative resolution
    (Pillar 7) — the whole body is fail-soft."""
    import orchestrator.agent.approval_resume as ar
    from orchestrator.manager import task_store

    def _boom(*a, **k):
        raise RuntimeError("join blew up")

    monkeypatch.setattr(task_store, "find_task_for_resolved_approval", _boom)
    # Must not raise.
    ar._guarantee_campaign_consumer(_CaptureConn(), uuid4(), uuid4(), "approved")


def test_mark_resolved_invokes_consumer_guarantee_on_applied(monkeypatch):
    """mark_approval_resolved runs the consumer guarantee exactly once, gated on the resolve having
    applied (rowcount>0) — the wiring that makes a double-resolve idempotent (the second resolve's
    rowcount is 0, so no second redrive/ack)."""
    import orchestrator.agent.approval_resume as ar

    calls: list[str] = []
    monkeypatch.setattr(
        ar, "_guarantee_campaign_consumer",
        lambda conn, tenant_id, approval_id, decision: calls.append(decision),
    )
    # _CaptureConn.execute returns rowcount=1 -> resolve applied -> guarantee runs once.
    mark_approval_resolved(_CaptureConn(), uuid4(), uuid4(), "approved")
    assert calls == ["approved"]


def test_mark_resolved_skips_consumer_guarantee_when_already_resolved(monkeypatch):
    """Double-resolve idempotency: when the resolve UPDATE matches 0 rows (already resolved), the
    consumer guarantee is NOT invoked again."""
    import orchestrator.agent.approval_resume as ar

    calls: list[str] = []
    monkeypatch.setattr(
        ar, "_guarantee_campaign_consumer",
        lambda conn, tenant_id, approval_id, decision: calls.append(decision),
    )

    class _AlreadyResolvedConn(_CaptureConn):
        def execute(self, sql, params=None):
            from types import SimpleNamespace

            if "current_user" in sql:
                return SimpleNamespace(fetchone=lambda: None, rowcount=0)
            self.calls.append((" ".join(sql.split()), params))
            # The resolve UPDATE matches 0 rows (resolved_at already set); everything else 1.
            rc = 0 if "UPDATE pending_approvals" in sql and "resolved_at = now()" in sql else 1
            return SimpleNamespace(rowcount=rc, fetchone=lambda: None)

    mark_approval_resolved(_AlreadyResolvedConn(), uuid4(), uuid4(), "approved")
    assert calls == []


def test_count_recent_campaign_requests_sql_and_value():
    """VT-334 per-week budget count: scopes to campaign_send + a created_at window, returns the
    count (the collapse guard skips at >= _WEEKLY_APPROVAL_BUDGET)."""
    from types import SimpleNamespace
    from uuid import uuid4 as _uuid4

    from orchestrator.db.wrappers import PendingApprovalsWrapper

    class _CountConn:
        def __init__(self):
            self.calls: list[tuple] = []

        def execute(self, sql, params=None):
            if "current_user" in sql:
                return SimpleNamespace(fetchone=lambda: None, rowcount=0)
            self.calls.append((" ".join(sql.split()), params))
            return SimpleNamespace(fetchone=lambda: {"n": 3}, rowcount=1)

    conn = _CountConn()
    n = PendingApprovalsWrapper().count_recent_campaign_requests(_uuid4(), days=7, conn=conn)
    assert n == 3
    sql, params = conn.calls[0]
    assert "count(*)" in sql
    # VT-369: one SHARED 2/week owner-interrupt budget across the campaign +
    # agent surfaces (plan §4.3).
    assert "approval_type IN ('campaign_send', 'agent_customer_send')" in sql
    assert "created_at >= now() - make_interval(days => %s)" in sql
    assert params[1] == 7


# --- theek-hai flip: customer-send scoping (Fazal money middle-path 2026-07-12) ----------------
def test_bare_weak_ack_holds_customer_send() -> None:
    """A bare 'theek hai' (weak ack, no send verb, no strong yes) does NOT approve a customer SEND
    -> None (re-ask). Fires on the deterministic fast-path, so no classify_fn is consulted."""
    tid = uuid4()
    for at in ("campaign_send", "agent_customer_send"):
        assert resolve_decision_from_reply("theek hai", tenant_id=tid, approval_type=at) is None
        assert resolve_decision_from_reply("ok", tenant_id=tid, approval_type=at) is None


def test_unambiguous_send_approval_still_approves() -> None:
    """An explicit send verb / strong yes is unambiguous -> still approves a customer send."""
    tid = uuid4()
    assert (
        resolve_decision_from_reply("theek hai bhej do", tenant_id=tid, approval_type="campaign_send")
        == "approved"
    )
    assert (
        resolve_decision_from_reply("haan bhej do", tenant_id=tid, approval_type="campaign_send")
        == "approved"
    )
    assert (
        resolve_decision_from_reply("bhej do", tenant_id=tid, approval_type="campaign_send")
        == "approved"
    )


def test_bare_weak_ack_still_approves_non_send() -> None:
    """The flip is customer-SEND-scoped: a non-send approval (autonomy_upgrade) keeps its existing
    bare-ack behavior -> 'theek hai' still approves."""
    tid = uuid4()
    assert (
        resolve_decision_from_reply("theek hai", tenant_id=tid, approval_type="autonomy_upgrade")
        == "approved"
    )


# --- CD5 §7D: owner "skip review, just send" is HONORED and AUDITED (Fazal ruling 2026-07-12) ----
def test_skip_review_approved_customer_send_is_honored_and_audited(monkeypatch) -> None:
    """An EXPLICIT owner skip-review waiver on a customer SEND ("bina review seedha bhej do") is
    HONORED — the decision is the deterministic 'approved', UNCHANGED — AND leaves a §7D audit trail
    (owner_skip_review_authorized). AUDIT-ONLY: the audit is a side-effect, never a decision input."""
    import orchestrator.observability.tm_audit as tm_audit_mod

    calls: list[dict] = []
    monkeypatch.setattr(tm_audit_mod, "emit_tm_audit", lambda **kw: calls.append(kw))
    decision = resolve_decision_from_reply(
        "bina review seedha bhej do", tenant_id=uuid4(), approval_type="campaign_send"
    )
    assert decision == "approved"  # decision UNCHANGED — the owner's waiver is honored
    assert len(calls) == 1
    kw = calls[0]
    assert kw["event_layer"] == "decides"
    assert kw["event_kind"] == "owner_skip_review_authorized"
    assert kw["actor"] == "team_manager"
    assert kw["decision"]["review_waived"] is True
    assert kw["decision"]["approval_type"] == "campaign_send"
    # PII-safe (CL-390): the owner reply body is never carried in the audit payload.
    assert "bhej" not in (kw.get("summary") or "")


def test_ordinary_approval_emits_no_skip_review_audit(monkeypatch) -> None:
    """An ordinary 'haan bhej do' approves but carries NO skip-review waiver -> NO §7D audit. The
    record fires ONLY on an explicit review-waiver, not on every customer-send approval."""
    import orchestrator.observability.tm_audit as tm_audit_mod

    calls: list[dict] = []
    monkeypatch.setattr(tm_audit_mod, "emit_tm_audit", lambda **kw: calls.append(kw))
    decision = resolve_decision_from_reply(
        "haan bhej do", tenant_id=uuid4(), approval_type="campaign_send"
    )
    assert decision == "approved"
    assert calls == []  # no skip-review marker -> no audit


def test_skip_review_audit_scoped_to_customer_send(monkeypatch) -> None:
    """The §7D skip-review audit is scoped to CUSTOMER-SEND approvals (money). A non-send approval
    type with the same waiver phrasing approves normally but emits NO skip-review audit."""
    import orchestrator.observability.tm_audit as tm_audit_mod

    calls: list[dict] = []
    monkeypatch.setattr(tm_audit_mod, "emit_tm_audit", lambda **kw: calls.append(kw))
    decision = resolve_decision_from_reply(
        "bina review seedha bhej do", tenant_id=uuid4(), approval_type="autonomy_upgrade"
    )
    assert decision == "approved"
    assert calls == []


def test_skip_review_audit_failure_never_blocks_decision(monkeypatch) -> None:
    """FAIL-SOFT (Pillar 7): an audit emit that RAISES must never affect the send decision — the
    owner's authorized send is not held on an observability write. Decision is still 'approved'."""
    import orchestrator.observability.tm_audit as tm_audit_mod

    def _boom(**kw):
        raise RuntimeError("audit sink down")

    monkeypatch.setattr(tm_audit_mod, "emit_tm_audit", _boom)
    decision = resolve_decision_from_reply(
        "bina review seedha bhej do", tenant_id=uuid4(), approval_type="campaign_send"
    )
    assert decision == "approved"  # audit raised, decision unaffected
