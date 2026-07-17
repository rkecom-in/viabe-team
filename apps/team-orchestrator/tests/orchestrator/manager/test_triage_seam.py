"""VT-606 (team-lead ruling round 2) — the triage seam's legacy-mode pin (pure, no DB). The
shadow/enforce (DB-backed) coverage is in ``test_triage_seam_db.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("anthropic")

from orchestrator.manager import triage_seam as ts  # noqa: E402


def test_legacy_mode_never_calls_triage(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pin: legacy mode must not even IMPORT triage.triage_turn, let alone call it — the hot
    path stays byte-identical to pre-VT-606."""
    called = {"n": 0}

    def _boom(*a, **k):  # would fail loudly if ever reached
        called["n"] += 1
        raise AssertionError("triage_turn must NEVER be called in legacy mode")

    monkeypatch.setattr("orchestrator.manager.triage.triage_turn", _boom)

    result = ts.triage_seam(uuid4(), "hello", "SMxxx", mode="legacy")

    assert called["n"] == 0
    assert result.outcome is None
    assert result.task_id is None
    assert result.skip_legacy_dispatch is False


def test_legacy_mode_no_op_result_is_a_singleton() -> None:
    """No DB access at all in legacy mode — a bogus tenant_id must not raise."""
    result = ts.triage_seam(uuid4(), "anything", "SMxxx", mode="legacy")
    assert result is ts._NO_OP


def test_triage_seam_result_direct_reply_defaults_none() -> None:
    """Shared infra (Step 1) — the new direct_reply_text field defaults None so every existing
    3-arg construction stays byte-compatible, and _NO_OP carries no reply."""
    r = ts.TriageSeamResult(outcome=None, task_id=None, skip_legacy_dispatch=False)
    assert r.direct_reply_text is None
    assert ts._NO_OP.direct_reply_text is None
    r2 = ts.TriageSeamResult(
        outcome="new_task", task_id=None, skip_legacy_dispatch=True, direct_reply_text="hello"
    )
    assert r2.direct_reply_text == "hello"


def test_legacy_mode_default_when_no_explicit_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAM_MANAGER_LOOP_MODE", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(
        "orchestrator.manager.triage.triage_turn", lambda **k: called.__setitem__("n", 1)
    )

    result = ts.triage_seam(uuid4(), "hello", "SMxxx")

    assert called["n"] == 0
    assert result is ts._NO_OP


def test_df5_question_shape_gate_pin() -> None:
    """Hook-caught regression pin: the DF5 net answers QUESTIONS only. classify_status_query is
    bag-of-words, so an IMPERATIVE carrying a count token ("win back lapsed customers") must NOT be
    question-shaped — it falls through to D3/triage (new_task), never a count answer."""
    from orchestrator.onboarding.campaign_first_contact import _INTERROGATIVE_LEAD_RE

    def q(t: str) -> bool:
        toks = set(t.lower().replace("?", " ").split())
        return "?" in t or bool(_INTERROGATIVE_LEAD_RE.match(t)) or bool(
            toks & {"kitne", "kitni", "kitna", "how"}
        )

    assert q("win back lapsed customers") is False
    assert q("run a win-back campaign for my lapsed customers") is False
    assert q("how many lapsed customers do I have?") is True
    assert q("total kitne customers hain jo lapse ho gaye?") is True
    assert q("what's the status?") is True


# ── VT-657 (option C) — LLM-primary campaign-recovery routing (pure, no DB) ───────────────────────


def _stub_seam_reads(
    monkeypatch: pytest.MonkeyPatch, *, has_active_task: bool = False, open_qs=None
) -> None:
    """Neutralize the seam's DB reads (pending_questions / has_active_task / the DF5 status net) for
    a pure-logic no-DB test — the routing decision under test never depends on real DB state."""
    monkeypatch.setattr(
        "orchestrator.manager.pending_questions.get_open", lambda *a, **k: list(open_qs or [])
    )
    monkeypatch.setattr(
        "orchestrator.manager.task_store.has_active_task", lambda *a, **k: has_active_task
    )
    monkeypatch.setattr(
        "orchestrator.owner_inputs.status_query.answer_status_query", lambda *a, **k: None
    )
    # VT-667 fix-4: the campaign-dispatch entry now first checks for a pending campaign_send approval
    # (revise_pending_campaign). Default it to "no pending campaign" so the first-contact routing
    # tests below stay pure-logic (no DB read); the revision path has its own dedicated tests.
    monkeypatch.setattr(ts, "revise_pending_campaign", lambda *a, **k: None)


