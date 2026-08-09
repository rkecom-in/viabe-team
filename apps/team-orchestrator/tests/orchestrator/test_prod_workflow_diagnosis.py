"""VT-634 phase 1 — the diagnosis must be right about what already reached a customer.

Every assertion here is about the one question the spec calls the hard part: a workflow that sent
to SOME customers and died must not be re-run (double-send) and must not be disabled (half a
campaign delivered, owner unaware). Getting `partial_send` wrong in either direction is the whole
risk, so both directions are pinned.
"""

from __future__ import annotations

import pytest

from orchestrator.prod_workflow_diagnosis import (
    DiagnosisUnavailable,
    EffectState,
    WorkflowFinding,
    _parse_workflow_uuid,
    diagnose_failed_workflows,
)


def _finding(*effects: EffectState) -> WorkflowFinding:
    return WorkflowFinding(
        workflow_uuid="manager_task:11111111-1111-1111-1111-111111111111:22222222-2222-2222-2222-222222222222",
        dbos_status="ERROR", tenant_id="11111111-1111-1111-1111-111111111111",
        task_id="22222222-2222-2222-2222-222222222222", task_status="running",
        terminal_outcome=None, effects=list(effects),
    )


class TestEffectState:
    def test_nothing_delivered_is_no_effect(self) -> None:
        e = EffectState("c1", intended=8, delivered=0, attempted_not_delivered=0)
        assert e.kind == "no_effect"
        assert e.remainder == 8

    def test_some_delivered_is_partial_send(self) -> None:
        e = EffectState("c1", intended=8, delivered=3, attempted_not_delivered=0)
        assert e.kind == "partial_send"
        assert e.remainder == 5

    def test_all_delivered_is_complete(self) -> None:
        e = EffectState("c1", intended=8, delivered=8, attempted_not_delivered=0)
        assert e.kind == "complete"
        assert e.remainder == 0

    def test_failed_attempts_do_not_count_as_delivered(self) -> None:
        """A 'window_closed' / 'rate_limited' / 'error' row is an ATTEMPT, not a delivery. If those
        counted as delivered the campaign would look complete and the un-messaged customers would
        vanish from the remainder — the dangerous direction of wrong."""
        e = EffectState("c1", intended=8, delivered=2, attempted_not_delivered=6)
        assert e.kind == "partial_send"
        assert e.remainder == 6, "the 6 failed attempts are still owed a message"

    def test_remainder_never_negative_when_ledger_exceeds_roster(self) -> None:
        """A retry can log more messages than there are recipients. That is not -2 people owed."""
        e = EffectState("c1", intended=3, delivered=5, attempted_not_delivered=0)
        assert e.remainder == 0
        assert e.kind == "complete"


class TestWorstEffectWins:
    def test_one_partial_dominates_a_complete(self) -> None:
        f = _finding(
            EffectState("done", intended=4, delivered=4, attempted_not_delivered=0),
            EffectState("half", intended=6, delivered=2, attempted_not_delivered=0),
        )
        assert f.effect_kind == "partial_send"
        assert f.requires_human is True

    def test_complete_dominates_no_effect(self) -> None:
        f = _finding(
            EffectState("none", intended=5, delivered=0, attempted_not_delivered=0),
            EffectState("done", intended=4, delivered=4, attempted_not_delivered=0),
        )
        assert f.effect_kind == "complete"
        assert f.requires_human is True

    def test_no_campaigns_at_all_is_no_effect_and_automatable(self) -> None:
        f = _finding()
        assert f.effect_kind == "no_effect"
        assert f.requires_human is False
        assert "SAFE TO CANCEL" in f.recommended_action


class TestRecommendedAction:
    def test_partial_send_refuses_both_rerun_and_disable(self) -> None:
        f = _finding(EffectState("half", intended=6, delivered=2, attempted_not_delivered=0))
        action = f.recommended_action
        assert "do not re-run" in action
        assert "do not disable" in action
        assert "4 customer(s)" in action, "the VTR needs the size of the remainder, not just a label"

    def test_complete_warns_about_double_send(self) -> None:
        f = _finding(EffectState("done", intended=4, delivered=4, attempted_not_delivered=0))
        assert "double-send" in f.recommended_action

    def test_anything_delivered_is_never_marked_automatable(self) -> None:
        for delivered in (1, 3, 4):
            f = _finding(EffectState("c", intended=4, delivered=delivered, attempted_not_delivered=0))
            assert f.requires_human is True, f"delivered={delivered} must require a human"


