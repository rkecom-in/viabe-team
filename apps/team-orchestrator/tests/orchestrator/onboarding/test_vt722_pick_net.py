"""VT-722 — enforce-mode writer parity: the mode-independent agent-pick net + the trial-terms
writer at the chooser presentation site.

The net records (draft + asserted-facts) at the runner's inbound seam BEFORE the mode split,
so the ledger fills on enforce where the walker beats never run. It never consumes the turn.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("dbos")

from orchestrator.onboarding import journey as j  # noqa: E402


class _Row(dict):
    pass


def _wire_tenant(monkeypatch, *, owner_inputs=True):
    class _Conn:
        def execute(self, sql, params=None):
            return SimpleNamespace(fetchone=lambda: _Row(owner_inputs=owner_inputs))

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("orchestrator.db.tenant_connection", lambda t: _Ctx())


def _spy_writers(monkeypatch):
    recorded: list[tuple[str, object]] = []
    drafts: list[dict] = []
    monkeypatch.setattr(
        "orchestrator.manager.asserted_facts.record_assertion",
        lambda tenant_id, key, value, **kw: recorded.append((key, value)) or True,
    )
    monkeypatch.setattr(
        "orchestrator.onboarding.draft_profile.get_draft", lambda t: {"attributes": {}}
    )
    monkeypatch.setattr(
        "orchestrator.onboarding.draft_profile.write_draft",
        lambda t, attrs, source=None: drafts.append(attrs),
    )
    return recorded, drafts


def test_exact_tap_records_pick_and_draft(monkeypatch):
    _wire_tenant(monkeypatch, owner_inputs=True)
    recorded, drafts = _spy_writers(monkeypatch)
    out = j.maybe_record_agent_pick(uuid4(), "Sales Recovery", "SMx1")
    assert out == "sales_recovery"
    assert dict(recorded).get("active_agent") == "sales_recovery"
    assert drafts and drafts[0]["activated_agents"] == ["sales_recovery"]


def test_non_tap_records_nothing(monkeypatch):
    _wire_tenant(monkeypatch, owner_inputs=True)
    recorded, drafts = _spy_writers(monkeypatch)
    assert j.maybe_record_agent_pick(uuid4(), "I want sales help please", "SMx2") is None
    assert recorded == [] and drafts == []


def test_pre_activation_gated(monkeypatch):
    """owner_inputs=False → the chooser can't have been presented; nothing records."""
    _wire_tenant(monkeypatch, owner_inputs=False)
    recorded, drafts = _spy_writers(monkeypatch)
    assert j.maybe_record_agent_pick(uuid4(), "Campaigns", "SMx3") is None
    assert recorded == [] and drafts == []


def test_net_fail_soft_on_db_error(monkeypatch):
    def _boom(t):
        raise RuntimeError("db down")

    monkeypatch.setattr("orchestrator.db.tenant_connection", _boom)
    assert j.maybe_record_agent_pick(uuid4(), "Campaigns", "SMx4") is None


def test_draft_failure_still_records_assertion(monkeypatch):
    _wire_tenant(monkeypatch, owner_inputs=True)
    recorded: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "orchestrator.manager.asserted_facts.record_assertion",
        lambda tenant_id, key, value, **kw: recorded.append((key, value)) or True,
    )

    def _boom(*a, **k):
        raise RuntimeError("draft down")

    monkeypatch.setattr("orchestrator.onboarding.draft_profile.get_draft", _boom)
    assert j.maybe_record_agent_pick(uuid4(), "Customer Win-back", "SMx5") == "customer_winback"
    assert dict(recorded).get("active_agent") == "customer_winback"


def test_chooser_handler_records_trial_terms(monkeypatch):
    """The data_inputs_enable chooser send records the trial-terms commitment (mode-independent
    presentation site)."""
    import importlib

    deh = importlib.import_module("orchestrator.direct_handlers.data_inputs_enable_handler")

    recorded: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "orchestrator.manager.asserted_facts.record_assertion",
        lambda tenant_id, key, value, **kw: recorded.append((key, value, kw)) or True,
    )
    monkeypatch.setattr(deh, "tenant_connection", lambda t: _null_ctx())
    monkeypatch.setattr(
        "orchestrator.templates_registry.content_sid_for", lambda name, lang="en": "HXchooser"
    )
    sent = {}
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.send_interactive_message",
        lambda sid, phone, **kw: sent.update(kw) or "MKDEVchooser",
    )
    monkeypatch.setattr(
        "orchestrator.onboarding.journey.get_journey", lambda t: None
    )
    event = SimpleNamespace(sender_phone="+15550001111", body="ACTIVATE TEAM")
    out = deh.data_inputs_enable_handler.__wrapped__(event, {"tenant_id": str(uuid4())}) if hasattr(
        deh.data_inputs_enable_handler, "__wrapped__"
    ) else deh.data_inputs_enable_handler(event, {"tenant_id": str(uuid4())})
    assert out["send_result"]["sid"] == "MKDEVchooser"
    keys = [r[0] for r in recorded]
    assert keys == ["trial_terms"]
    assert recorded[0][1] == {"months": 1, "auto_charge": False, "cancel_anytime": True}
    assert recorded[0][2].get("message_sid") == "MKDEVchooser"


def _null_ctx():
    class _Conn:
        def execute(self, *a, **k):
            return SimpleNamespace(fetchone=lambda: None)

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    return _Ctx()
