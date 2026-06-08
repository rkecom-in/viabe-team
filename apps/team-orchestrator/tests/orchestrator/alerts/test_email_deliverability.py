"""VT-113 — email sender registry + deliverability monitor. Pure-Python (no DB).

Dep-less smoke collects ALL tests with only pytest+pyyaml; importing the orchestrator package pulls
pydantic etc. → importorskip gates this module (skipped in smoke, runs full suite); orchestrator
imports are deferred into the tests. The Resend request shape is pinned via an injected transport
(the #420 "canary present but never green" lesson); the live call is a gated post-egress canary.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("pydantic")

# Fixed reference time so the rolling-24h window is deterministic in tests.
_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def _ca(hours_ago: float) -> str:
    """A Resend-style created_at string (space sep, +00 offset) N hours before _NOW."""
    return (_NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S.%f+00")


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
        return {"has_more": False, "data": [  # real shape: newest-first, created_at present
            {"id": "1", "created_at": _ca(1), "last_event": "delivered"},
            {"id": "2", "created_at": _ca(2), "last_event": "delivered"},
            {"id": "3", "created_at": _ca(3), "last_event": "bounced"},
            {"id": "4", "created_at": _ca(4), "last_event": "complained"},
        ]}

    stats = ed.fetch_resend_stats(get_fn=get_fn, now=_NOW)
    assert captured["path"].startswith("/emails?limit=100")  # paginated list call
    assert stats.ok and stats.sent == 4 and stats.bounced == 1 and stats.complained == 1
    assert stats.bounce_rate == 0.25 and stats.complaint_rate == 0.25
    assert stats.breached() and stats.capped is False  # 25% bounce > 5%


def test_window_excludes_rows_older_than_24h(monkeypatch):
    from orchestrator.alerts import email_deliverability as ed

    monkeypatch.setenv("RESEND_API_KEY", "re_test")

    def get_fn(path, api_key):  # newest-first; rows 3-4 are outside the 24h window
        return {"has_more": False, "data": [
            {"id": "1", "created_at": _ca(2), "last_event": "bounced"},      # in
            {"id": "2", "created_at": _ca(23), "last_event": "delivered"},   # in (edge)
            {"id": "3", "created_at": _ca(30), "last_event": "bounced"},     # OLD → excluded
            {"id": "4", "created_at": _ca(50), "last_event": "complained"},  # OLD → excluded
        ]}

    stats = ed.fetch_resend_stats(get_fn=get_fn, now=_NOW)
    assert stats.sent == 2 and stats.bounced == 1 and stats.complained == 0  # only in-window counted


def test_pagination_follows_after_cursor_and_short_circuits(monkeypatch):
    from orchestrator.alerts import email_deliverability as ed

    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    calls = []

    def get_fn(path, api_key):
        calls.append(path)
        if "after=" not in path:  # page 1 — both in window, has_more
            return {"has_more": True, "data": [
                {"id": "p1a", "created_at": _ca(1), "last_event": "bounced"},
                {"id": "p1b", "created_at": _ca(10), "last_event": "delivered"},
            ]}
        return {"has_more": True, "data": [  # page 2 — last row is old → short-circuit, no page 3
            {"id": "p2a", "created_at": _ca(20), "last_event": "complained"},
            {"id": "p2b", "created_at": _ca(40), "last_event": "bounced"},
        ]}

    stats = ed.fetch_resend_stats(get_fn=get_fn, now=_NOW)
    assert len(calls) == 2 and "after=p1b" in calls[1]  # followed the cursor, stopped despite has_more
    assert stats.sent == 3 and stats.bounced == 1 and stats.complained == 1  # p2b (old) excluded


def test_page_cap_sets_capped_and_stops(monkeypatch):
    from orchestrator.alerts import email_deliverability as ed

    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    calls = []

    def get_fn(path, api_key):  # every page in-window + has_more → would spin without the cap
        calls.append(path)
        return {"has_more": True, "data": [
            {"id": f"id{len(calls)}", "created_at": _ca(1), "last_event": "delivered"},
        ]}

    stats = ed.fetch_resend_stats(get_fn=get_fn, now=_NOW)
    assert stats.capped is True and len(calls) == ed._MAX_PAGES  # stopped at the cap, did not spin


def test_pii_fields_never_reach_the_result(monkeypatch):
    from orchestrator.alerts import email_deliverability as ed

    monkeypatch.setenv("RESEND_API_KEY", "re_test")

    def get_fn(path, api_key):  # full-access key returns recipient PII
        return {"has_more": False, "data": [
            {"id": "1", "created_at": _ca(1), "last_event": "bounced",
             "to": ["victim@example.com"], "from": "ops@viabe.ai", "subject": "Secret Subject"},
        ]}

    stats = ed.fetch_resend_stats(get_fn=get_fn, now=_NOW)
    blob = repr(stats)
    assert "victim@example.com" not in blob and "Secret Subject" not in blob  # counts only
    assert stats.sent == 1 and stats.bounced == 1


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
