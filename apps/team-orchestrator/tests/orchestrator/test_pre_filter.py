"""VT-3.8 tests — Pre-Filter Gate (Stage 1) + 5 direct handlers.

Refactored in VT-3.2: pre_filter / handlers take a SubscriberState (not the
removed Tenant stub). Require a live Postgres via ``DATABASE_URL`` plus the
dbos / langgraph stack; run in the CI ``orchestrator`` job.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("langgraph")

import psycopg  # noqa: E402 — imported after the dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — pre-filter tests skipped",
)


@pytest.fixture(scope="module")
def gate():
    """Apply migrations, launch DBOS, expose the gate + handler registry."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos
    from orchestrator import types
    from orchestrator.direct_handlers import HANDLERS
    from orchestrator.pre_filter_gate import pre_filter
    from orchestrator.state import new_subscriber_state

    launch_dbos()
    try:
        yield SimpleNamespace(
            dsn=dsn,
            pre_filter=pre_filter,
            HANDLERS=HANDLERS,
            t=types,
            make_state=new_subscriber_state,
        )
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at) "
            "VALUES ('VT-3.8 Test', 'founding', 'trial', now()) RETURNING id"
        ).fetchone()
    assert row is not None
    return str(row[0])


def _state(gate, tenant_id: str | UUID):
    """A minimal SubscriberState for routing — only tenant_id matters here."""
    return gate.make_state(UUID(str(tenant_id)))


def _inbound(gate, body: str):
    return gate.t.WebhookEvent(body=body, sender_phone="+910000000000")


def _callback(gate, state: str):
    return gate.t.WebhookEvent(
        message_type="status_callback",
        status_callback_state=state,
        twilio_message_sid="SM-test",
    )


# --- Routing rules -----------------------------------------------------------


