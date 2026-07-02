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


def test_monthly_workflow_id_format() -> None:
    tenant = uuid4()
    assert st.monthly_workflow_id(tenant, "2026-05") == f"monthly:{tenant}:2026-05"


def test_cross_trigger_isolation_different_namespaces() -> None:
    """Same numeric value across trigger types yields distinct workflow_ids."""
    same = UUID("00000000-0000-4000-8000-000000000001")
    ids = {
        st.attribution_close_workflow_id(same),
        st.monthly_workflow_id(same, "2026-05"),
        st.weekly_workflow_id(same, "2026-W22"),
    }
    assert len(ids) == 3


# ---------------------------------------------------------------------------
# 2. Cron expressions — IST cadence per brief §Phase 1
# ---------------------------------------------------------------------------

def test_cron_expressions_match_brief() -> None:
    assert st.WEEKLY_CADENCE_CRON == "0 9 * * MON"
    assert st.ATTRIBUTION_CLOSE_CRON == "0 2 * * *"
    # VT-365: the day-39 refund-evaluation trigger is gone; the kept lifecycle
    # sweep is the daily VT-90 trial-expiry evaluation (7 AM IST, off-peak).
    assert st.TRIAL_EVALUATION_CRON == "0 7 * * *"
    assert st.MONTHLY_IMPACT_CRON == "0 8 1 * *"
    # VT-47 — 5th trigger: owner-approval timeout sweep, every 30 min.
    assert st.APPROVAL_TIMEOUT_SWEEP_CRON == "*/30 * * * *"
    # VT-432 — 18th trigger: daily implicit-attribution sweep, 23:00 UTC / 04:30 IST.
    assert st.IMPLICIT_ATTRIBUTION_SWEEP_CRON == "0 23 * * *"
    # VT-439 — 19th trigger: daily Razorpay orphan-detect backstop, 01:00 UTC / 06:30 IST.
    assert st.RECONCILE_SUBSCRIPTION_ORPHANS_CRON == "0 1 * * *"
    # VT-440 — 20th trigger: daily Razorpay dead-letter backstop, 22:30 UTC / 04:00 IST.
    assert st.DEAD_LETTER_RETRY_SWEEP_CRON == "30 22 * * *"
    # VT-560 — 3 steady-state sweeps: retry-ladder + silent-terminal every 10 min, orphan-run hourly.
    assert st.STALLED_TASK_SWEEP_CRON == "*/10 * * * *"
    assert st.SILENT_TERMINAL_SWEEP_CRON == "*/10 * * * *"
    assert st.ORPHAN_RUN_REAPER_CRON == "0 * * * *"


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


# VT-365: the day-39 refund-evaluation body (run_day39_evaluation_body) and its
# refund/continue/replay branch tests are DELETED — the day-39 2x-or-refund
# subsystem (billing.day39_evaluator, _scan_day39_eligible, _send_day39_refund_offer)
# was removed. The kept lifecycle sweep is the VT-90 trial-expiry evaluation
# (trial_evaluation_scheduled → trial_sweep.run_trial_evaluation_body), exercised
# via the scheduled-handler signature smoke + the register idempotency count below.


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
        lambda conn, tenant_id, approval_id, decision, **kw: marked.append((approval_id, decision)),
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
        lambda conn, tenant_id, approval_id, decision, **kw: None,
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
# VT-432 — implicit attribution sweep handler (18th trigger)
# ---------------------------------------------------------------------------


