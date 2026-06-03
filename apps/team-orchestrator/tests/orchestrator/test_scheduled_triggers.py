"""Tests for VT-28 scheduled triggers — pure unit tests.

Workflow_id derivation, shell-event payload shape, Pillar 1 isolation
(deterministic triggers never import LLM modules), and the
register-before-launch idempotency guard.

Real DBOS / Anthropic / pipeline_log integration lives in the canary
(``canaries/vt28_scheduled_triggers.py``).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")

from orchestrator import scheduled_triggers as st  # noqa: E402
from orchestrator.observability import log as log_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _phone_salt(monkeypatch):
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "test-salt-vt28")


def _captured_payloads(monkeypatch) -> list[tuple[Any, ...]]:
    captured: list[tuple[Any, ...]] = []

    def _capture(event_type, run_id, tenant_id, severity, component, payload, duration_ms):
        captured.append((event_type, run_id, tenant_id, severity, component, payload, duration_ms))

    monkeypatch.setattr(log_mod, "_do_insert_sync", _capture)
    return captured


# ---------------------------------------------------------------------------
# 1. workflow_id derivation — deterministic per VT-28 §1-4
# ---------------------------------------------------------------------------

def test_weekly_workflow_id_format() -> None:
    tenant = uuid4()
    assert st.weekly_workflow_id(tenant, "2026-W22") == f"weekly:{tenant}:2026-W22"


def test_attribution_close_workflow_id_format() -> None:
    campaign = uuid4()
    assert (
        st.attribution_close_workflow_id(campaign)
        == f"attribution_close:{campaign}"
    )


def test_day39_workflow_id_format() -> None:
    tenant = uuid4()
    assert st.day39_workflow_id(tenant) == f"day39:{tenant}"


def test_monthly_workflow_id_format() -> None:
    tenant = uuid4()
    assert st.monthly_workflow_id(tenant, "2026-05") == f"monthly:{tenant}:2026-05"


def test_cross_trigger_isolation_different_namespaces() -> None:
    """Same numeric value across trigger types yields distinct workflow_ids."""
    same = UUID("00000000-0000-4000-8000-000000000001")
    ids = {
        st.attribution_close_workflow_id(same),
        st.day39_workflow_id(same),
        st.monthly_workflow_id(same, "2026-05"),
        st.weekly_workflow_id(same, "2026-W22"),
    }
    assert len(ids) == 4


# ---------------------------------------------------------------------------
# 2. Cron expressions — IST cadence per brief §Phase 1
# ---------------------------------------------------------------------------

def test_cron_expressions_match_brief() -> None:
    assert st.WEEKLY_CADENCE_CRON == "0 9 * * MON"
    assert st.ATTRIBUTION_CLOSE_CRON == "0 2 * * *"
    assert st.DAY39_EVALUATION_CRON == "0 6 * * *"
    assert st.MONTHLY_IMPACT_CRON == "0 8 1 * *"
    # VT-47 — 5th trigger: owner-approval timeout sweep, every 30 min.
    assert st.APPROVAL_TIMEOUT_SWEEP_CRON == "*/30 * * * *"


# ---------------------------------------------------------------------------
# 3. Shell-event emission (Cond 2 — phantom-Done prevention)
# ---------------------------------------------------------------------------

def test_attribution_close_body_delegates_to_billing_module(monkeypatch) -> None:
    """VT-176: body scans eligibility + calls billing.close_attribution per row.

    Monkeypatch the scanner + the billing function; assert exactly one
    delegated call per eligible candidate. No DB / no real billing.
    """
    eligible = [UUID("00000000-0000-4000-8000-000000aaa176")]
    monkeypatch.setattr(st, "_scan_attribution_close_eligible", lambda now: eligible)

    called_with: list[UUID] = []

    def _fake_close(campaign_id):
        called_with.append(campaign_id)
        from types import SimpleNamespace

        return SimpleNamespace(campaign_id=campaign_id, total_arrr_paise=0)

    import orchestrator.billing.attribution_close as ac_mod

    monkeypatch.setattr(ac_mod, "close_attribution", _fake_close)

    out = st.run_attribution_close_body(
        now=datetime(2026, 5, 26, 2, 0, tzinfo=timezone.utc)
    )
    assert called_with == eligible
    assert out == eligible


def test_day39_body_delegates_and_invokes_refund_transition(monkeypatch) -> None:
    """VT-176: body scans eligibility + calls billing.evaluate_day39.

    Refund-verdict tenant also triggers a transition attempt (best-effort
    wrapped). Monkeypatch the scanner + the evaluator + the transition
    helper; assert the call graph.
    """
    eligible = [UUID("00000000-0000-4000-8000-000000bbb176")]
    monkeypatch.setattr(st, "_scan_day39_eligible", lambda now: eligible)

    from types import SimpleNamespace

    refund_verdict = SimpleNamespace(
        tenant_id=eligible[0],
        verdict="refund_triggered",
        already_decided=False,
    )

    import orchestrator.billing.day39_evaluator as eval_mod

    monkeypatch.setattr(eval_mod, "evaluate_day39", lambda tid: refund_verdict)

    transition_calls: list[UUID] = []
    monkeypatch.setattr(
        st, "_apply_day39_refund_transition", lambda tid: transition_calls.append(tid)
    )

    out = st.run_day39_evaluation_body(
        now=datetime(2026, 5, 26, 6, 0, tzinfo=timezone.utc)
    )
    assert len(out) == 1
    assert out[0].verdict == "refund_triggered"
    assert transition_calls == eligible


def test_day39_body_continue_branch_skips_refund_transition(monkeypatch) -> None:
    """VT-176: continue verdict does NOT call apply_transition."""
    eligible = [UUID("00000000-0000-4000-8000-000000ccc176")]
    monkeypatch.setattr(st, "_scan_day39_eligible", lambda now: eligible)

    from types import SimpleNamespace

    cont_verdict = SimpleNamespace(
        tenant_id=eligible[0],
        verdict="continue",
        already_decided=False,
    )

    import orchestrator.billing.day39_evaluator as eval_mod

    monkeypatch.setattr(eval_mod, "evaluate_day39", lambda tid: cont_verdict)

    transition_calls: list[UUID] = []
    monkeypatch.setattr(
        st, "_apply_day39_refund_transition", lambda tid: transition_calls.append(tid)
    )

    st.run_day39_evaluation_body(now=datetime(2026, 5, 26, 6, 0, tzinfo=timezone.utc))
    assert transition_calls == []


def test_day39_body_replays_skip_refund_transition(monkeypatch) -> None:
    """VT-176: already_decided=True replay skips the transition call."""
    eligible = [UUID("00000000-0000-4000-8000-000000ddd176")]
    monkeypatch.setattr(st, "_scan_day39_eligible", lambda now: eligible)

    from types import SimpleNamespace

    replay_verdict = SimpleNamespace(
        tenant_id=eligible[0],
        verdict="refund_triggered",
        already_decided=True,
    )

    import orchestrator.billing.day39_evaluator as eval_mod

    monkeypatch.setattr(eval_mod, "evaluate_day39", lambda tid: replay_verdict)

    transition_calls: list[UUID] = []
    monkeypatch.setattr(
        st, "_apply_day39_refund_transition", lambda tid: transition_calls.append(tid)
    )

    st.run_day39_evaluation_body(now=datetime(2026, 5, 26, 6, 0, tzinfo=timezone.utc))
    assert transition_calls == [], "replay should not re-trigger the transition"


# ---------------------------------------------------------------------------
# 4. Weekly cadence — emits real event (full implementation, NOT a shell)
# ---------------------------------------------------------------------------

def test_monthly_impact_body_emits_started_event(monkeypatch) -> None:
    """VT-176: monthly impact body emits ``monthly_impact_started`` event
    per eligible tenant. Monkeypatched DB returns one eligible tenant."""
    captured = _captured_payloads(monkeypatch)
    eligible_id = UUID("00000000-0000-4000-8000-000000eee176")

    # Monkeypatch get_pool to return a fake connection that yields one row.
    class _FakeCursor:
        def execute(self, *args, **kwargs):
            self._stored_args = args

        def fetchall(self):
            return [{"id": eligible_id}]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeConn:
        def cursor(self, row_factory=None):
            return _FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakePool:
        def connection(self):
            return _FakeConn()

    from orchestrator import graph as graph_mod

    monkeypatch.setattr(graph_mod, "_pool", _FakePool())

    out = st.run_monthly_impact_body(
        now=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    )
    time.sleep(0.05)
    assert eligible_id in out
    assert captured, "log_event never reached"
    event_type, _, _, _, _, payload, _ = captured[0]
    assert event_type == "monthly_impact_started"
    assert payload["tenant_id"] == str(eligible_id)
    assert payload["target_month"] == "2026-06"


def test_weekly_cadence_emits_full_event_not_shell(monkeypatch) -> None:
    captured = _captured_payloads(monkeypatch)
    st.run_weekly_cadence_body(
        now=datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc)
    )
    time.sleep(0.05)
    assert captured
    event_type, _, _, _, _, payload, _ = captured[0]
    assert event_type == "weekly_cadence_fired"  # not a shell — has real path
    assert payload["trigger_reason"] == "weekly_cadence"
    assert payload["anthropic_invoked"] is True


# ---------------------------------------------------------------------------
# VT-47 — owner-approval timeout sweep body (5th trigger)
# ---------------------------------------------------------------------------


class _SweepConn:
    """Captures the UPDATE pipeline_runs the sweep issues after a resume."""

    def __init__(self):
        self.updates: list[tuple] = []

    def execute(self, sql, params=None):
        if "UPDATE pipeline_runs" in sql:
            self.updates.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_approval_timeout_sweep_resolves_and_resumes(monkeypatch) -> None:
    """VT-47: a past-timeout open approval is resolved with decision='timeout'
    and its paused run is resumed via resume_run('timeout'). The body returns
    the resolved approval ids for canary inspection."""
    captured = _captured_payloads(monkeypatch)
    tid = str(uuid4())
    rid = str(uuid4())
    aid = str(uuid4())

    monkeypatch.setattr(
        st, "_scan_timed_out_approvals",
        lambda now: [{"id": aid, "tenant_id": tid, "run_id": rid}],
    )

    # No real DB: tenant_connection yields a capture conn; mark_resolved + resume
    # are stubbed. Patch the symbols where the body imports them.
    sweep_conn = _SweepConn()
    monkeypatch.setattr(
        "orchestrator.db.tenant_connection", lambda t: sweep_conn
    )
    marked: list[tuple] = []
    monkeypatch.setattr(
        "orchestrator.agent.approval_resume.mark_approval_resolved",
        lambda conn, approval_id, decision, **kw: marked.append((approval_id, decision)),
    )
    resumed: list[tuple] = []
    monkeypatch.setattr(
        "orchestrator.agent.approval_resume.resume_run",
        lambda run_id, decision: resumed.append((run_id, decision)) or {},
    )

    out = st.run_approval_timeout_sweep_body(
        now=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    )

    assert out == [UUID(aid)]
    assert marked == [(aid, "timeout")]
    assert resumed == [(rid, "timeout")]
    # The original paused run was driven to completed.
    assert sweep_conn.updates, "sweep must close the paused run"
    # CL-390: the emitted event carries ids + decision only, no PII.
    time.sleep(0.05)
    assert captured
    event_type, _, _, _, _, payload, _ = captured[0]
    assert event_type == st.APPROVAL_TIMED_OUT_EVENT
    assert payload["decision"] == "timeout"
    assert payload["approval_id"] == aid
    assert "phone" not in str(payload).lower()


def test_approval_timeout_sweep_empty_is_noop(monkeypatch) -> None:
    """No timed-out approvals -> empty result, no resume calls."""
    monkeypatch.setattr(st, "_scan_timed_out_approvals", lambda now: [])
    out = st.run_approval_timeout_sweep_body(
        now=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    )
    assert out == []


def test_approval_timeout_sweep_one_failure_does_not_halt(monkeypatch) -> None:
    """Per-approval try/except: one stuck resume must not abort the sweep."""
    tid, r1, r2 = str(uuid4()), str(uuid4()), str(uuid4())
    a1, a2 = str(uuid4()), str(uuid4())
    monkeypatch.setattr(
        st, "_scan_timed_out_approvals",
        lambda now: [
            {"id": a1, "tenant_id": tid, "run_id": r1},
            {"id": a2, "tenant_id": tid, "run_id": r2},
        ],
    )
    monkeypatch.setattr(
        "orchestrator.db.tenant_connection", lambda t: _SweepConn()
    )
    monkeypatch.setattr(
        "orchestrator.agent.approval_resume.mark_approval_resolved",
        lambda conn, approval_id, decision, **kw: None,
    )

    def _resume(run_id, decision):
        if run_id == r1:
            raise RuntimeError("stuck")
        return {}

    monkeypatch.setattr(
        "orchestrator.agent.approval_resume.resume_run", _resume
    )
    out = st.run_approval_timeout_sweep_body(
        now=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    )
    # a1 failed; a2 still resolved — the sweep continued.
    assert out == [UUID(a2)]


# ---------------------------------------------------------------------------
# 5. Pillar 1 — deterministic bodies must NOT import LLM modules
# ---------------------------------------------------------------------------

def test_deterministic_bodies_do_not_import_orchestrator_agent() -> None:
    """The 3 deterministic trigger bodies must not transitively pull in
    ChatAnthropic / Anthropic / orchestrator_agent / supervisor.

    Direct check: re-import the module and verify the relevant names
    aren't in its namespace (would indicate an accidental `from
    orchestrator.agent.orchestrator_agent import ...` statement).
    """
    import orchestrator.scheduled_triggers as mod

    forbidden = {
        "ChatAnthropic",
        "Anthropic",
        "orchestrator_agent",
        "supervisor",
        "build_orchestrator_agent",
    }
    for name in forbidden:
        assert name not in dir(mod), (
            f"deterministic trigger module leaks {name!r} — Pillar 1 violation"
        )


# ---------------------------------------------------------------------------
# 6. Scheduled handler signatures match DBOS @scheduled contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fn",
    [
        st.weekly_cadence_scheduled,
        st.attribution_close_scheduled,
        st.day39_evaluation_scheduled,
        st.monthly_impact_scheduled,
    ],
)
def test_scheduled_handler_accepts_scheduled_and_actual_time(monkeypatch, fn) -> None:
    """DBOS scheduled-handler signature smoke. Bodies that scan eligibility
    are stubbed to return empty so we exercise the signature without DB."""
    _captured_payloads(monkeypatch)
    monkeypatch.setattr(st, "_scan_attribution_close_eligible", lambda now: [])
    monkeypatch.setattr(st, "_scan_day39_eligible", lambda now: [])

    # Monthly impact body queries the pool inline; stub the pool getter.
    class _EmptyCursor:
        def execute(self, *a, **k): pass
        def fetchall(self): return []
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    class _EmptyConn:
        def cursor(self, row_factory=None): return _EmptyCursor()
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    class _EmptyPool:
        def connection(self): return _EmptyConn()

    from orchestrator import graph as graph_mod
    monkeypatch.setattr(graph_mod, "_pool", _EmptyPool())

    fake_scheduled = datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 5, 26, 9, 0, 12, tzinfo=timezone.utc)
    fn(fake_scheduled, fake_actual)


# ---------------------------------------------------------------------------
# 7. register_scheduled_triggers idempotency
# ---------------------------------------------------------------------------

def test_register_scheduled_triggers_idempotent(monkeypatch) -> None:
    """Two calls should not raise; second call is a no-op short-circuit.

    Migrated from VT-28 canary Assertion #10 per VT-176 review §Condition 1
    (architectural-invariant check, not a runtime-API check — belongs as a
    pure unit test). DBOS scheduled-poller registration MUST be idempotent
    because re-registering shifts the launch-time ``app_version`` hash and
    breaks the recovery filter at ``_recovery.py:58``.
    """
    from dbos import DBOS
    call_count = {"n": 0}

    def _fake_scheduled(cron):
        def _wrap(fn):
            call_count["n"] += 1
            return fn
        return _wrap

    monkeypatch.setattr(DBOS, "scheduled", _fake_scheduled)
    st._registered = False
    st.register_scheduled_triggers()
    first = call_count["n"]
    st.register_scheduled_triggers()
    second = call_count["n"]
    # VT-47 added the 5th trigger (owner-approval timeout sweep); VT-68 added the
    # 6th (nightly L3 construction).
    assert first == 6, "expected 6 triggers registered on first call"
    assert second == 6, "second call must short-circuit (idempotent)"
    st._registered = False