def _boom(*_a, **_k):
    raise AssertionError("this path must not be reached")


def test_vt657_llm_campaign_recovery_routes_through_shared_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new_task the LLM classifies as campaign_recovery routes through the SHARED
    _dispatch_campaign_first_contact (the D3 dispatch body), NOT the generic clarification plan that
    made the brain dither. The message deliberately does NOT trip the FROZEN keyword net
    (is_campaign_plan_imperative is False for "…offer to bring my past customers back" — the exact
    j02 phrasing D3 misses), so ONLY the LLM route can fire the dispatch."""
    from orchestrator.manager.triage import TriageResult

    _stub_seam_reads(monkeypatch)
    monkeypatch.setattr(
        "orchestrator.manager.triage.triage_turn",
        lambda **k: TriageResult(
            outcome="new_task", reasoning="win-back", task_kind="campaign_recovery"
        ),
    )
    sentinel = ts.TriageSeamResult(
        outcome="new_task", task_id=uuid4(), skip_legacy_dispatch=True
    )
    called = {"n": 0}

    def _fake_dispatch(tenant_id, message_text, message_sid):
        called["n"] += 1
        return sentinel

    monkeypatch.setattr(ts, "_dispatch_campaign_first_contact", _fake_dispatch)
    monkeypatch.setattr(ts, "_create_plan_for_new_task", _boom)  # generic plan must NOT build

    result = ts.triage_seam(
        uuid4(),
        "put together a Diwali offer to bring my past customers back in",
        "SMcamp",
        mode="enforce",
    )
    assert called["n"] == 1
    assert result is sentinel


def test_vt657_llm_general_new_task_skips_campaign_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A general new_task (task_kind default) must NOT hit the campaign dispatch — it builds the
    generic plan exactly as before (backward-compatible)."""
    from orchestrator.manager.triage import TriageResult

    _stub_seam_reads(monkeypatch)
    monkeypatch.setattr(
        "orchestrator.manager.triage.triage_turn",
        lambda **k: TriageResult(outcome="new_task", reasoning="connect data", task_kind="general"),
    )
    monkeypatch.setattr(ts, "_dispatch_campaign_first_contact", _boom)  # must NOT fire for general
    made = {"n": 0}
    monkeypatch.setattr(
        ts, "_create_plan_for_new_task", lambda *a, **k: made.__setitem__("n", 1) or None
    )

    result = ts.triage_seam(uuid4(), "connect my shopify store", "SMgen", mode="enforce")
    assert made["n"] == 1
    assert result.outcome == "new_task"
    assert result.skip_legacy_dispatch is False  # task_id None -> falls through to legacy