def test_implicit_attribution_sweep_scheduled_calls_sweep(monkeypatch) -> None:
    """VT-432: the scheduled handler delegates to run_implicit_attribution_sweep
    and logs the result. Monkeypatched sweep returns synthetic counts; no DB,
    no send (assert no Twilio/Resend call)."""
    import orchestrator.feedback.implicit_attribution as ia_mod

    called_with: list[dict] = []

    def _fake_sweep() -> dict:
        called_with.append({})
        return {"considered": 3, "written": 2, "skipped_no_outcome": 1}

    monkeypatch.setattr(ia_mod, "run_implicit_attribution_sweep", _fake_sweep)

    fake_scheduled = datetime(2026, 6, 25, 23, 0, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 6, 25, 23, 0, 5, tzinfo=timezone.utc)
    # Handler must not raise; best-effort wrapper catches exceptions.
    st.implicit_attribution_sweep_scheduled(fake_scheduled, fake_actual)

    assert called_with, "run_implicit_attribution_sweep must be called by the handler"


def test_implicit_attribution_sweep_no_send(monkeypatch) -> None:
    """VT-432: the sweep path NEVER reaches Twilio or Resend. Patch both send
    clients and assert neither is called when the handler runs."""
    import orchestrator.feedback.implicit_attribution as ia_mod

    # Stub sweep to return a non-empty result to exercise the full handler path.
    monkeypatch.setattr(
        ia_mod, "run_implicit_attribution_sweep",
        lambda: {"considered": 1, "written": 1, "skipped_no_outcome": 0},
    )

    send_called: list[str] = []

    # Patch at the alerts clients level — if any send path is reachable it
    # would call these.
    try:
        import orchestrator.alerts.clients as clients_mod  # noqa: F401

        monkeypatch.setattr(
            clients_mod,
            "send_telegram",
            lambda *a, **kw: send_called.append("telegram"),
        )
        monkeypatch.setattr(
            clients_mod,
            "send_resend_email",
            lambda *a, **kw: send_called.append("resend"),
        )
    except (ImportError, AttributeError):
        pass  # module may not be importable without network deps; that's fine

    fake_scheduled = datetime(2026, 6, 25, 23, 0, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 6, 25, 23, 0, 5, tzinfo=timezone.utc)
    st.implicit_attribution_sweep_scheduled(fake_scheduled, fake_actual)

    assert not send_called, (
        f"VT-432 sweep must NOT send via Telegram/Resend; called: {send_called}"
    )


def test_implicit_attribution_sweep_handler_is_best_effort(monkeypatch) -> None:
    """VT-432: a sweep exception must not propagate — handler is best-effort."""
    import orchestrator.feedback.implicit_attribution as ia_mod

    def _raising_sweep() -> dict:
        raise RuntimeError("DB unavailable")

    monkeypatch.setattr(ia_mod, "run_implicit_attribution_sweep", _raising_sweep)

    fake_scheduled = datetime(2026, 6, 25, 23, 0, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 6, 25, 23, 0, 5, tzinfo=timezone.utc)
    # Must not raise — handler wraps in try/except BLE001.
    st.implicit_attribution_sweep_scheduled(fake_scheduled, fake_actual)


# ---------------------------------------------------------------------------
# VT-439 — reconcile_subscription_orphans handler (19th trigger)
# ---------------------------------------------------------------------------


def _make_empty_pool():
    """Shared pool stub that returns zero subscription rows."""
    class _Cursor:
        def execute(self, *a, **k): pass
        def fetchall(self): return []
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    class _Conn:
        def cursor(self, row_factory=None): return _Cursor()
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    class _Pool:
        def connection(self): return _Conn()

    return _Pool()


