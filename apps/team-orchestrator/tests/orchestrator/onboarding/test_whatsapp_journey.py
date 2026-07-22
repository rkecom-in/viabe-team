"""VT-692 — WhatsApp-journey discovery kick + promotion + completion belt (dep-less units).

Invariants under test: web tenants structurally untouchable (created_via gate + fill-empty-only);
discovery kicked at most once (workflow-id idempotency); GSTIN only ever a single-candidate HINT;
off-taxonomy business_type never asserted; the belt never fires while discovery is in flight.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

from orchestrator.onboarding import whatsapp_journey as wj  # noqa: E402

_TID = str(uuid4())
_ANSWERS = {
    "business_name": "Rkecom Services Pvt Ltd", "owner_name": "Lubna Khan",
    "business_type": "Business Intelligence Services", "city": "Mumbai",
}


def _wire(monkeypatch, *, created_via="whatsapp", wf_status=None, tenant_row=None):
    calls: dict[str, Any] = {"started": [], "updates": []}
    row = tenant_row if tenant_row is not None else {
        "created_via": created_via, "business_name": "", "business_type": None,
        "city_tier": None, "whatsapp_number": "+919900112233",
    }
    monkeypatch.setattr(wj, "_tenant_row", lambda t: row)

    from dbos import DBOS

    monkeypatch.setattr(DBOS, "get_workflow_status", staticmethod(lambda wid: wf_status))

    def _start(fn, *a, **k):
        calls["started"].append(a)

    monkeypatch.setattr(DBOS, "start_workflow", staticmethod(_start))
    return calls


def test_kick_fires_once_for_whatsapp_tenant(monkeypatch) -> None:
    calls = _wire(monkeypatch)
    import orchestrator.feature_flags as ff

    monkeypatch.setattr(ff, "llm_discovery_enabled", lambda: False)  # no LLM leg in unit env
    assert wj.maybe_kick_discovery(_TID, _ANSWERS) is True
    assert len(calls["started"]) == 1
    tid_arg, seed = calls["started"][0]
    assert tid_arg == _TID
    assert seed["business_name"] == "Rkecom Services Pvt Ltd"
    assert seed["city"] == "Mumbai"
    assert seed["gstin"] is None
    assert seed["business_type"] is None, "free text must never ride the seed as a type"


def test_kick_idempotent_when_workflow_exists(monkeypatch) -> None:
    calls = _wire(monkeypatch, wf_status=SimpleNamespace(status="SUCCESS"))
    assert wj.maybe_kick_discovery(_TID, _ANSWERS) is False
    assert calls["started"] == []


def test_kick_never_fires_for_web_tenant(monkeypatch) -> None:
    calls = _wire(monkeypatch, created_via="web")
    assert wj.maybe_kick_discovery(_TID, _ANSWERS) is False
    assert calls["started"] == []


def test_kick_needs_business_name(monkeypatch) -> None:
    calls = _wire(monkeypatch)
    assert wj.maybe_kick_discovery(_TID, {"city": "Mumbai"}) is False
    assert calls["started"] == []


def test_gstin_hint_only_on_single_candidate(monkeypatch) -> None:
    import orchestrator.feature_flags as ff

    monkeypatch.setattr(ff, "llm_discovery_enabled", lambda: True)
    import orchestrator.onboarding.entity_match as em

    one = [SimpleNamespace(candidate_gstin="27ABCDE1234F1Z5"),
           SimpleNamespace(candidate_gstin="27ABCDE1234F1Z5"),
           SimpleNamespace(candidate_gstin=None)]
    monkeypatch.setattr(em, "fetch_candidates", lambda n, c, **k: one)
    assert wj._gstin_candidate("Rkecom", "Mumbai") == "27ABCDE1234F1Z5"

    two = [SimpleNamespace(candidate_gstin="27ABCDE1234F1Z5"),
           SimpleNamespace(candidate_gstin="29XYZDE9876K2A1")]
    monkeypatch.setattr(em, "fetch_candidates", lambda n, c, **k: two)
    assert wj._gstin_candidate("Rkecom", "Mumbai") is None, "ambiguity must yield no hint"


def test_belt_false_while_discovery_pending(monkeypatch) -> None:
    _wire(monkeypatch, wf_status=SimpleNamespace(status="PENDING"))
    import orchestrator.onboarding.draft_profile as dp

    monkeypatch.setattr(dp, "get_draft", lambda t: {})
    assert wj.should_force_complete(_TID, _ANSWERS) is False


def test_belt_true_when_no_draft_and_discovery_terminal(monkeypatch) -> None:
    _wire(monkeypatch, wf_status=SimpleNamespace(status="SUCCESS"))
    import orchestrator.onboarding.draft_profile as dp

    monkeypatch.setattr(dp, "get_draft", lambda t: {})
    assert wj.should_force_complete(_TID, _ANSWERS) is True


def test_belt_false_when_draft_exists(monkeypatch) -> None:
    _wire(monkeypatch, wf_status=SimpleNamespace(status="SUCCESS"))
    import orchestrator.onboarding.draft_profile as dp

    monkeypatch.setattr(dp, "get_draft", lambda t: {"attributes": {"category": "x"}})
    assert wj.should_force_complete(_TID, _ANSWERS) is False


def test_belt_false_for_web_tenant_or_missing_core(monkeypatch) -> None:
    _wire(monkeypatch, created_via="web", wf_status=None)
    import orchestrator.onboarding.draft_profile as dp

    monkeypatch.setattr(dp, "get_draft", lambda t: {})
    assert wj.should_force_complete(_TID, _ANSWERS) is False
    _wire(monkeypatch, wf_status=None)
    assert wj.should_force_complete(_TID, {"business_name": "x"}) is False


def test_on_answers_advanced_never_raises(monkeypatch) -> None:
    def _boom(t):
        raise RuntimeError("db down")

    monkeypatch.setattr(wj, "_tenant_row", _boom)
    wj.on_answers_advanced(_TID, _ANSWERS)  # must not raise
    wj.on_answers_advanced(_TID, {})        # no core fields → cheap no-op


# --- VT-692 addendum: post-discovery follow-through push -----------------------------------------


def _wire_push(monkeypatch, *, created_via="whatsapp", journey=None, queue=None,
               complete=False, pending_push=False):
    calls: dict[str, Any] = {"enqueued": [], "installed": None, "completed": False}
    row = {"created_via": created_via, "business_name": "", "business_type": None,
           "city_tier": None, "whatsapp_number": "+919900112233"}
    monkeypatch.setattr(wj, "_tenant_row", lambda t: row)

    class _Cur:
        def __init__(self, r): self._r = r
        def fetchone(self): return self._r

    class _Conn:
        def execute(self, sql, p=None): return _Cur((1,) if pending_push else None)

    class _CM:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    import sys

    import orchestrator.db.tenant_connection  # noqa: F401 — ensure the submodule is loaded

    tc_mod = sys.modules["orchestrator.db.tenant_connection"]
    monkeypatch.setattr(tc_mod, "tenant_connection", lambda t: _CM())

    import orchestrator.onboarding.journey as j

    monkeypatch.setattr(j, "get_journey", lambda t: journey)
    monkeypatch.setattr(j, "_tenant_phase_and_type", lambda t: ("onboarding", None))
    monkeypatch.setattr(j, "populate_profile_from_draft", lambda t: {})
    monkeypatch.setattr(j, "_compose_queue", lambda t, bt: queue or [])
    monkeypatch.setattr(j, "_install_recomposed_queue",
                        lambda t, q, sid: calls.__setitem__("installed", [x["field"] for x in q]))
    monkeypatch.setattr(j, "_journey_profile_complete", lambda t, bt, a, s: complete)
    monkeypatch.setattr(j, "_complete", lambda t: calls.__setitem__("completed", True))
    monkeypatch.setattr(j, "_completion_message",
                        lambda a: {"reply_en": "All set — recap.", "reply_hi": "हो गया।"})

    import orchestrator.owner_surface.owner_comms_queue as cq

    monkeypatch.setattr(cq, "enqueue",
                        lambda t, *, kind, payload, priority=None, **k:
                        calls["enqueued"].append({"kind": kind, "payload": payload}) or uuid4())
    return calls


_ACTIVE = {"status": "active", "answers": {"business_name": "X"}, "skipped": []}


def test_push_enqueues_recomposed_head(monkeypatch) -> None:
    q = [{"field": "gstin", "kind": "confirm", "prompt_en": "We found GSTIN 27AB… — is that right?",
          "prompt_hi": "हमें GSTIN मिला — सही है?"}]
    calls = _wire_push(monkeypatch, journey=_ACTIVE, queue=q)
    assert wj.push_next_question_after_discovery(_TID) is True
    assert calls["installed"] == ["gstin"]
    assert calls["enqueued"][0]["payload"]["journey_push"] == "true"
    assert "GSTIN" in calls["enqueued"][0]["payload"]["text_en"]


def test_push_completes_and_enqueues_recap_when_done(monkeypatch) -> None:
    calls = _wire_push(monkeypatch, journey=_ACTIVE, queue=[], complete=True)
    assert wj.push_next_question_after_discovery(_TID) is True
    assert calls["completed"] is True
    assert calls["enqueued"][0]["payload"]["text_en"] == "All set — recap."


def test_push_never_enqueues_empty_promise(monkeypatch) -> None:
    calls = _wire_push(monkeypatch, journey=_ACTIVE, queue=[], complete=False)
    assert wj.push_next_question_after_discovery(_TID) is False
    assert calls["enqueued"] == []


def test_push_dedups_and_gates(monkeypatch) -> None:
    calls = _wire_push(monkeypatch, journey=_ACTIVE, queue=[{"field": "x", "kind": "gap",
                       "prompt_en": "Q", "prompt_hi": "Q"}], pending_push=True)
    assert wj.push_next_question_after_discovery(_TID) is False  # one pending push at a time
    calls2 = _wire_push(monkeypatch, created_via="web", journey=_ACTIVE, queue=[])
    assert wj.push_next_question_after_discovery(_TID) is False  # web tenants never pushed
    calls3 = _wire_push(monkeypatch, journey=None)
    assert wj.push_next_question_after_discovery(_TID) is False  # no active journey
    assert calls["enqueued"] == calls2["enqueued"] == calls3["enqueued"] == []