def test_vt657_campaign_recovery_not_dispatched_when_active_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The C route mirrors D3's guard: with an active task already owning the tenant, a
    campaign_recovery new_task does NOT mint a second campaign dispatch (never fires the helper) —
    it takes the generic path."""
    from orchestrator.manager.triage import TriageResult

    _stub_seam_reads(monkeypatch, has_active_task=True)
    monkeypatch.setattr(
        "orchestrator.manager.triage.triage_turn",
        lambda **k: TriageResult(
            outcome="new_task", reasoning="win-back", task_kind="campaign_recovery"
        ),
    )
    monkeypatch.setattr(ts, "_dispatch_campaign_first_contact", _boom)  # active task -> no dispatch
    monkeypatch.setattr(ts, "_create_plan_for_new_task", lambda *a, **k: None)

    result = ts.triage_seam(
        uuid4(),
        "put together a Diwali offer to bring my past customers back in",
        "SMact",
        mode="enforce",
    )
    assert result.outcome == "new_task"


# ── VT-667 fix-4 — pending-campaign REVISION (correction → supersede stale draft + re-dispatch) ────


class _FakeApprovals:
    """Stand-in for PendingApprovalsWrapper: returns the seeded open approval / bound task and
    records the supersede (mark_resolved) — appending to a shared ``order`` list so a test can pin
    that the old approval is resolved BEFORE the fresh loop starts (no double-arm window)."""

    def __init__(self, *, open_approval, bound_task, order):
        self._open = open_approval
        self._bound = bound_task
        self._order = order
        self.resolved: list[dict] = []

    def find_open_for_tenant(self, tenant_id, *, conn=None):
        return self._open

    def find_bound_task_for_approval(self, tenant_id, approval_id, *, conn=None):
        return self._bound

    def mark_resolved(
        self, tenant_id, approval_id, *, decision, status, owner_message_sid=None, conn=None
    ):
        self._order.append("supersede")
        self.resolved.append({"approval_id": approval_id, "decision": decision, "status": status})
        return 1


_ORIGINAL_BRIEF = "run a simple win-back (no offer) for my lapsed customers"


def _install_revision_stubs(monkeypatch, *, open_approval, bound_task, new_status="planned"):
    """Wire revise_pending_campaign's collaborators for a pure-logic test (no DB / no DBOS)."""
    order: list[str] = []
    fake = _FakeApprovals(open_approval=open_approval, bound_task=bound_task, order=order)
    monkeypatch.setattr("orchestrator.db.wrappers.PendingApprovalsWrapper", lambda: fake)

    new_id = uuid4()
    events: dict[str, list] = {
        "cancelled_wf": [], "cancelled_task": [], "started": [], "created": [], "order": order,
    }

    def _get_task(tenant_id, task_id):
        if bound_task is not None and str(task_id) == str(bound_task["id"]):
            return {"objective": _ORIGINAL_BRIEF}  # the OLD task carries the original brief
        return {"status": new_status}  # the freshly-created revision task's admission status

    monkeypatch.setattr("orchestrator.manager.task_store.get_task", _get_task)
    monkeypatch.setattr(
        "orchestrator.manager.task_store.set_task_status",
        lambda tid, task_id, status, **k: events["cancelled_task"].append((str(task_id), status))
        or True,
    )

    def _create_plan(tenant_id, plan, *, source_message_sid, shadow=False):
        events["created"].append(
            {"situation": plan.steps[0].situation, "sid": source_message_sid, "shadow": shadow}
        )
        return new_id

    monkeypatch.setattr("orchestrator.manager.plan_store.create_plan", _create_plan)
    monkeypatch.setattr(
        "orchestrator.manager.workflow.start_manager_task_workflow",
        lambda tid, task_id: (events["order"].append("start"), events["started"].append(str(task_id))),
    )
    monkeypatch.setattr(
        "orchestrator.manager.workflow.manager_task_workflow_id",
        lambda tid, task_id: f"manager_task:{tid}:{task_id}",
    )
    import dbos

    monkeypatch.setattr(
        dbos.DBOS, "cancel_workflow",
        lambda wf_id: events["cancelled_wf"].append(wf_id), raising=False,
    )
    monkeypatch.setattr(ts, "emit_tm_audit", lambda **k: None)
    return fake, events, new_id