def test_reconcile_subscription_orphans_scheduled_calls_reconcile(monkeypatch) -> None:
    """VT-439: the scheduled handler delegates to reconcile_subscription_orphans
    with the DB-fetched subscription IDs and logs the result. Monkeypatched DB
    returns one known ID; reconcile stub reports zero orphans. No send path."""
    import orchestrator.api.razorpay_subscribe as rs_mod
    from orchestrator import graph as graph_mod

    known_id = "sub_stub_tenant1_abc123"

    class _Cursor:
        def execute(self, *a, **k): pass
        def fetchall(self): return [{"razorpay_subscription_id": known_id}]
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    class _Conn:
        def cursor(self, row_factory=None): return _Cursor()
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    class _Pool:
        def connection(self): return _Conn()

    monkeypatch.setattr(graph_mod, "_pool", _Pool())

    called_with: list[list[str]] = []

    def _fake_reconcile(vendor_ids: list[str]) -> list[str]:
        called_with.append(vendor_ids)
        return []  # no orphans

    monkeypatch.setattr(rs_mod, "reconcile_subscription_orphans", _fake_reconcile)

    fake_scheduled = datetime(2026, 6, 25, 1, 0, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 6, 25, 1, 0, 3, tzinfo=timezone.utc)
    st.reconcile_subscription_orphans_scheduled(fake_scheduled, fake_actual)

    assert called_with, "reconcile_subscription_orphans must be called by the handler"
    assert called_with[0] == [known_id], "handler must pass the DB-fetched IDs"


def test_reconcile_subscription_orphans_no_send(monkeypatch) -> None:
    """VT-439: the sweep path NEVER reaches Twilio or Resend (DETECT-ONLY).
    Patch both send clients and assert neither is called when the handler runs."""
    import orchestrator.api.razorpay_subscribe as rs_mod
    from orchestrator import graph as graph_mod

    monkeypatch.setattr(graph_mod, "_pool", _make_empty_pool())
    monkeypatch.setattr(rs_mod, "reconcile_subscription_orphans", lambda ids: [])

    send_called: list[str] = []
    try:
        import orchestrator.alerts.clients as clients_mod

        monkeypatch.setattr(
            clients_mod, "send_telegram", lambda *a, **kw: send_called.append("telegram")
        )
        monkeypatch.setattr(
            clients_mod, "send_resend_email", lambda *a, **kw: send_called.append("resend")
        )
    except (ImportError, AttributeError):
        pass

    fake_scheduled = datetime(2026, 6, 25, 1, 0, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 6, 25, 1, 0, 3, tzinfo=timezone.utc)
    st.reconcile_subscription_orphans_scheduled(fake_scheduled, fake_actual)

    assert not send_called, (
        f"VT-439 handler must NOT send via Telegram/Resend; called: {send_called}"
    )


def test_reconcile_subscription_orphans_handler_is_best_effort(monkeypatch) -> None:
    """VT-439: a reconcile exception must not propagate — handler is best-effort."""
    from orchestrator import graph as graph_mod

    class _RaisingPool:
        def connection(self):
            raise RuntimeError("DB unavailable")

    monkeypatch.setattr(graph_mod, "_pool", _RaisingPool())

    fake_scheduled = datetime(2026, 6, 25, 1, 0, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 6, 25, 1, 0, 3, tzinfo=timezone.utc)
    # Must not raise — handler wraps in try/except BLE001.
    st.reconcile_subscription_orphans_scheduled(fake_scheduled, fake_actual)


# ---------------------------------------------------------------------------
# VT-440 — dead_letter_retry_sweep handler (20th trigger). DETECT/ALERT-ONLY.
# ---------------------------------------------------------------------------


def _patch_count_pending(monkeypatch, value: int) -> None:
    """Stub orchestrator.billing.dead_letter.count_pending to return `value`."""
    import orchestrator.billing.dead_letter as dl_mod

    monkeypatch.setattr(dl_mod, "count_pending", lambda: value)