def test_opt_out_keyword_en_routes_and_sets_flag(gate):
    tenant_id = _new_tenant(gate.dsn)
    sub = _state(gate, tenant_id)
    result = gate.pre_filter(_inbound(gate, "STOP"), sub)

    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "opt_out_handler"

    outcome = gate.HANDLERS["opt_out_handler"](_inbound(gate, "STOP"), sub)
    assert outcome["opt_out_set"] is True
    assert outcome["send_result"]["success"] is True  # VT-3.3c: honest send

    with psycopg.connect(gate.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT opt_out FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    assert row == (True,)


def test_opt_out_keyword_hi_routes_and_sets_flag(gate):
    tenant_id = _new_tenant(gate.dsn)
    sub = _state(gate, tenant_id)
    result = gate.pre_filter(_inbound(gate, "बंद करो"), sub)

    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "opt_out_handler"

    gate.HANDLERS["opt_out_handler"](_inbound(gate, "बंद करो"), sub)
    with psycopg.connect(gate.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT opt_out FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    assert row == (True,)


def test_cd6_global_stop_routes_to_opt_out_handler(gate):
    """R5 / CD6 — a Hinglish GLOBAL send-stop the keyword list misses ("bas ab message mat bhejo")
    routes to the authoritative opt_out_handler (Rule a leg). A PER-CUSTOMER stop must NOT — it falls
    through to the brain (the edge-router exclusion path), never a tenant opt-out (Fazal CD6)."""
    tenant_id = _new_tenant(gate.dsn)
    sub = _state(gate, tenant_id)

    result = gate.pre_filter(_inbound(gate, "bas ab message mat bhejo"), sub)
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "opt_out_handler"

    # per-customer stop ("Rajesh ko …") + a bare send-negation reply stay OUT of the opt-out leg
    for body in ("Rajesh ko message mat bhejo", "us customer ko mat bhejo", "mat bhejo"):
        r = gate.pre_filter(_inbound(gate, body), sub)
        assert isinstance(r, gate.t.RouteToBrain), body


# --- VT-303 data-inputs ENABLE (opt-in) + brain consent gate -----------------


def test_enable_keyword_routes_to_enable_handler(gate):
    sub = _state(gate, _new_tenant(gate.dsn))
    result = gate.pre_filter(_inbound(gate, "ACTIVATE TEAM"), sub)
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "data_inputs_enable_handler"


def test_enable_keyword_is_case_and_space_insensitive(gate):
    sub = _state(gate, _new_tenant(gate.dsn))
    result = gate.pre_filter(_inbound(gate, "  enable data inputs  "), sub)
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "data_inputs_enable_handler"


def test_data_inputs_enable_handler_sets_owner_inputs_true(gate, monkeypatch):
    import importlib

    # The submodule and its function share a name; __init__ binds the function
    # over the submodule attribute, so import_module is needed to get the module.
    h = importlib.import_module(
        "orchestrator.direct_handlers.data_inputs_enable_handler"
    )
    sent: dict[str, object] = {}

    def _capture(body: str, phone: str, **kwargs: object) -> str:
        # VT-611 Package H0: the handler now passes tenant_id + surface='system' so this confirm
        # lands in the lifetime conversation_log (was bare -> no-op'd).
        sent["body"], sent["phone"] = body, phone
        sent["tenant_id"], sent["surface"] = kwargs.get("tenant_id"), kwargs.get("surface")
        return "SMfake"

    monkeypatch.setattr(h, "send_freeform_message", _capture)
    tenant_id = _new_tenant(gate.dsn)
    sub = _state(gate, tenant_id)
    outcome = h.data_inputs_enable_handler(_inbound(gate, "ACTIVATE TEAM"), sub)
    assert outcome["owner_inputs_set"] is True
    with psycopg.connect(gate.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT owner_inputs FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    assert row == (True,)
    assert sent["tenant_id"] == UUID(tenant_id)
    assert sent["surface"] == "system"


def test_consent_required_handler_sends_prompt_no_transmit(gate, monkeypatch):
    import importlib

    h = importlib.import_module(
        "orchestrator.direct_handlers.consent_required_handler"
    )
    sent: dict[str, str] = {}

    def _capture(body: str, phone: str, **kwargs: object) -> str:
        # VT-583: the handler now passes tenant_id + surface='system' so the ask is recorded as the
        # lifetime-log consent marker (the runner gate keys the follow-up affirm off it).
        sent["body"], sent["phone"] = body, phone
        sent["surface"] = str(kwargs.get("surface"))
        return "SMfake"

    monkeypatch.setattr(h, "send_freeform_message", _capture)
    sub = _state(gate, _new_tenant(gate.dsn))
    outcome = h.consent_required_handler(_inbound(gate, "plan a campaign"), sub)
    assert outcome["handler"] == "consent_required_handler"
    assert outcome["consent_prompt_sent"] is True
    # The prompt tells the owner exactly which phrase enables data inputs.
    assert "ACTIVATE TEAM" in sent["body"]
    assert sent["surface"] == "system"  # recorded as the consent-ask marker


def test_consent_required_handler_acknowledges_decline(gate, monkeypatch):
    # cluster-5 (sr_consent_decline_then_explicit): an explicit decline ("no thanks, not right now")
    # is ACKNOWLEDGED, not re-pushed verbatim — but the full prompt (incl. ACTIVATE TEAM) still ships
    # so the exact-keyword floor still works on the next turn.
    import importlib

    h = importlib.import_module("orchestrator.direct_handlers.consent_required_handler")
    sent: dict[str, str] = {}

    def _capture(body: str, phone: str, **kwargs: object) -> str:
        sent["body"] = body
        return "SMfake"

    monkeypatch.setattr(h, "send_freeform_message", _capture)
    sub = _state(gate, _new_tenant(gate.dsn))
    outcome = h.consent_required_handler(_inbound(gate, "no thanks, not right now"), sub)
    assert outcome["consent_prompt_sent"] is True
    assert sent["body"].startswith(h._DECLINE_ACK)  # decline acknowledged, not verbatim repeat
    assert "ACTIVATE TEAM" in sent["body"]  # exact-keyword floor still available


def test_brain_owner_inputs_ok_fail_closed_on_error(gate, monkeypatch):
    """VT-303 / CL-425: a consent-check error fails CLOSED (no transmit)."""
    import orchestrator.runner as runner_mod

    def _boom(_tenant):  # noqa: ANN001, ANN202
        raise RuntimeError("db down")

    monkeypatch.setattr(runner_mod, "_owner_inputs_enabled", _boom)
    assert runner_mod._brain_owner_inputs_ok(str(uuid4())) is False


def test_brain_owner_inputs_ok_true_after_enable(gate, monkeypatch):
    import importlib

    import orchestrator.runner as runner_mod

    h = importlib.import_module(
        "orchestrator.direct_handlers.data_inputs_enable_handler"
    )
    monkeypatch.setattr(h, "send_freeform_message", lambda body, phone, **kw: "SMfake")
    tenant_id = _new_tenant(gate.dsn)
    # Default owner_inputs is FALSE -> gate would divert to consent_required.
    assert runner_mod._brain_owner_inputs_ok(tenant_id) is False
    # Owner enables -> gate now permits the brain transmit.
    h.data_inputs_enable_handler(_inbound(gate, "ACTIVATE TEAM"), _state(gate, tenant_id))
    assert runner_mod._brain_owner_inputs_ok(tenant_id) is True


def test_dsr_keyword_routes_creates_ticket(gate):
    tenant_id = _new_tenant(gate.dsn)
    sub = _state(gate, tenant_id)
    event = _inbound(gate, "I want data deletion for my account")

    result = gate.pre_filter(event, sub)
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "dsr_handler"

    outcome = gate.HANDLERS["dsr_handler"](event, sub)
    assert outcome["send_result"]["success"] is True  # VT-3.3c: honest send
    assert outcome["dsr_ticket_id"] is not None

    with psycopg.connect(gate.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, request_type FROM dsr_tickets WHERE id = %s",
            (outcome["dsr_ticket_id"],),
        ).fetchone()
    assert row == ("acknowledged", "deletion")


def test_devanagari_dsr_routes_creates_ticket(gate):
    """VT-329 end-to-end: a Devanagari DSR message routes to dsr_handler (it was silently dropped
    before — the launch-gate failure mode)."""
    tenant_id = _new_tenant(gate.dsn)
    sub = _state(gate, tenant_id)
    event = _inbound(gate, "मेरा डेटा हटाओ please")

    result = gate.pre_filter(event, sub)
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "dsr_handler"


def test_mixed_script_dsr_routes_creates_ticket(gate):
    """VT-329 (Cowork): a code-switched DSR message ('मेरा data delete karo') routes to dsr_handler
    — the curated code-switched keywords + boundary-safe containment close the BLOCK miss."""
    tenant_id = _new_tenant(gate.dsn)
    sub = _state(gate, tenant_id)
    event = _inbound(gate, "मेरा data delete karo")

    result = gate.pre_filter(event, sub)
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "dsr_handler"


def test_hinglish_opt_out_routes(gate):
    """VT-329 (Cowork): 'band karo' (+ 'please बंद करो') route opt_out_handler — opt-out is now
    boundary-safe containment, not whole-body-exact."""
    tenant_id = _new_tenant(gate.dsn)
    sub = _state(gate, tenant_id)
    for body in ("band karo", "please बंद करो"):
        result = gate.pre_filter(_inbound(gate, body), sub)
        assert isinstance(result, gate.t.RouteToDirectHandler)
        assert result.handler_name == "opt_out_handler"


@pytest.mark.parametrize("cb_state", ["delivered", "read", "undelivered"])
def test_status_callback_delivery_routes_to_reconciler(gate, cb_state):
    """VT-564: delivered/read/undelivered callbacks now route to the customer-send delivery
    reconciler (was: delivered/read Rejected, undelivered → brain)."""
    result = gate.pre_filter(_callback(gate, cb_state), _state(gate, uuid4()))
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "customer_send_delivery_handler"


def test_status_callback_failed_routes_to_template_error_handler(gate):
    result = gate.pre_filter(_callback(gate, "failed"), _state(gate, uuid4()))
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "template_error_handler"


def test_status_query_converses_via_brain_by_default(gate):
    """VT-583 (CL-2026-07-03-conversing-surfaces): a genuine STATUS query now CONVERSES — it routes to
    the brain (query tools + conversation window) instead of the canned status_ping template. Default ON."""
    result = gate.pre_filter(_inbound(gate, "any update?"), _state(gate, uuid4()))
    assert isinstance(result, gate.t.RouteToBrain)
    assert "status_query" in (result.reason or "")


def test_status_ping_falls_back_to_handler_when_converse_off(gate, monkeypatch):
    """VT-583: with CONVERSE_STATUS_QUERIES turned off, the deterministic status_ping_handler is the
    fail-soft fallback (unchanged truthful send). VT-464 D2: a genuine status query still matches Rule f."""
    monkeypatch.setenv("CONVERSE_STATUS_QUERIES", "0")
    tenant_id = _new_tenant(gate.dsn)
    sub = _state(gate, tenant_id)
    result = gate.pre_filter(_inbound(gate, "any update?"), sub)
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "status_ping_handler"

    outcome = gate.HANDLERS["status_ping_handler"](_inbound(gate, "any update?"), sub)
    assert outcome["send_result"]["success"] is True  # VT-3.3c: honest send
    assert "trial" in outcome["status_text"]  # accurate phase, no fabrication


@pytest.mark.parametrize("greeting", ["Hi", "Hello", "Hey", "hey there", "namaste"])
def test_bare_greeting_falls_through_to_brain(gate, greeting):
    """VT-464 D2 (HEADLINE): a bare greeting must reach the brain, NOT
    status_ping. The old _STATUS_PING regex swallowed hi/hello/hey into
    status_ping_handler BEFORE the brain ran, bypassing the rebuilt
    Team-Manager's 'Hi → business-manager, not customer-service' greeting +
    onboarding. DPDP routing (opt-out/DSR/consent) is unchanged."""
    result = gate.pre_filter(_inbound(gate, greeting), _state(gate, uuid4()))
    assert isinstance(result, gate.t.RouteToBrain), (
        f"bare greeting {greeting!r} must route to the brain, got {result!r}"
    )


@pytest.mark.parametrize(
    "query", ["any update?", "any updates", "what's the status", "kya hua"]
)
def test_status_query_matches_rule_f_and_converses(gate, query):
    """VT-464 D2 + VT-583: genuine status-intent phrases still match Rule f, and now converse via the
    brain by default (CONVERSE_STATUS_QUERIES on)."""
    result = gate.pre_filter(_inbound(gate, query), _state(gate, uuid4()))
    assert isinstance(result, gate.t.RouteToBrain)
    assert "status_query" in (result.reason or "")


@pytest.mark.parametrize(
    "query", ["any update?", "any updates", "what's the status", "kya hua"]
)
def test_status_query_falls_back_to_handler_when_converse_off(gate, monkeypatch, query):
    """VT-583: the same phrases route to the deterministic status_ping_handler when converse is off."""
    monkeypatch.setenv("CONVERSE_STATUS_QUERIES", "0")
    result = gate.pre_filter(_inbound(gate, query), _state(gate, uuid4()))
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "status_ping_handler"


def test_substantive_message_routes_to_brain(gate):
    result = gate.pre_filter(
        _inbound(gate, "I want to launch a campaign for dormant customers"),
        _state(gate, uuid4()),
    )
    assert isinstance(result, gate.t.RouteToBrain)
    assert "substantive owner message" in result.reason


def test_ambiguous_message_routes_to_brain(gate):
    result = gate.pre_filter(
        _inbound(gate, "how are things going with the cafe customers"),
        _state(gate, uuid4()),
    )
    assert isinstance(result, gate.t.RouteToBrain)


def test_duplicate_event_routes_to_dupe_handler(gate):
    """VT-3.3a: a duplicate-flagged event routes to dupe_handler, even when
    its body would otherwise match another rule."""
    event = gate.t.WebhookEvent(body="STOP", dupe_status=True)
    result = gate.pre_filter(event, _state(gate, uuid4()))
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "dupe_handler"


# --- Capture ratio -----------------------------------------------------------


def test_capture_ratio_on_synthetic_event_mix(gate):
    """A synthetic 100-event mix routes 60-80% to direct handlers / reject.

    Capture ratio = (RouteToDirectHandler + Reject) / total (Notion §4).
    """
    sub = _state(gate, uuid4())
    events: list = []
    events += [_inbound(gate, "STOP") for _ in range(20)]
    events += [_inbound(gate, "बंद करो") for _ in range(20)]
    events += [_inbound(gate, "please process data deletion") for _ in range(15)]
    events += [_inbound(gate, "hi") for _ in range(5)]
    events += [_inbound(gate, "any update") for _ in range(5)]
    events += [_callback(gate, "failed") for _ in range(5)]
    events += [_callback(gate, "delivered") for _ in range(5)]
    events += [
        _inbound(gate, f"I want to plan campaign number {i} for my shop")
        for i in range(25)
    ]
    assert len(events) == 100

    captured = 0
    for event in events:
        result = gate.pre_filter(event, sub)
        if isinstance(result, (gate.t.RouteToDirectHandler, gate.t.Reject)):
            captured += 1

    ratio = captured / len(events)
    assert 0.60 <= ratio <= 0.80, f"capture ratio {ratio:.2f} outside 60-80%"