def test_revision_supersedes_old_draft_and_redispatches_with_combined_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core fix: a correction while a campaign_send approval is OPEN cancels the stale loop,
    supersedes the old approval (decision='rejected' → the old draft can never send), cancels the
    old task, and re-dispatches SR with creative_brief = ORIGINAL brief + the correction verbatim."""
    tenant = uuid4()
    old_task = uuid4()
    aid = str(uuid4())
    fake, events, new_id = _install_revision_stubs(
        monkeypatch,
        open_approval={"id": aid, "run_id": str(uuid4()), "approval_type": "campaign_send"},
        bound_task={"id": str(old_task), "status": "waiting_owner", "approval_type": "campaign_send"},
    )

    correction = "this isn't the Diwali offer I asked for — redo it with the festive vibe + 20% discount"
    ack = ts.revise_pending_campaign(tenant, correction, "SMcorr")

    assert ack == ts._REVISION_ACK
    # supersede: exactly one, non-approved → the old draft is unsendable at the chokepoint
    assert fake.resolved == [{"approval_id": aid, "decision": "rejected", "status": "rejected"}]
    # the stale loop workflow was cancelled (by the deterministic id) BEFORE anything else
    assert events["cancelled_wf"] == [f"manager_task:{tenant}:{old_task}"]
    # the stale task was cancelled to free the one-active slot
    assert events["cancelled_task"] == [(str(old_task), "cancelled")]
    # a fresh loop was started for the revision task
    assert events["started"] == [str(new_id)]
    # the re-dispatched plan's brief carries BOTH the original AND the correction (VT-667 threading)
    situation = events["created"][0]["situation"]
    assert "simple win-back (no offer)" in situation  # original brief
    assert "festive vibe" in situation and "20% discount" in situation  # the owner's correction
    assert events["created"][0]["sid"] == "SMcorr"  # idempotency key = the correction's sid
    assert events["created"][0]["shadow"] is False


def test_revision_supersede_happens_before_the_new_loop_starts_no_double_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Money-safety: the old approval is RESOLVED before the fresh SR loop is started, so there is
    never a window with two open approvals for the tenant (no double-arm / no double-send)."""
    fake, events, _ = _install_revision_stubs(
        monkeypatch,
        open_approval={"id": str(uuid4()), "run_id": str(uuid4()), "approval_type": "campaign_send"},
        bound_task={"id": str(uuid4()), "status": "waiting_owner", "approval_type": "campaign_send"},
    )
    ts.revise_pending_campaign(uuid4(), "make it festive", "SMorder")
    assert events["order"].index("supersede") < events["order"].index("start")


def test_revision_no_open_approval_returns_none_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pending campaign_send approval → NOT a revision: return None, supersede/dispatch nothing."""
    fake, events, _ = _install_revision_stubs(monkeypatch, open_approval=None, bound_task=None)
    assert ts.revise_pending_campaign(uuid4(), "anything", "SMx") is None
    assert fake.resolved == [] and events["started"] == [] and events["created"] == []


def test_revision_non_campaign_send_approval_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OPEN approval of another type (autonomy_upgrade, agent_customer_send batch, …) is NOT a
    campaign draft — the revision must not touch it (returns None → caller keeps its own path)."""
    fake, events, _ = _install_revision_stubs(
        monkeypatch,
        open_approval={"id": str(uuid4()), "run_id": str(uuid4()), "approval_type": "autonomy_upgrade"},
        bound_task=None,
    )
    assert ts.revise_pending_campaign(uuid4(), "yes go ahead", "SMx") is None
    assert fake.resolved == [] and events["started"] == []


def test_revision_legacy_graph_approval_without_bound_task_still_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A campaign_send approval with NO bound manager_task (legacy graph path) still supersedes +
    re-dispatches — nothing to cancel, brief falls back to the correction alone."""
    fake, events, new_id = _install_revision_stubs(
        monkeypatch,
        open_approval={"id": str(uuid4()), "run_id": str(uuid4()), "approval_type": "campaign_send"},
        bound_task=None,
    )
    ack = ts.revise_pending_campaign(uuid4(), "add a Diwali discount", "SMleg")
    assert ack == ts._REVISION_ACK
    assert events["cancelled_wf"] == [] and events["cancelled_task"] == []  # nothing bound to cancel
    assert len(fake.resolved) == 1 and events["started"] == [str(new_id)]
    assert events["created"][0]["situation"] == "add a Diwali discount"  # correction-only brief


def test_revision_not_admitted_falls_through_after_supersede(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the fresh plan somehow does not admit 'planned', never leave a queued revision that won't
    start — return None (caller falls through). The old approval is still superseded (money-safe)."""
    fake, events, _ = _install_revision_stubs(
        monkeypatch,
        open_approval={"id": str(uuid4()), "run_id": str(uuid4()), "approval_type": "campaign_send"},
        bound_task={"id": str(uuid4()), "status": "waiting_owner", "approval_type": "campaign_send"},
        new_status="queued",  # slot somehow still held → not admitted 'planned'
    )
    assert ts.revise_pending_campaign(uuid4(), "make it festive", "SMq") is None
    assert len(fake.resolved) == 1  # supersede STILL happened (old draft unsendable)
    assert events["started"] == []  # no orphan loop started