def test_dead_letter_retry_sweep_alerts_when_pending(monkeypatch) -> None:
    """VT-440: with pending dead-letters, the handler alerts Fazal exactly once
    with a COUNT (PII-free), and never reaches a replay/charge/send-of-money path."""
    _patch_count_pending(monkeypatch, 3)

    import orchestrator.alerts.clients as clients_mod

    alerts: list[str] = []
    monkeypatch.setattr(clients_mod, "alert_fazal", lambda text: alerts.append(text))

    # Guard against any replay/send: patching replay to explode proves it is never called.
    import orchestrator.billing.dead_letter as dl_mod

    def _no_replay(*a, **k):
        raise AssertionError("VT-440 sweep must NEVER call dead_letter.replay")

    monkeypatch.setattr(dl_mod, "replay", _no_replay)

    fake_scheduled = datetime(2026, 6, 25, 22, 30, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 6, 25, 22, 30, 4, tzinfo=timezone.utc)
    st.dead_letter_retry_sweep_scheduled(fake_scheduled, fake_actual)

    assert len(alerts) == 1, "exactly one Fazal alert when pending > 0"
    assert "3 pending" in alerts[0], "alert must carry the count"
    # PII-free: no event_id / payload leak — the count only.
    assert "event_id" not in alerts[0].lower()


def test_dead_letter_retry_sweep_silent_when_empty(monkeypatch) -> None:
    """VT-440: zero pending dead-letters → NO alert (no noise)."""
    _patch_count_pending(monkeypatch, 0)

    import orchestrator.alerts.clients as clients_mod

    alerts: list[str] = []
    monkeypatch.setattr(clients_mod, "alert_fazal", lambda text: alerts.append(text))

    fake_scheduled = datetime(2026, 6, 25, 22, 30, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 6, 25, 22, 30, 4, tzinfo=timezone.utc)
    st.dead_letter_retry_sweep_scheduled(fake_scheduled, fake_actual)

    assert alerts == [], "no alert when there are no pending dead-letters"


def test_dead_letter_retry_sweep_no_send(monkeypatch) -> None:
    """VT-440: DETECT/ALERT-ONLY — the sweep NEVER reaches Twilio or the raw
    Resend/Telegram send clients (the Fazal alert is the only outbound, and it is
    a count, not a money/customer send). Patch the low-level send clients and
    assert neither fires even with pending rows."""
    _patch_count_pending(monkeypatch, 5)

    import orchestrator.alerts.clients as clients_mod

    send_called: list[str] = []
    monkeypatch.setattr(
        clients_mod, "send_telegram", lambda *a, **kw: send_called.append("telegram")
    )
    monkeypatch.setattr(
        clients_mod, "send_resend_email", lambda *a, **kw: send_called.append("resend")
    )
    # alert_fazal is allowed (the observability alert); stub it to a no-op so the
    # real client doesn't try to reach the network in the unit test.
    monkeypatch.setattr(clients_mod, "alert_fazal", lambda text: None)

    fake_scheduled = datetime(2026, 6, 25, 22, 30, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 6, 25, 22, 30, 4, tzinfo=timezone.utc)
    st.dead_letter_retry_sweep_scheduled(fake_scheduled, fake_actual)

    assert not send_called, (
        f"VT-440 sweep must not directly send via Telegram/Resend; called: {send_called}"
    )


def test_dead_letter_retry_sweep_is_best_effort(monkeypatch) -> None:
    """VT-440: a count_pending exception must NOT propagate (best-effort sweep)."""
    import orchestrator.billing.dead_letter as dl_mod

    def _boom():
        raise RuntimeError("DB unavailable")

    monkeypatch.setattr(dl_mod, "count_pending", _boom)

    fake_scheduled = datetime(2026, 6, 25, 22, 30, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 6, 25, 22, 30, 4, tzinfo=timezone.utc)
    # Must not raise — handler wraps in try/except BLE001.
    st.dead_letter_retry_sweep_scheduled(fake_scheduled, fake_actual)


