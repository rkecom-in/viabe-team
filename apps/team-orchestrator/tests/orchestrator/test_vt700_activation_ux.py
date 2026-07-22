"""VT-700 — the buttoned, Manager-introducing activation ask + the post-grant agent chooser.

Fazal (live, 2026-07-23): the ACTIVATE TEAM ask "could have had a button", "needs to introduce
the Manager", and after the go-ahead "the owner must be able to choose from the list of
specialist agents to activate". Dep-less units over both direct handlers (sends monkeypatched).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import importlib  # noqa: E402

# The package re-exports the handler FUNCTIONS under the module names — import the MODULES.
crh = importlib.import_module("orchestrator.direct_handlers.consent_required_handler")
deh = importlib.import_module("orchestrator.direct_handlers.data_inputs_enable_handler")

_TID = "44444444-4444-4444-4444-444444444444"


def _event(body: str = "hello") -> Any:
    return SimpleNamespace(sender_phone="+919999004001", body=body)


def _state() -> dict[str, Any]:
    return {"tenant_id": _TID}


def _wire_interactive(monkeypatch, module, *, sid_table=None, raise_send=False):
    sent: dict[str, Any] = {}
    import orchestrator.templates_registry as tr
    import orchestrator.utils.twilio_send as ts

    table = sid_table if sid_table is not None else {
        "team_activate_button": "HXactivate", "journey_suggest_3": "HXsuggest",
    }
    monkeypatch.setattr(tr, "content_sid_for", lambda name, lang="en": table.get(name))

    def _interactive(sid, phone, *, content_variables=None, tenant_id=None, surface=None, **k):
        if raise_send:
            raise RuntimeError("transport down")
        sent.update({"sid": sid, "vars": content_variables, "surface": surface})
        return "SMint"

    monkeypatch.setattr(ts, "send_interactive_message", _interactive)
    frees: dict[str, Any] = {}
    monkeypatch.setattr(
        module, "send_freeform_message",
        lambda body, phone, **k: frees.update({"body": body}) or "SMfree",
    )
    return sent, frees


# --- the activation ask -----------------------------------------------------------------------


def test_consent_prompt_introduces_the_manager() -> None:
    p = crh._CONSENT_PROMPT
    assert "I'm your Manager" in p, "the ask introduces WHO the owner hired"
    assert "nothing goes to a customer without your approval" in p
    assert "ACTIVATE TEAM" in p, "the exact grant floor stays advertised"
    assert "STOP" in p, "the consent-bearing pause language is carried verbatim"
    assert "process your messages and customer data" in p


def test_consent_ask_rides_the_button_object(monkeypatch) -> None:
    sent, frees = _wire_interactive(monkeypatch, crh)
    out = crh.consent_required_handler(_event(), _state())
    assert out["consent_prompt_sent"] is True
    assert sent["sid"] == "HXactivate" and sent["surface"] == "system"
    assert "ACTIVATE TEAM" in sent["vars"]["1"], (
        "the full prompt rides {{1}} so the log marker keeps the enable phrase"
    )
    assert "body" not in frees, "no double-send"


def test_consent_ask_falls_back_to_freeform(monkeypatch) -> None:
    sent, frees = _wire_interactive(monkeypatch, crh, sid_table={})
    out = crh.consent_required_handler(_event(), _state())
    assert out["consent_prompt_sent"] is True
    assert "sid" not in sent and "ACTIVATE TEAM" in frees["body"]
    _, frees2 = _wire_interactive(monkeypatch, crh, raise_send=True)
    out2 = crh.consent_required_handler(_event(), _state())
    assert out2["consent_prompt_sent"] is True and "ACTIVATE TEAM" in frees2["body"]


def test_consent_decline_ack_survives_interactive(monkeypatch) -> None:
    sent, _ = _wire_interactive(monkeypatch, crh)
    crh.consent_required_handler(_event("no thanks, not right now"), _state())
    assert sent["vars"]["1"].startswith(crh._DECLINE_ACK)


# --- the post-grant agent chooser -------------------------------------------------------------


def _wire_enable(monkeypatch, *, journey=None, **kw):
    class _Conn:
        def execute(self, *a):
            return self

    class _CM:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(deh, "tenant_connection", lambda t: _CM())
    import orchestrator.onboarding.journey as j

    monkeypatch.setattr(j, "get_journey", lambda t: journey)
    flows: dict[str, Any] = {}
    monkeypatch.setattr(j, "_set_flow", lambda t, f, **k: flows.update({"flow": f}))
    sent, frees = _wire_interactive(monkeypatch, deh, **kw)
    return sent, frees, flows


def test_enable_confirm_carries_agent_chooser_buttons(monkeypatch) -> None:
    sent, frees, flows = _wire_enable(
        monkeypatch, journey={"status": "complete", "answers": {}}
    )
    out = deh.data_inputs_enable_handler(_event("ACTIVATE TEAM"), _state())
    assert out["owner_inputs_set"] is True
    assert sent["sid"] == "HXsuggest"
    assert sent["vars"]["2"] == "Sales Recovery"
    assert sent["vars"]["3"] == "Customer Win-back"
    assert sent["vars"]["4"] == "Campaigns"
    assert "FREE 1-month trial" in sent["vars"]["1"] and "ONLY if you choose" in sent["vars"]["1"]
    assert flows.get("flow") == "agent_choice", "the deterministic pick beat is armed"


def test_enable_chooser_not_armed_without_completed_journey(monkeypatch) -> None:
    sent, frees, flows = _wire_enable(monkeypatch, journey=None)
    deh.data_inputs_enable_handler(_event("ACTIVATE TEAM"), _state())
    assert flows == {}, "no journey → no flow arming (activation itself unaffected)"


def test_enable_falls_back_to_freeform_with_inline_options(monkeypatch) -> None:
    sent, frees, flows = _wire_enable(
        monkeypatch, journey={"status": "complete", "answers": {}}, sid_table={}
    )
    out = deh.data_inputs_enable_handler(_event("ACTIVATE TEAM"), _state())
    assert out["owner_inputs_set"] is True
    assert "Sales Recovery / Customer Win-back / Campaigns" in frees["body"]
    assert flows.get("flow") == "agent_choice"


def test_catalog_titles_stay_in_sync() -> None:
    from orchestrator.onboarding.journey import _AGENT_CATALOG

    assert [b.lower() for b in deh._AGENT_BUTTONS] == list(_AGENT_CATALOG.keys()), (
        "button titles and the journey catalog are the SAME enum — a tap echo must resolve"
    )