def test_dispatch_first_contact_or_revision_prefers_revision_when_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared campaign-dispatch entry routes to the REVISION (not a first-contact re-mint) when
    revise_pending_campaign reports a pending draft."""
    monkeypatch.setattr(ts, "revise_pending_campaign", lambda *a, **k: "REWORKING IT")
    monkeypatch.setattr(ts, "_dispatch_campaign_first_contact", _boom)  # must NOT re-mint
    out = ts._dispatch_campaign_first_contact_or_revision(uuid4(), "redo the offer", "SMr")
    assert out is not None
    assert out.skip_legacy_dispatch is True
    assert out.direct_reply_text == "REWORKING IT"
    assert out.outcome == "new_task"


def test_dispatch_first_contact_or_revision_falls_to_first_contact_when_no_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pending draft → the entry behaves exactly as the first-contact dispatch (unchanged)."""
    sentinel = ts.TriageSeamResult(outcome="new_task", task_id=uuid4(), skip_legacy_dispatch=True)
    monkeypatch.setattr(ts, "revise_pending_campaign", lambda *a, **k: None)
    monkeypatch.setattr(ts, "_dispatch_campaign_first_contact", lambda *a, **k: sentinel)
    assert ts._dispatch_campaign_first_contact_or_revision(uuid4(), "win back", "SMf") is sentinel


# --- VT-670: the already-SENT re-mint guard (warn-once, fail-open) -------------------------------


class _FakeCampaigns:
    """A CampaignsWrapper stand-in returning a fixed recent-campaigns list."""

    def __init__(self, rows):
        self._rows = rows

    def list_recent_basic(self, tenant_id, *, limit=5, conn=None):
        return list(self._rows)


class _FakePool:
    """A get_pool() stand-in whose connection returns a fixed dedup-SELECT row."""

    def __init__(self, warned_row):
        self._warned_row = warned_row

    def connection(self):
        from contextlib import contextmanager

        pool = self

        @contextmanager
        def _cm():
            class _Conn:
                def execute(self, sql, params):
                    class _Cur:
                        def fetchone(_s):
                            return pool._warned_row

                    return _Cur()

            yield _Conn()

        return _cm()


def _install_sent_guard_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows,
    warned_row=None,
):
    """Wire the guard's lazy imports to fakes + capture emitted audit events."""
    import orchestrator.db.wrappers as wrappers_mod
    import orchestrator.graph as graph_mod

    events: list[dict] = []
    monkeypatch.setattr(wrappers_mod, "CampaignsWrapper", lambda: _FakeCampaigns(rows))
    monkeypatch.setattr(graph_mod, "get_pool", lambda: _FakePool(warned_row))
    monkeypatch.setattr(ts, "emit_tm_audit", lambda **kw: events.append(kw))
    return events


def _sent_row(hours_ago: float, status: str = "sent"):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4 as _u

    return {
        "id": str(_u()),
        "status": status,
        "generated_at": datetime.now(UTC) - timedelta(hours=hours_ago),
    }