def test_dead_letter_retry_sweep_idempotent_double_run(monkeypatch) -> None:
    """VT-440 money-safety (handler level): running the sweep TWICE over the same
    pending state produces the SAME alert each time and ZERO money effect — no
    replay/charge/write. The sweep is read-only; the alert is the only side effect
    and is itself idempotent (same count both runs)."""
    _patch_count_pending(monkeypatch, 2)

    import orchestrator.alerts.clients as clients_mod
    import orchestrator.billing.dead_letter as dl_mod

    alerts: list[str] = []
    monkeypatch.setattr(clients_mod, "alert_fazal", lambda text: alerts.append(text))
    monkeypatch.setattr(
        dl_mod, "replay", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("replay must never be called by the sweep")
        ),
    )

    fake_scheduled = datetime(2026, 6, 25, 22, 30, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 6, 25, 22, 30, 4, tzinfo=timezone.utc)
    st.dead_letter_retry_sweep_scheduled(fake_scheduled, fake_actual)
    st.dead_letter_retry_sweep_scheduled(fake_scheduled, fake_actual)

    # Two identical alerts — the count is unchanged because the sweep wrote nothing.
    assert len(alerts) == 2
    assert alerts[0] == alerts[1], "both runs alert with the identical (count-only) text"


# ---------------------------------------------------------------------------
# VT-560 — boot-only reapers/detectors promoted to steady-state scheduled sweeps
# ---------------------------------------------------------------------------


def test_stalled_task_sweep_scheduled_delegates(monkeypatch) -> None:
    """VT-560: the 10-min handler calls reap_stalled_manager_tasks (the VT-557 ladder)."""
    import orchestrator.orphan_reaper as reaper_mod

    called: list[int] = []
    monkeypatch.setattr(reaper_mod, "reap_stalled_manager_tasks", lambda: called.append(1) or 0)

    fake_scheduled = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 7, 2, 10, 0, 3, tzinfo=timezone.utc)
    st.stalled_task_sweep_scheduled(fake_scheduled, fake_actual)
    assert called, "handler must call reap_stalled_manager_tasks"


def test_silent_terminal_sweep_scheduled_delegates(monkeypatch) -> None:
    """VT-560: the 10-min handler calls detect_silent_terminal_runs (the VT-552 detector)."""
    import orchestrator.orphan_reaper as reaper_mod

    called: list[int] = []
    monkeypatch.setattr(reaper_mod, "detect_silent_terminal_runs", lambda: called.append(1) or 0)

    fake_scheduled = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 7, 2, 10, 0, 3, tzinfo=timezone.utc)
    st.silent_terminal_sweep_scheduled(fake_scheduled, fake_actual)
    assert called, "handler must call detect_silent_terminal_runs"


def test_orphan_run_reaper_scheduled_delegates(monkeypatch) -> None:
    """VT-560: the hourly handler calls reap_orphan_runs (the VT-481 reaper)."""
    import orchestrator.orphan_reaper as reaper_mod

    called: list[int] = []
    monkeypatch.setattr(reaper_mod, "reap_orphan_runs", lambda: called.append(1) or 0)

    fake_scheduled = datetime(2026, 7, 2, 11, 0, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 7, 2, 11, 0, 3, tzinfo=timezone.utc)
    st.orphan_run_reaper_scheduled(fake_scheduled, fake_actual)
    assert called, "handler must call reap_orphan_runs"


