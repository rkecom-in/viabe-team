"""VT-633 F-2 — ``workflow._execute_approved_campaign``'s own PURE unit tests.

DBOS's ``@DBOS.step()`` decorator runs the wrapped function as a plain call when there is no
active DBOS workflow context (``dbos/_core.py``'s ``decorate_step``: "If the step is called from a
workflow, run it as a step. Otherwise, run it as a normal function.") — these tests call
``_execute_approved_campaign`` directly, outside any workflow, so NO DBOS launch / live Postgres is
needed. Every DB read (``PendingApprovalsWrapper`` / ``CampaignsWrapper``) and the run-control check
are monkeypatched at the CLASS/module level; ``execute_approved_campaign`` (the real money-path
fan-out) is ALWAYS monkeypatched here too — these tests must NEVER drive a real send.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

pytest.importorskip("dbos")
pytest.importorskip("psycopg")


@contextmanager
def _fake_tenant_connection(tenant_id):
    yield object()


@pytest.fixture(autouse=True)
def _no_real_db(monkeypatch: pytest.MonkeyPatch):
    """Every test in this file monkeypatches its own wrapper reads explicitly; this just keeps
    ``tenant_connection`` from ever trying to open a real connection if a test reaches the
    execute_approved_campaign call."""
    import orchestrator.manager.workflow as wf

    monkeypatch.setattr(wf, "tenant_connection", _fake_tenant_connection)


def _mock_approval(monkeypatch, wf, approval: dict | None) -> None:
    from orchestrator.db.wrappers import PendingApprovalsWrapper

    monkeypatch.setattr(
        PendingApprovalsWrapper, "approval_for_run", lambda self, tenant_id, run_id: approval
    )


def _mock_campaign_status(monkeypatch, wf, status: str | None) -> None:
    from orchestrator.db.wrappers import CampaignsWrapper

    monkeypatch.setattr(CampaignsWrapper, "get_status", lambda self, tenant_id, campaign_id: status)


def _mock_check_pause(monkeypatch, wf, held: bool) -> None:
    # _execute_approved_campaign imports check_pause LOCALLY (mirrors supervisor.py's own style),
    # so patching has to target the source module, not a wf-module attribute.
    import orchestrator.run_control as run_control

    monkeypatch.setattr(run_control, "check_pause", lambda tenant_id, kind: held)


def test_approved_campaign_executes_once_and_returns_summary(monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf

    _mock_approval(
        monkeypatch, wf,
        {"decision": "approved", "approval_type": "campaign_send", "campaign_id": "camp-1"},
    )
    _mock_campaign_status(monkeypatch, wf, "approved")
    _mock_check_pause(monkeypatch, wf, held=False)

    calls: list[tuple] = []

    def _fake_execute(tenant_id, campaign_id, *, conn):
        calls.append((tenant_id, campaign_id))
        return {"sent": 3, "skipped_opt_out": 1, "skipped_complaint_freeze": 0, "failed": 0, "killed": 0}

    monkeypatch.setattr("orchestrator.campaign.execute.execute_approved_campaign", _fake_execute)

    result = wf._execute_approved_campaign("tenant-1", "task-1", "step-1", 1)

    assert result == {
        "executed": True,
        "summary": {
            "sent": 3, "skipped_opt_out": 1, "skipped_complaint_freeze": 0, "failed": 0, "killed": 0,
        },
    }
    assert calls == [("tenant-1", "camp-1")]  # executed exactly once


def test_non_campaign_approval_is_a_no_op(monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf

    _mock_approval(
        monkeypatch, wf,
        {"decision": "approved", "approval_type": "sensitive_data_access", "campaign_id": None},
    )

    def _must_not_execute(*a, **k):
        raise AssertionError("execute_approved_campaign must not be called for a non-campaign approval")

    monkeypatch.setattr("orchestrator.campaign.execute.execute_approved_campaign", _must_not_execute)

    result = wf._execute_approved_campaign("tenant-1", "task-1", "step-1", 1)

    assert result == {
        "executed": False,
        "reason": "not_a_campaign_send_approval:sensitive_data_access",
    }


def test_run_control_hold_blocks_execution(monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf

    _mock_approval(
        monkeypatch, wf,
        {"decision": "approved", "approval_type": "campaign_send", "campaign_id": "camp-1"},
    )
    _mock_campaign_status(monkeypatch, wf, "proposed")
    _mock_check_pause(monkeypatch, wf, held=True)

    def _must_not_execute(*a, **k):
        raise AssertionError("execute_approved_campaign must not be called while held")

    monkeypatch.setattr("orchestrator.campaign.execute.execute_approved_campaign", _must_not_execute)

    result = wf._execute_approved_campaign("tenant-1", "task-1", "step-1", 1)

    assert result == {"executed": False, "reason": "run_control_hold"}


def test_execute_raising_never_propagates(monkeypatch: pytest.MonkeyPatch):
    import orchestrator.manager.workflow as wf

    _mock_approval(
        monkeypatch, wf,
        {"decision": "approved", "approval_type": "campaign_send", "campaign_id": "camp-1"},
    )
    _mock_campaign_status(monkeypatch, wf, "proposed")
    _mock_check_pause(monkeypatch, wf, held=False)

    def _boom(*a, **k):
        raise RuntimeError("twilio blew up")

    monkeypatch.setattr("orchestrator.campaign.execute.execute_approved_campaign", _boom)

    result = wf._execute_approved_campaign("tenant-1", "task-1", "step-1", 1)

    assert result == {"executed": False, "reason": "error:RuntimeError"}


def test_no_approval_row_is_a_no_op(monkeypatch: pytest.MonkeyPatch):
    """Defensive: a run_id with no approval row at all (should not happen in practice — the
    approved-branch only reaches here off an already-'approved' decision) is a clean no-op, not a
    crash."""
    import orchestrator.manager.workflow as wf

    _mock_approval(monkeypatch, wf, None)

    result = wf._execute_approved_campaign("tenant-1", "task-1", "step-1", 1)

    assert result == {"executed": False, "reason": "no_approval_row"}


def test_campaign_already_sent_is_not_re_executed(monkeypatch: pytest.MonkeyPatch):
    """The idempotency guard: a DBOS retry that lands after execute_approved_campaign already
    advanced the campaign to 'sent' must NOT re-enter the fan-out (a second real send)."""
    import orchestrator.manager.workflow as wf

    _mock_approval(
        monkeypatch, wf,
        {"decision": "approved", "approval_type": "campaign_send", "campaign_id": "camp-1"},
    )
    _mock_campaign_status(monkeypatch, wf, "sent")

    def _must_not_execute(*a, **k):
        raise AssertionError("execute_approved_campaign must not be called for an already-sent campaign")

    monkeypatch.setattr("orchestrator.campaign.execute.execute_approved_campaign", _must_not_execute)

    result = wf._execute_approved_campaign("tenant-1", "task-1", "step-1", 1)

    assert result == {"executed": False, "reason": "campaign_not_pending_execution:sent"}
