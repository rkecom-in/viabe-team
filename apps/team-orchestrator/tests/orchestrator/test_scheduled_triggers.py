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


# ---------------------------------------------------------------------------
# 3. Shell-event emission (Cond 2 — phantom-Done prevention)
# ---------------------------------------------------------------------------

def test_attribution_close_emits_shell_event_not_closed(monkeypatch) -> None:
    captured = _captured_payloads(monkeypatch)
    now = datetime(2026, 5, 26, 2, 0, tzinfo=timezone.utc)
    st.run_attribution_close_body(now=now)
    time.sleep(0.05)
    assert captured, "log_event never reached"
    event_type, _, _, severity, component, payload, _ = captured[0]
    assert event_type == "attribution_close_shell"
    assert event_type != "attribution_closed"  # reserved name MUST not fire
    assert severity == "info"
    assert component == "scheduled_trigger"
    assert payload["status"] == "skipped_schema_pending"
    assert payload["trigger_reason"] == "attribution_close"


def test_day39_emits_shell_event_not_evaluated(monkeypatch) -> None:
    captured = _captured_payloads(monkeypatch)
    st.run_day39_evaluation_body(
        now=datetime(2026, 5, 26, 6, 0, tzinfo=timezone.utc)
    )
    time.sleep(0.05)
    assert captured
    event_type, _, _, _, _, payload, _ = captured[0]
    assert event_type == "day39_shell"
    assert event_type != "day39_evaluated"  # reserved
    assert event_type != "day39_continue"  # reserved
    assert event_type != "day39_refund_triggered"  # reserved
    assert payload["status"] == "skipped_schema_pending"


def test_monthly_impact_emits_shell_event_not_started(monkeypatch) -> None:
    captured = _captured_payloads(monkeypatch)
    st.run_monthly_impact_body(
        now=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    )
    time.sleep(0.05)
    assert captured
    event_type, _, _, _, _, payload, _ = captured[0]
    assert event_type == "monthly_impact_shell"
    assert event_type != "monthly_impact_started"  # reserved
    assert payload["status"] == "skipped_schema_pending"


# ---------------------------------------------------------------------------
# 4. Weekly cadence — emits real event (full implementation, NOT a shell)
# ---------------------------------------------------------------------------

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
    _captured_payloads(monkeypatch)
    fake_scheduled = datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc)
    fake_actual = datetime(2026, 5, 26, 9, 0, 12, tzinfo=timezone.utc)
    # Should not raise.
    fn(fake_scheduled, fake_actual)


# ---------------------------------------------------------------------------
# 7. register_scheduled_triggers idempotency
# ---------------------------------------------------------------------------

def test_register_scheduled_triggers_idempotent(monkeypatch) -> None:
    """Two calls should not raise; second call is a no-op short-circuit."""
    from dbos import DBOS
    call_count = {"n": 0}

    def _fake_scheduled(cron):
        def _wrap(fn):
            call_count["n"] += 1
            return fn
        return _wrap

    monkeypatch.setattr(DBOS, "scheduled", _fake_scheduled)
    # Reset module-level guard.
    st._registered = False
    st.register_scheduled_triggers()
    first = call_count["n"]
    st.register_scheduled_triggers()
    second = call_count["n"]
    assert first == 4, "expected 4 triggers registered on first call"
    assert second == 4, "second call must short-circuit (idempotent)"
    # Cleanup so other tests aren't tainted by the module guard staying True.
    st._registered = False