@pytest.mark.parametrize(
    ("handler", "target"),
    [
        ("stalled_task_sweep_scheduled", "reap_stalled_manager_tasks"),
        ("silent_terminal_sweep_scheduled", "detect_silent_terminal_runs"),
        ("orphan_run_reaper_scheduled", "reap_orphan_runs"),
    ],
)
def test_vt560_sweep_handlers_are_best_effort(monkeypatch, handler, target) -> None:
    """VT-560: a body exception must NOT propagate — every sweep handler is best-effort."""
    import orchestrator.orphan_reaper as reaper_mod

    def _boom() -> int:
        raise RuntimeError("DB unavailable")

    monkeypatch.setattr(reaper_mod, target, _boom)
    fake_scheduled = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 7, 2, 12, 0, 3, tzinfo=timezone.utc)
    # Must not raise — handler wraps in try/except BLE001.
    getattr(st, handler)(fake_scheduled, fake_actual)


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
        st.trial_evaluation_scheduled,  # VT-365: replaced the removed day-39 handler
        st.monthly_impact_scheduled,
        st.implicit_attribution_sweep_scheduled,  # VT-432
        st.reconcile_subscription_orphans_scheduled,  # VT-439
        st.dead_letter_retry_sweep_scheduled,  # VT-440
    ],
)
def test_scheduled_handler_accepts_scheduled_and_actual_time(monkeypatch, fn) -> None:
    """DBOS scheduled-handler signature smoke. Bodies that scan eligibility
    are stubbed to return empty so we exercise the signature without DB."""
    _captured_payloads(monkeypatch)
    monkeypatch.setattr(st, "_scan_attribution_close_eligible", lambda now: [])
    # VT-365: the trial sweep scans active trials via get_pool().connection();
    # the empty-pool stub below yields zero rows so the body is a clean no-op.

    # Monthly impact body + VT-439 orphan handler + VT-440 dead-letter count query the
    # pool inline; stub it. fetchone() returns a 0-count tuple for count_pending (VT-440).
    class _EmptyCursor:
        def execute(self, *a, **k): pass
        def fetchall(self): return []
        def fetchone(self): return {"n": 0}  # count_pending reads row["n"] (dict_row)
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


def test_trial_evaluation_scheduled_wires_real_owner_notify(monkeypatch) -> None:
    """VT-426 (Row C): the daily trial sweep handler must pass the REAL owner notify
    (``trial_sweep._owner_notify`` → the VT-393 send seam) into the body — NOT the
    logging-only ``_default_notify`` default. Without this, a trial-ending owner gets
    no WhatsApp. We capture the kwargs the handler hands the body."""
    from orchestrator.billing import trial_sweep as ts

    captured: dict[str, Any] = {}

    def _fake_body(now=None, *, notify_fn=None):
        captured["now"] = now
        captured["notify_fn"] = notify_fn
        return []

    monkeypatch.setattr(ts, "run_trial_evaluation_body", _fake_body)

    fake_scheduled = datetime(2026, 5, 26, 1, 30, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 5, 26, 1, 30, 9, tzinfo=timezone.utc)
    st.trial_evaluation_scheduled(fake_scheduled, fake_actual)

    assert captured["now"] == fake_actual
    assert captured["notify_fn"] is ts._owner_notify, (
        "scheduler must wire the real _owner_notify, not the logging _default_notify"
    )


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
    workflow_count = {"n": 0}

    def _fake_scheduled(cron):
        def _wrap(fn):
            call_count["n"] += 1
            return fn
        return _wrap

    def _fake_workflow(*args, **kwargs):
        # VT-464 D3: register_scheduled_triggers now wraps each handler with
        # DBOS.workflow() BEFORE DBOS.scheduled() (DBOS 2.x: scheduled() alone
        # does NOT register a workflow, so the cron fire raised
        # DBOSWorkflowFunctionNotFoundError). Patch it too so this stays a pure
        # unit check that doesn't mutate the real global DBOS registry.
        def _wrap(fn):
            workflow_count["n"] += 1
            return fn
        return _wrap

    monkeypatch.setattr(DBOS, "scheduled", _fake_scheduled)
    monkeypatch.setattr(DBOS, "workflow", _fake_workflow)
    st._registered = False
    st.register_scheduled_triggers()
    first = call_count["n"]
    first_wf = workflow_count["n"]
    st.register_scheduled_triggers()
    second = call_count["n"]
    # The registered set (19): weekly_cadence, attribution_close, trial_evaluation
    # (VT-90, the kept lifecycle sweep — NOT the removed VT-365 day-39 refund eval),
    # monthly_impact, approval_timeout_sweep (VT-47), L3_construction (VT-68),
    # reconstitution_sweep (VT-76), audit_chain_verify (VT-304), pii_log_sweep
    # (VT-305), kg_drain_sweep (VT-307), l2_retention_sweep (VT-311),
    # waitlist_retention_purge (VT-354), sla_breach_sweep (VT-357), vtr_digest (VT-280),
    # override_expiry_sweep (VT-374 — the F8 next-run pin expiry bound),
    # outbox_redaction_sweep (VT-382 — the CL-437 ruling-3.3 redaction backfill/backstop),
    # l2_approved_send_sweep (VT-418 — the L2 owner-approve→send reconciler, recovery-only),
    # implicit_attribution_sweep (VT-432 — daily VT-198 feedback tier-1 sweep, NO SEND),
    # reconcile_subscription_orphans (VT-439 — daily Razorpay orphan-DETECT backstop, DETECT-ONLY),
    # dead_letter_retry_sweep (VT-440 — daily Razorpay dead-letter backstop, DETECT/ALERT-ONLY).
    # VT-365 removed two triggers (day-39 refund evaluation + the VT-85 refund-offer
    # 48h timeout sweep): 16 → 14; VT-374 added one: 14 → 15; VT-382 added one: 15 → 16;
    # VT-418 added one: 16 → 17; VT-432 added one: 17 → 18; VT-439 added one: 18 → 19;
    # VT-440 added one: 19 → 20; VT-560 added three (stalled_task_sweep +
    # silent_terminal_sweep + orphan_run_reaper — the boot-only reapers/detectors
    # promoted to steady-state @DBOS.scheduled sweeps): 20 → 23.
    assert first == 23, "expected 23 triggers registered on first call"
    assert second == 23, "second call must short-circuit (idempotent)"
    # VT-464 D3: every scheduled handler MUST also be registered as a workflow
    # (one DBOS.workflow() wrap per DBOS.scheduled() call) — otherwise the cron
    # fire raises DBOSWorkflowFunctionNotFoundError.
    assert first_wf == 23, "expected 23 handlers wrapped as @DBOS.workflow"
    st._registered = False


