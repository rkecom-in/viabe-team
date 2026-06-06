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
    monkeypatch.setattr(h, "send_freeform_message", lambda body, phone: "SMfake")
    tenant_id = _new_tenant(gate.dsn)
    sub = _state(gate, tenant_id)
    outcome = h.data_inputs_enable_handler(_inbound(gate, "ACTIVATE TEAM"), sub)
    assert outcome["owner_inputs_set"] is True
    with psycopg.connect(gate.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT owner_inputs FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    assert row == (True,)


def test_consent_required_handler_sends_prompt_no_transmit(gate, monkeypatch):
    import importlib

    h = importlib.import_module(
        "orchestrator.direct_handlers.consent_required_handler"
    )
    sent: dict[str, str] = {}

    def _capture(body: str, phone: str) -> str:
        sent["body"], sent["phone"] = body, phone
        return "SMfake"

    monkeypatch.setattr(h, "send_freeform_message", _capture)
    sub = _state(gate, _new_tenant(gate.dsn))
    outcome = h.consent_required_handler(_inbound(gate, "plan a campaign"), sub)
    assert outcome["handler"] == "consent_required_handler"
    assert outcome["consent_prompt_sent"] is True
    # The prompt tells the owner exactly which phrase enables data inputs.
    assert "ACTIVATE TEAM" in sent["body"]


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
    monkeypatch.setattr(h, "send_freeform_message", lambda body, phone: "SMfake")
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


# --- VT-329 — Devanagari boundary + mixed-script DSR matching --------------------------------
def test_devanagari_dsr_matches_pure() -> None:
    """VT-329: the Devanagari DSR keyword (मेरा डेटा) now fires — it was 100% DEAD under `\\b`
    (matras ∉ \\w → the trailing `\\b` could never anchor). Pure helper-level proof."""
    from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

    assert matches_opt_out_or_dsr("मेरा डेटा हटाओ") is True  # was False (dead pattern)
    assert matches_opt_out_or_dsr("refund मेरा डेटा delete") is True
    assert matches_opt_out_or_dsr("hello how are you") is False  # benign, no over-fire


def test_mixed_script_dsr_matches_pure() -> None:
    """VT-329 (Cowork): owners code-switch mid-sentence — a COMPLETE Devanagari keyword surrounded
    by EN/Hinglish must still route. (A SCRIPT-SPLIT phrase like 'मेरा data' — मेरा + EN 'data',
    no complete keyword — does NOT route; that's keyword curation, Type-2 / VT-8, out of scope.)"""
    from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

    assert matches_opt_out_or_dsr("मेरा डेटा delete karo") is True
    assert matches_opt_out_or_dsr("please delete मेरा डेटा now") is True


def test_devanagari_stem_through_matra_is_intended_failsafe() -> None:
    """VT-329 (Cowork): a Devanagari keyword ending in a bare consonant matches THROUGH a
    following matra (matras ∉ \\w → (?!\\w) passes) — e.g. stem 'हट' fires inside 'हटाओ'. This
    over-match is INTENDED + fail-safe for DSR/opt-out: over-route a deletion/opt-out request,
    never miss one, and it usefully covers Devanagari inflections. A following CONSONANT (\\w)
    still blocks it (no runaway match into an unrelated word)."""
    import re

    pat = re.compile(r"(?<!\w)हट(?!\w)", re.IGNORECASE | re.UNICODE)
    assert pat.search("हटाओ") is not None  # stem-through-matra — by design
    assert pat.search("मेरा डेटा हटाओ") is not None
    assert pat.search("हटक") is None  # क is \w → boundary holds, no false-extend


def test_devanagari_dsr_routes_creates_ticket(gate):
    """VT-329 end-to-end: a Devanagari DSR message routes to dsr_handler (it was silently dropped
    before — the launch-gate failure mode)."""
    tenant_id = _new_tenant(gate.dsn)
    sub = _state(gate, tenant_id)
    event = _inbound(gate, "मेरा डेटा हटाओ please")

    result = gate.pre_filter(event, sub)
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "dsr_handler"


def test_status_callback_delivered_is_rejected(gate):
    result = gate.pre_filter(_callback(gate, "delivered"), _state(gate, uuid4()))
    assert isinstance(result, gate.t.Reject)
    assert "observability" in result.reason


def test_status_callback_failed_routes_to_template_error_handler(gate):
    result = gate.pre_filter(_callback(gate, "failed"), _state(gate, uuid4()))
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "template_error_handler"


def test_status_ping_routes_to_status_ping_handler(gate):
    tenant_id = _new_tenant(gate.dsn)
    sub = _state(gate, tenant_id)
    result = gate.pre_filter(_inbound(gate, "hi"), sub)
    assert isinstance(result, gate.t.RouteToDirectHandler)
    assert result.handler_name == "status_ping_handler"

    outcome = gate.HANDLERS["status_ping_handler"](_inbound(gate, "hi"), sub)
    assert outcome["send_result"]["success"] is True  # VT-3.3c: honest send
    assert "trial" in outcome["status_text"]  # accurate phase, no fabrication


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