class TestWorkflowUuidParsing:
    def test_parses_the_manager_task_shape(self) -> None:
        assert _parse_workflow_uuid("manager_task:tenant-a:task-b") == ("tenant-a", "task-b")

    @pytest.mark.parametrize("bad", [
        "live_dispatch:tenant-a:run-b",   # a different workflow family
        "manager_task:tenant-a",          # truncated
        "manager_task::task-b",           # empty tenant
        "",
        "garbage",
    ])
    def test_refuses_to_guess(self, bad: str) -> None:
        """A mis-parsed tenant id would attribute one tenant's sends to another — i.e. tell a VTR
        that the wrong customers were messaged. Returning None is the only safe failure."""
        tenant, _task = _parse_workflow_uuid(bad)
        assert tenant is None or bad.startswith("manager_task:tenant-a:")


class TestUnavailableIsNotSilence:
    def test_broken_diagnosis_raises_instead_of_reporting_nothing_wrong(self, monkeypatch) -> None:
        """The failure mode that motivated this: an unreachable DBOS DB returning `[]` renders on
        the console as 'no failed workflows' — the most dangerous sentence this module can emit
        when it is actually blind."""
        import orchestrator.prod_workflow_diagnosis as mod

        def _boom(*_a, **_k):
            raise OSError("system DB unreachable")

        monkeypatch.setattr(mod, "_read_dbos_rows", _boom)
        with pytest.raises(DiagnosisUnavailable):
            diagnose_failed_workflows(pool=object())

    def test_findings_are_ordered_worst_first(self, monkeypatch) -> None:
        """A VTR works the list top-down, so the customer-affecting rows must be at the top."""
        # orchestrator.manager.task_store pulls psycopg, which the dep-less smoke suite (mirrors
        # CI 'test') does not install.
        pytest.importorskip("psycopg")
        import orchestrator.prod_workflow_diagnosis as mod

        from orchestrator.manager import task_store

        monkeypatch.setattr(mod, "_read_dbos_rows", lambda *_a, **_k: [
            ("manager_task:t1:a", "ERROR", None),
            ("manager_task:t2:b", "PENDING", None),
        ])
        # The per-tenant reads go through the RLS-scoped accessors now, so both must be stubbed —
        # without the task_store stub the pool lookup raises, the per-tenant guard catches it, and
        # every finding comes back with NO effects (correctly marked unreadable, but useless for
        # an ordering assertion).
        monkeypatch.setattr(task_store, "get_task", lambda *_a, **_k: {"status": "running"})
        monkeypatch.setattr(mod, "_effect_states", lambda _c, tenant, _t: (
            [EffectState("c", intended=5, delivered=0, attempted_not_delivered=0)] if tenant == "t1"
            else [EffectState("c", intended=5, delivered=2, attempted_not_delivered=0)]
        ))
        findings = diagnose_failed_workflows(pool=object())
        assert [f.effect_kind for f in findings] == ["partial_send", "no_effect"]


class TestUnreadableEffectsAreNotNoEffect:
    def test_a_tenant_whose_effects_cannot_be_read_is_flagged_not_dropped(self, monkeypatch) -> None:
        """A workflow whose effect-state is unreadable is exactly the one a human must look at.
        It must not silently render as 'nothing reached a customer' — and it must not vanish from
        the list either, which would be the same lie by omission."""
        # orchestrator.manager.task_store pulls psycopg, which the dep-less smoke
        # suite does not install.
        pytest.importorskip("psycopg")
        import orchestrator.prod_workflow_diagnosis as mod
        from orchestrator.manager import task_store

        monkeypatch.setattr(mod, "_read_dbos_rows",
                            lambda *_a, **_k: [("manager_task:t1:a", "ERROR", None)])
        monkeypatch.setattr(task_store, "get_task", lambda *_a, **_k: {"status": "running"})

        def _unreadable(*_a, **_k):
            raise OSError("tenant DB unreachable")

        monkeypatch.setattr(mod, "_effect_states", _unreadable)

        findings = diagnose_failed_workflows(pool=object())
        assert len(findings) == 1, "the unreadable workflow must still be reported"
        assert "UNREADABLE" in (findings[0].error_summary or ""), (
            "the finding must carry the fact that effect-state could not be read, so it is never "
            "mistaken for a clean no-effect result"
        )


class _FakeConn:
    def execute(self, *_a, **_k):
        return self

    def fetchone(self):
        return ("running", None)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _FakePool:
    def connection(self):
        return _FakeConn()