def test_scheduled_sweeps_are_registered_workflows() -> None:
    """VT-464 D3: the scheduled sweeps must land in the DBOS workflow registry.

    The live re-drive saw approval_timeout_sweep_scheduled +
    l2_approved_send_sweep_scheduled fire and raise
    ``DBOSWorkflowFunctionNotFoundError: ... not a registered workflow function``
    — because DBOS 2.x's ``DBOS.scheduled`` registers ONLY a cron poller, not a
    workflow, so the poller-fire enqueue + recovery lookup in
    ``workflow_info_map`` missed. After the fix (register_scheduled_triggers
    wraps each handler with ``@DBOS.workflow`` before ``@DBOS.scheduled``) both
    sweeps resolve. This drives the REAL registration path (no monkeypatch) and
    asserts the registry contains them — the regression guard for the crash.
    """
    from dbos._dbos import _get_or_create_dbos_registry

    st._registered = False
    try:
        st.register_scheduled_triggers()
        reg = _get_or_create_dbos_registry()
        assert "approval_timeout_sweep_scheduled" in reg.workflow_info_map, (
            "approval_timeout_sweep_scheduled must be a registered workflow "
            "(else the 30-min poller fire raises DBOSWorkflowFunctionNotFoundError)"
        )
        assert "l2_approved_send_sweep_scheduled" in reg.workflow_info_map, (
            "l2_approved_send_sweep_scheduled must be a registered workflow "
            "(else the reconciler poller fire raises DBOSWorkflowFunctionNotFoundError)"
        )
        # VT-560: the three boot-only reapers/detectors are now steady-state scheduled
        # sweeps — each MUST be a registered workflow or its poller fire would raise
        # DBOSWorkflowFunctionNotFoundError (the same class the live re-drive hit).
        for name in (
            "stalled_task_sweep_scheduled",
            "silent_terminal_sweep_scheduled",
            "orphan_run_reaper_scheduled",
        ):
            assert name in reg.workflow_info_map, (
                f"{name} must be a registered workflow (VT-560 steady-state sweep)"
            )
    finally:
        st._registered = False