def test_sent_guard_blocks_first_reask_with_honest_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A campaign SENT 2h ago + no prior warn → honest already-sent reply, no mint, audit stamped."""
    from orchestrator.onboarding.campaign_first_contact import ALREADY_SENT_REPLY

    events = _install_sent_guard_stubs(monkeypatch, rows=[_sent_row(2.0)])
    out = ts._recent_sent_campaign_guard(uuid4(), "SMdup")
    assert out is not None
    assert out.direct_reply_text == ALREADY_SENT_REPLY
    assert out.skip_legacy_dispatch is True and out.task_id is None
    assert len(events) == 1
    assert events[0]["event_kind"] == ts._ALREADY_SENT_AUDIT_KIND
    assert events[0]["decision"]["message_sid"] == "SMdup"


def test_sent_guard_second_ask_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """WARN-ONCE: the dedup stamp exists since the send → the owner asked AGAIN = explicit confirm
    → guard returns None (mint proceeds). Never a stall loop."""
    events = _install_sent_guard_stubs(
        monkeypatch, rows=[_sent_row(2.0)], warned_row=(1,)
    )
    assert ts._recent_sent_campaign_guard(uuid4(), "SMagain") is None
    assert events == []  # no double warn


def test_sent_guard_outside_window_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A send OLDER than the window never blocks."""
    events = _install_sent_guard_stubs(
        monkeypatch, rows=[_sent_row(ts.ALREADY_SENT_WINDOW_HOURS + 5.0)]
    )
    assert ts._recent_sent_campaign_guard(uuid4(), "SMold") is None
    assert events == []


def test_sent_guard_only_sent_status_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recent REJECTED/CANCELLED draft stays freely re-mintable — only 'sent' blocks."""
    events = _install_sent_guard_stubs(
        monkeypatch,
        rows=[_sent_row(1.0, status="cancelled"), _sent_row(3.0, status="proposed")],
    )
    assert ts._recent_sent_campaign_guard(uuid4(), "SMrej") is None
    assert events == []


def test_sent_guard_fails_open_on_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any wrapper failure → None (fail-OPEN: dedup protection must not block a legit campaign)."""
    import orchestrator.db.wrappers as wrappers_mod

    def _boom_wrapper():
        raise RuntimeError("db down")

    monkeypatch.setattr(wrappers_mod, "CampaignsWrapper", _boom_wrapper)
    assert ts._recent_sent_campaign_guard(uuid4(), "SMerr") is None


def test_first_contact_returns_guard_result_before_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatch chokepoint returns the guard's reply INSTEAD of minting when the guard fires."""
    import orchestrator.onboarding.campaign_first_contact as cfc

    guard_result = ts.TriageSeamResult(
        outcome="new_task", task_id=None, skip_legacy_dispatch=True, direct_reply_text="ALREADY"
    )
    monkeypatch.setattr(cfc, "campaign_cohort_is_empty", lambda tid: False)
    monkeypatch.setattr(ts, "_recent_sent_campaign_guard", lambda *a, **k: guard_result)
    # create_plan must never be reached.
    import orchestrator.manager.plan_store as plan_store_mod

    monkeypatch.setattr(
        plan_store_mod, "create_plan", lambda *a, **k: (_ for _ in ()).throw(AssertionError("minted"))
    )
    out = ts._dispatch_campaign_first_contact(uuid4(), "run winback again", "SMg")
    assert out is guard_result


def test_first_contact_mints_when_guard_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard None → the mint path runs exactly as before (no behavior change)."""
    import orchestrator.manager.plan_store as plan_store_mod
    import orchestrator.manager.task_store as task_store_mod
    import orchestrator.manager.workflow as workflow_mod
    import orchestrator.onboarding.campaign_first_contact as cfc

    tid = uuid4()
    task_id = uuid4()
    started: list = []
    monkeypatch.setattr(cfc, "campaign_cohort_is_empty", lambda t: False)
    monkeypatch.setattr(cfc, "mentions_customer_list_request", lambda t: False)
    monkeypatch.setattr(ts, "_recent_sent_campaign_guard", lambda *a, **k: None)
    monkeypatch.setattr(ts, "emit_tm_audit", lambda **kw: None)
    monkeypatch.setattr(plan_store_mod, "create_plan", lambda *a, **k: task_id)
    monkeypatch.setattr(
        task_store_mod, "get_task", lambda t, i: {"id": str(i), "status": "planned"}
    )
    monkeypatch.setattr(workflow_mod, "start_manager_task_workflow", lambda t, i: started.append(i))
    out = ts._dispatch_campaign_first_contact(tid, "run winback", "SMok")
    assert out is not None and out.task_id == task_id
    assert started == [task_id]
