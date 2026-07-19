"""VT-335 (VT-84 PR-2) — adhoc_campaign (via approval gate) + template_error.

Pure: the router's adhoc marker (NEVER a direct send — Cowork Q4 hard invariant) +
template_error routing. DB (gated on DATABASE_URL): the report insert + the DSR
HARD-DELETE keystone + cross-tenant. Heavy imports local (dep-less smoke safe).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

# Dep-less CI 'test' job: some tests import owner_inputs.* (-> anthropic) or routing.
# Skip the module cleanly when anthropic is absent; the full real-PG suite runs it.
pytest.importorskip("anthropic")


# ----------------------------- pure: adhoc marker + invariant --------------------------
def test_adhoc_returns_owner_initiated_marker_never_sends(monkeypatch) -> None:
    """Cowork Q4: a SEND must NEVER fire off a Haiku intent. adhoc -> the 'owner_initiated'
    trigger marker (fall-through to the agent + approval gate), NOT a DispatchResult, NOT a
    send."""
    import orchestrator.edge_cases_router as r

    sent: list[str] = []
    monkeypatch.setattr(r, "_send_edge_ack", lambda tid, phone, text: sent.append(text))
    ev = SimpleNamespace(body="send a campaign now", sender_phone="+910000000000")
    out = r.route_edge_case(
        tenant_id="t",
        event=ev,
        classify_fn=lambda b: SimpleNamespace(classification="adhoc_campaign_request"),
    )
    assert out == "owner_initiated"  # a str marker, not a DispatchResult
    assert sent == []  # NO send happened in the router (the approval gate sends, post-confirm)


def test_owner_initiated_cannot_bypass_approval_gate() -> None:
    """Hard invariant: trigger_reason='owner_initiated' does NOT reach campaign_execute
    without owner_decision='approved' — route_after_approval keys on owner_decision ONLY."""
    from orchestrator.routing import route_after_approval

    assert (
        route_after_approval({"trigger_reason": "owner_initiated", "owner_decision": None}) == "end"
    )
    assert route_after_approval({"trigger_reason": "owner_initiated"}) == "end"
    # only an explicit approval reaches execute — regardless of trigger_reason
    assert (
        route_after_approval({"trigger_reason": "owner_initiated", "owner_decision": "approved"})
        == "campaign_execute"
    )
    assert (
        route_after_approval({"trigger_reason": "weekly_cadence", "owner_decision": "rejected"})
        == "end"
    )


def test_template_error_routes_to_dispatchresult(monkeypatch) -> None:
    import orchestrator.edge_cases_router as r

    monkeypatch.setattr(r, "_send_edge_ack", lambda tid, phone, text: None)
    monkeypatch.setattr(
        "orchestrator.owner_inputs.template_error.handle_template_error",
        lambda tid, body: SimpleNamespace(
            report_id=uuid4(), recent_template_id="X", response_text="ok"
        ),
    )
    ev = SimpleNamespace(body="the message you sent was wrong", sender_phone="+910000000000")
    out = r.route_edge_case(
        tenant_id="t",
        event=ev,
        classify_fn=lambda b: SimpleNamespace(classification="template_error_followup"),
    )
    assert out is not None and out.reason == "edge_case:template_error"


def test_template_error_with_open_campaign_approval_routes_to_revision(monkeypatch) -> None:
    """VT-667 fix-4: in enforce mode, a 'this message is wrong' report while an OPEN campaign_send
    approval is pending is a CORRECTION of the pending draft, not a delivered-template failure — it
    routes to the campaign REVISION (edge_case:campaign_revision), NEVER the Fazal-review deflection
    (handle_template_error must not fire), and the honest 'reworking it' ack is sent to the owner."""
    import orchestrator.edge_cases_router as r

    sent: dict[str, object] = {}
    monkeypatch.setattr(r, "_send_edge_ack", lambda tid, phone, text: sent.update(text=text))
    monkeypatch.setattr("orchestrator.manager.loop_mode.is_enforce", lambda *a, **k: True)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "orchestrator.manager.triage_seam.revise_pending_campaign",
        lambda tid, body, sid: seen.update(body=body, sid=sid) or "REWORKING IT",
    )
    monkeypatch.setattr(
        "orchestrator.owner_inputs.template_error.handle_template_error",
        lambda tid, body: (_ for _ in ()).throw(AssertionError("must NOT deflect to Fazal-review")),
    )

    ev = SimpleNamespace(
        body="this isn't the Diwali offer I asked for — redo it with the festive vibe",
        sender_phone="+910000000000",
        twilio_message_sid="SMcorr",
    )
    out = r.route_edge_case(
        tenant_id="t",
        event=ev,
        classify_fn=lambda b: SimpleNamespace(classification="template_error_followup"),
    )
    assert out is not None and out.reason == "edge_case:campaign_revision"
    assert sent.get("text") == "REWORKING IT"
    assert seen.get("sid") == "SMcorr" and "festive vibe" in str(seen.get("body"))


def test_template_error_without_open_campaign_keeps_deflection_path(monkeypatch) -> None:
    """No pending campaign_send approval (revise_pending_campaign returns None) → the genuine
    template-error path is byte-unchanged: handle_template_error fires, reason=edge_case:template_error."""
    import orchestrator.edge_cases_router as r

    monkeypatch.setattr(r, "_send_edge_ack", lambda tid, phone, text: None)
    monkeypatch.setattr("orchestrator.manager.loop_mode.is_enforce", lambda *a, **k: True)
    monkeypatch.setattr(
        "orchestrator.manager.triage_seam.revise_pending_campaign", lambda tid, body, sid: None
    )
    fired = {"n": 0}
    monkeypatch.setattr(
        "orchestrator.owner_inputs.template_error.handle_template_error",
        lambda tid, body: fired.__setitem__("n", 1)
        or SimpleNamespace(report_id=uuid4(), recent_template_id="X", response_text="ok"),
    )
    ev = SimpleNamespace(
        body="the message you sent rendered wrong", sender_phone="+910000000000",
        twilio_message_sid="SMte",
    )
    out = r.route_edge_case(
        tenant_id="t", event=ev,
        classify_fn=lambda b: SimpleNamespace(classification="template_error_followup"),
    )
    assert fired["n"] == 1
    assert out is not None and out.reason == "edge_case:template_error"


def test_purge_order_includes_template_error_after_founding() -> None:
    from orchestrator import dsr_purge

    order = list(dsr_purge._PURGE_ORDER)
    assert "template_error_reports" in order
    # hard-deleted AFTER founding_tier_claims (Cowork Q2 position)
    assert order.index("template_error_reports") > order.index("founding_tier_claims")


# ----------------------------- DB integration ------------------------------------------


def _tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (tid, f"vt335-{tid}"),
        )
    return tid


@pytest.mark.integration
def test_template_error_inserts_report(monkeypatch, _dbpool) -> None:
    import orchestrator.owner_inputs.template_error as te

    monkeypatch.setattr(te, "_alert_fazal_safe", lambda *a, **k: None)  # no Telegram in test
    tid = _tenant(_dbpool)
    res = te.handle_template_error(tid, "the offer message was in the wrong language")
    assert res.report_id is not None
    with _dbpool.connection() as conn:
        row = conn.execute(
            "SELECT owner_complaint, status FROM template_error_reports WHERE id=%s",
            (str(res.report_id),),
        ).fetchone()
    assert row["status"] == "open" and "wrong language" in row["owner_complaint"]


@pytest.mark.integration
def test_template_error_dsr_hard_delete(monkeypatch, _dbpool) -> None:
    """The keystone: a DSR purge HARD-DELETES the report (no anonymize-retain)."""
    import orchestrator.owner_inputs.template_error as te
    from orchestrator.dsr_purge import _delete_where_tenant

    monkeypatch.setattr(te, "_alert_fazal_safe", lambda *a, **k: None)
    tid = _tenant(_dbpool)
    res = te.handle_template_error(tid, "complaint text for DSR")
    assert res.report_id is not None
    from uuid import UUID

    with _dbpool.connection() as conn:
        deleted = _delete_where_tenant(conn, "template_error_reports", UUID(tid))
        assert deleted >= 1
        gone = conn.execute(
            "SELECT count(*) AS n FROM template_error_reports WHERE tenant_id=%s", (tid,)
        ).fetchone()["n"]
    assert gone == 0  # hard-deleted, not anonymized-retained


@pytest.mark.integration
def test_template_error_cross_tenant(monkeypatch, _dbpool) -> None:
    import orchestrator.owner_inputs.template_error as te
    from uuid import UUID

    from orchestrator.dsr_purge import _delete_where_tenant

    monkeypatch.setattr(te, "_alert_fazal_safe", lambda *a, **k: None)
    a, b = _tenant(_dbpool), _tenant(_dbpool)
    te.handle_template_error(a, "tenant A complaint")
    rb = te.handle_template_error(b, "tenant B complaint")
    with _dbpool.connection() as conn:
        _delete_where_tenant(conn, "template_error_reports", UUID(a))  # purge A only
        b_rows = conn.execute(
            "SELECT count(*) AS n FROM template_error_reports WHERE tenant_id=%s", (b,)
        ).fetchone()["n"]
    assert rb.report_id is not None and b_rows == 1  # B untouched
