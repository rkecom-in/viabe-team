"""VT-740 — the owner-approval redrive gets an effect-check that REPORTS and never blocks.

``approval_resume._guarantee_campaign_consumer`` redrives a dead manager_task when the owner
approves a ``campaign_send``. That path had no idea what had already gone out. VT-740's exit gate
asks for it to be "gated the same way" as the reaper; the row's own conclusion, after two reverted
attempts, is that a BLOCK here converts an owner authorization into silence — the VT-668 failure
the enclosing function exists to prevent. So what lands is visibility, and these tests pin the
three properties that makes it safe to keep:

  * it fires (reachability — two inert changes have already shipped on this row),
  * it never blocks and never raises, and
  * it reads inside a SAVEPOINT, because the caller wraps the whole resolution in one transaction
    and a server-side error on that connection would turn the owner's COMMIT into a ROLLBACK.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.agent import approval_resume  # noqa: E402


class _FakeTxn:
    """Records that a SAVEPOINT scope was opened, and whether it unwound."""

    def __init__(self, log: list[str]) -> None:
        self._log = log

    def __enter__(self):
        self._log.append("enter")
        return self

    def __exit__(self, exc_type, exc, tb):
        self._log.append("rollback" if exc_type else "commit")
        return False  # never swallow — psycopg re-raises after rolling back to the savepoint


class _FakeConn:
    """Stands in for the owner-resolution connection. ``transaction()`` is the only thing the
    effect-check is allowed to touch on it — everything else goes through the wrapper layer."""

    def __init__(self) -> None:
        self.txn_log: list[str] = []

    def transaction(self):
        return _FakeTxn(self.txn_log)

    def execute(self, *a, **k):  # pragma: no cover — reaching this is the bug
        raise AssertionError("the effect-check must not run raw SQL on the owner's connection")


@pytest.fixture()
def alerts(monkeypatch):
    from orchestrator.alerts import dispatch as dispatch_mod

    seen: list = []
    monkeypatch.setattr(dispatch_mod, "dispatch_alert", lambda t: seen.append(t) or uuid4())
    return seen


def _patch_wrapper(monkeypatch, *, campaigns, rollup):
    from orchestrator.db.wrappers import CampaignsWrapper

    monkeypatch.setattr(
        CampaignsWrapper, "list_recent_basic", lambda self, t, **k: campaigns, raising=True
    )
    monkeypatch.setattr(
        CampaignsWrapper, "effect_state_rollup", lambda self, t, **k: rollup, raising=True
    )


def test_a_partially_delivered_live_campaign_pages_a_human(monkeypatch, alerts):
    cid = str(uuid4())
    _patch_wrapper(
        monkeypatch,
        campaigns=[{"id": cid, "status": "approved"}],
        rollup=[{"campaign_id": cid, "intended": 100, "delivered": 40, "attempted": 0,
                 "unattributable_delivered": 0}],
    )
    conn = _FakeConn()
    tenant = uuid4()

    approval_resume._report_effect_on_redrive(
        conn, tenant, "task-1", from_status="dead_letter", redriven=True
    )

    assert len(alerts) == 1
    a = alerts[0]
    assert a.trigger_kind == "escalation" and a.severity == "critical"
    assert a.payload["effect_kind"] == "partial_send"
    assert a.payload["delivered"] == 40
    assert a.payload["remainder"] == 60
    assert a.payload["redriven"] is True
    assert a.payload["campaign_ids"] == [cid]
    # The message must say plainly that nothing was blocked — an operator reading it needs to know
    # the send may already have been re-driven, not that the system stopped it.
    assert "NOT blocked" in a.message_text
    assert conn.txn_log == ["enter", "commit"]


def test_settled_campaigns_are_not_treated_as_live_effect(monkeypatch, alerts):
    """Every tenant that ever ran a campaign has 'sent' rows. Counting them would make this alert
    fire on every redrive forever, which is the same as not alerting at all."""
    done, live = str(uuid4()), str(uuid4())
    _patch_wrapper(
        monkeypatch,
        campaigns=[{"id": done, "status": "sent"}, {"id": live, "status": "approved"}],
        rollup=[
            {"campaign_id": done, "intended": 50, "delivered": 50, "attempted": 0,
             "unattributable_delivered": 0},
            {"campaign_id": live, "intended": 10, "delivered": 0, "attempted": 0,
             "unattributable_delivered": 0},
        ],
    )
    approval_resume._report_effect_on_redrive(
        _FakeConn(), uuid4(), "task-2", from_status="blocked", redriven=True
    )
    assert alerts == []


def test_unattributable_sends_are_reported_as_unknown_never_as_clear(monkeypatch, alerts):
    """The agent draft-send path keys sends ``agent:{draft_id}`` and no campaign claims them. Zero
    attributed deliveries with unattributable sends present must NOT read as "nothing went out" —
    that false all-clear on the money path is what the VT-634 fix was for."""
    cid = str(uuid4())
    _patch_wrapper(
        monkeypatch,
        campaigns=[{"id": cid, "status": "approved"}],
        rollup=[{"campaign_id": cid, "intended": 8, "delivered": 0, "attempted": 0,
                 "unattributable_delivered": 3}],
    )
    approval_resume._report_effect_on_redrive(
        _FakeConn(), uuid4(), "task-3", from_status="blocked", redriven=True
    )
    assert len(alerts) == 1
    assert alerts[0].payload["effect_kind"] == "unknown"


def test_a_read_failure_never_unwinds_the_owners_resolution(monkeypatch, alerts):
    """The read runs on the connection the caller has an OPEN transaction on. A failure must roll
    back to the savepoint and be swallowed — never propagate, never alert on garbage."""
    from orchestrator.db.wrappers import CampaignsWrapper

    def _boom(self, t, **k):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(CampaignsWrapper, "list_recent_basic", _boom, raising=True)
    conn = _FakeConn()

    approval_resume._report_effect_on_redrive(
        conn, uuid4(), "task-4", from_status="blocked", redriven=True
    )

    assert alerts == []
    assert conn.txn_log == ["enter", "rollback"], "the read was not inside a SAVEPOINT"


def test_an_alert_failure_never_unwinds_the_owners_resolution(monkeypatch):
    cid = str(uuid4())
    _patch_wrapper(
        monkeypatch,
        campaigns=[{"id": cid, "status": "approved"}],
        rollup=[{"campaign_id": cid, "intended": 5, "delivered": 5, "attempted": 0,
                 "unattributable_delivered": 0}],
    )
    from orchestrator.alerts import dispatch as dispatch_mod

    def _boom(_t):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(dispatch_mod, "dispatch_alert", _boom)
    approval_resume._report_effect_on_redrive(
        _FakeConn(), uuid4(), "task-5", from_status="blocked", redriven=True
    )  # must not raise


def test_the_check_actually_runs_on_the_owner_approval_path(monkeypatch, alerts):
    """REACHABILITY. Drives ``_guarantee_campaign_consumer`` — the real seam — and asserts both
    that the redrive STILL HAPPENS (no block) and that the effect-check ran with the redrive's own
    result. A visibility hook nobody calls is the failure mode this row has already shipped twice.
    """
    from orchestrator.manager import task_store

    cid = str(uuid4())
    _patch_wrapper(
        monkeypatch,
        campaigns=[{"id": cid, "status": "approved"}],
        rollup=[{"campaign_id": cid, "intended": 20, "delivered": 7, "attempted": 1,
                 "unattributable_delivered": 0}],
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        task_store, "find_task_for_resolved_approval",
        lambda t, a, **k: {"id": "task-9", "status": "blocked", "approval_type": "campaign_send"},
    )
    monkeypatch.setattr(
        task_store, "redrive_task",
        lambda t, task, **k: calls.append(("redrive", task)) or True,
    )
    monkeypatch.setattr(
        approval_resume, "_ack_owner_stalled_campaign",
        lambda conn, t, *, reset: calls.append(("ack", reset)),
    )

    approval_resume._guarantee_campaign_consumer(_FakeConn(), uuid4(), uuid4(), "approved")

    assert ("redrive", "task-9") in calls, "the owner's authorization was blocked"
    assert ("ack", True) in calls, "the owner was not told"
    assert len(alerts) == 1
    assert alerts[0].payload["task_id"] == "task-9"
    assert alerts[0].payload["redriven"] is True
    assert alerts[0].payload["effect_kind"] == "partial_send"


def test_a_rejection_still_reports_nothing(monkeypatch, alerts):
    """Unchanged behaviour: only an APPROVED campaign_send reaches the consumer guarantee, so a
    rejection must not alert (it also must not read the ledger)."""
    from orchestrator.manager import task_store

    monkeypatch.setattr(
        task_store, "find_task_for_resolved_approval",
        lambda *a, **k: pytest.fail("a rejection must not reach the consumer guarantee"),
    )
    approval_resume._guarantee_campaign_consumer(_FakeConn(), uuid4(), uuid4(), "rejected")
    assert alerts == []
