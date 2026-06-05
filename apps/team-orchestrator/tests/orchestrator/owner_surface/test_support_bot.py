"""VT-88 — SupportBot escalation fallback (Phase 1).

Pure: phone last-4 masking. Logic (DB-gated): the no-silence ack on every unresolved
terminal + the deterministic 1st=ack-only / 2nd+=escalate counter + the PII-safe alert +
cross-tenant. Heavy imports guarded (VT-337 dep-less lesson).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.owner_surface import support_bot as sb  # noqa: E402


# ----------------------------- pure: last-4 mask ---------------------------------------
@pytest.mark.parametrize(
    ("raw", "out"),
    [("+919876543210", "3210"), ("9876543210", "3210"), (None, "?"), ("", "?"), ("12", "?")],
)
def test_last4(raw, out) -> None:
    assert sb._last4(raw) == out


def test_resolved_terminal_is_noop(monkeypatch) -> None:
    """A resolved terminal (completed/paused) → no ack, no escalate."""
    calls: dict[str, int] = {"ack": 0, "alert": 0}
    monkeypatch.setattr(
        sb, "_send_handoff_ack", lambda *a, **k: calls.__setitem__("ack", calls["ack"] + 1)
    )
    monkeypatch.setattr(
        sb, "_alert_fazal_safe", lambda *a, **k: calls.__setitem__("alert", calls["alert"] + 1)
    )
    ev = SimpleNamespace(sender_phone="+910000000000")
    out = sb.maybe_escalate_support(
        tenant_id=str(uuid4()), run_id="r", event=ev, final_status="completed"
    )
    assert out["action"] == "none"
    assert calls == {"ack": 0, "alert": 0}


def test_alert_is_pii_safe(monkeypatch) -> None:
    """The Fazal alert carries last-4 + run_id only — never the raw phone."""
    captured: list[str] = []
    monkeypatch.setattr(
        "orchestrator.billing.refund_executor._alert_fazal", lambda text: captured.append(text)
    )
    sb._alert_fazal_safe(uuid4(), "+919876543210", "run-123")
    text = captured[0]
    assert "3210" in text and "run-123" in text
    assert "9876543210" not in text and "+919876543210" not in text  # raw phone NEVER in the alert


# ----------------------------- DB: the counter + escalate boundary ---------------------


def _seed_runs(pool, tid, *, n: int, status: str = "escalated") -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, 't', 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tid),),
        )
        for _ in range(n):
            conn.execute(
                "INSERT INTO pipeline_runs (tenant_id, run_type, status) VALUES (%s, 'webhook', %s)",
                (str(tid), status),
            )


def _patch_sends(monkeypatch) -> dict[str, int]:
    calls = {"ack": 0, "record": 0, "alert": 0}
    monkeypatch.setattr(
        sb, "_send_handoff_ack", lambda *a, **k: calls.__setitem__("ack", calls["ack"] + 1)
    )
    monkeypatch.setattr(
        sb, "_alert_fazal_safe", lambda *a, **k: calls.__setitem__("alert", calls["alert"] + 1)
    )
    monkeypatch.setattr(
        "orchestrator.escalations.record_escalation",
        lambda *a, **k: calls.__setitem__("record", calls["record"] + 1),
    )
    return calls


@pytest.mark.integration
def test_first_unresolved_is_ack_only(monkeypatch, _dbpool) -> None:
    calls = _patch_sends(monkeypatch)
    tid = uuid4()
    _seed_runs(_dbpool, tid, n=1)  # this run = the 1st unresolved in 24h
    ev = SimpleNamespace(sender_phone="+919811111111")
    out = sb.maybe_escalate_support(
        tenant_id=str(tid), run_id="r1", event=ev, final_status="escalated"
    )
    assert out["action"] == "ack_only" and out["unresolved_24h"] == 1
    assert calls["ack"] == 1 and calls["record"] == 0 and calls["alert"] == 0  # ack, no escalate


@pytest.mark.integration
def test_second_unresolved_escalates(monkeypatch, _dbpool) -> None:
    calls = _patch_sends(monkeypatch)
    tid = uuid4()
    _seed_runs(_dbpool, tid, n=2)  # 2 unresolved in 24h → this is the 2nd+
    ev = SimpleNamespace(sender_phone="+919811111111")
    out = sb.maybe_escalate_support(
        tenant_id=str(tid), run_id="r2", event=ev, final_status="aborted_hard_limit"
    )
    assert out["action"] == "escalated" and out["unresolved_24h"] == 2
    assert calls["ack"] == 1 and calls["record"] == 1 and calls["alert"] == 1  # ack + escalate


@pytest.mark.integration
def test_counter_cross_tenant(monkeypatch, _dbpool) -> None:
    _patch_sends(monkeypatch)
    a, b = uuid4(), uuid4()
    _seed_runs(_dbpool, a, n=3)  # tenant A: 3 unresolved
    _seed_runs(_dbpool, b, n=1)  # tenant B: 1 (its own count, A's runs don't leak in)
    assert sb._unresolved_count_24h(a) == 3
    assert sb._unresolved_count_24h(b) == 1
