"""VT-113 — email sender registry + deliverability monitor. Pure-Python (no DB).

Dep-less smoke collects ALL tests with only pytest+pyyaml; importing the orchestrator package pulls
pydantic etc. → importorskip gates this module (skipped in smoke, runs full suite); orchestrator
imports are deferred into the tests. The Resend request shape is pinned via an injected transport
(the #420 "canary present but never green" lesson); the live call is a gated post-egress canary.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")


# --- sender registry (grep-zero From source) ---------------------------------
def test_sender_from_roles(monkeypatch):
    from orchestrator.alerts.email_senders import reply_to, sender_from

    monkeypatch.delenv("RESEND_FROM_EMAIL", raising=False)
    assert sender_from("transactional") == "noreply@viabe.ai"
    assert sender_from("support") == "support@viabe.ai"
    assert sender_from("alerts") == "ops@viabe.ai"
    assert reply_to("transactional") == "support@viabe.ai"


def test_resend_from_email_overrides_alerts(monkeypatch):
    from orchestrator.alerts.email_senders import sender_from

    monkeypatch.setenv("RESEND_FROM_EMAIL", "ops-override@viabe.ai")
    assert sender_from("alerts") == "ops-override@viabe.ai"  # back-compat override
    assert sender_from("transactional") == "noreply@viabe.ai"  # other roles unaffected


# --- deliverability stats (shape-pinned, fail-soft) --------------------------
def test_stats_compute_rates_from_injected_rows(monkeypatch):
    from orchestrator.alerts import email_deliverability as ed

    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    captured = {}

    def get_fn(path, api_key):
        captured["path"] = path
        captured["key"] = api_key
        return {"data": [
            {"last_event": "delivered"}, {"last_event": "delivered"},
            {"last_event": "bounced"}, {"last_event": "complained"},
        ]}

    stats = ed.fetch_resend_stats(get_fn=get_fn)
    assert captured["path"] == "/emails"  # pinned endpoint shape
    assert stats.ok and stats.sent == 4 and stats.bounced == 1 and stats.complained == 1
    assert stats.bounce_rate == 0.25 and stats.complaint_rate == 0.25
    assert stats.breached()  # 25% bounce > 5%


def test_no_api_key_fails_soft(monkeypatch):
    from orchestrator.alerts import email_deliverability as ed

    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    assert ed.fetch_resend_stats(get_fn=lambda p, k: {}).ok is False


def test_check_alerts_on_breach(monkeypatch):
    from orchestrator.alerts import email_deliverability as ed

    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    alerts = []
    monkeypatch.setattr("orchestrator.billing.refund_executor._alert_fazal", lambda t: alerts.append(t))
    monkeypatch.setattr(ed, "fetch_resend_stats", lambda **k: ed.DeliverabilityStats(ok=True, sent=100, bounced=8, complained=0))
    out = ed.run_deliverability_check_body()
    assert out["alerted"] is True and len(alerts) == 1 and "deliverability ALERT" in alerts[0]


def test_check_no_alert_when_healthy(monkeypatch):
    from orchestrator.alerts import email_deliverability as ed

    alerts = []
    monkeypatch.setattr("orchestrator.billing.refund_executor._alert_fazal", lambda t: alerts.append(t))
    monkeypatch.setattr(ed, "fetch_resend_stats", lambda **k: ed.DeliverabilityStats(ok=True, sent=1000, bounced=10, complained=0))  # 1% bounce
    out = ed.run_deliverability_check_body()
    assert out["alerted"] is False and not alerts


def test_check_fail_soft_when_vendor_down(monkeypatch):
    from orchestrator.alerts import email_deliverability as ed

    alerts = []
    monkeypatch.setattr("orchestrator.billing.refund_executor._alert_fazal", lambda t: alerts.append(t))
    monkeypatch.setattr(ed, "fetch_resend_stats", lambda **k: ed.DeliverabilityStats(ok=False))
    out = ed.run_deliverability_check_body()
    assert out["ok"] is False and out["alerted"] is False and not alerts  # vendor down → skip, no crash


# --- gated live canary (Rule #15; fail-not-skip once egress + creds available) ---
@pytest.mark.skipif(__import__("os").environ.get("RESEND_LIVE_CANARY") != "1", reason="RESEND_LIVE_CANARY!=1 — gated post-egress")
def test_real_resend_stats_call():
    from orchestrator.alerts import email_deliverability as ed

    stats = ed.fetch_resend_stats()
    assert stats.ok, "real Resend stats call must succeed once egress + RESEND_API_KEY are available"
