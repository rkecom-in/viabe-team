"""VT-694 — suggestion-button delivery (dep-less units)."""
from __future__ import annotations

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

from orchestrator.onboarding import journey as j  # noqa: E402

_TID = "22222222-2222-2222-2222-222222222222"


def _wire(monkeypatch, *, content_sid="HXsuggest", raise_send=False):
    sent: dict = {}
    import orchestrator.templates_registry as tr
    import orchestrator.utils.twilio_send as ts

    monkeypatch.setattr(tr, "content_sid_for", lambda name, lang="en": content_sid)

    def _send(sid, phone, *, content_variables=None, tenant_id=None, surface=None):
        if raise_send:
            raise RuntimeError("transport down")
        sent.update({"sid": sid, "vars": content_variables})
        return "MKDEVx"

    monkeypatch.setattr(ts, "send_interactive_message", _send)
    return sent


def test_suggestion_send_pads_with_skip(monkeypatch) -> None:
    sent = _wire(monkeypatch)
    ok = j._send_suggestion_buttons("+919811112222", "Hours?", ["24/7 online"], tenant_id=_TID)
    assert ok is True
    assert sent["vars"] == {"1": "Hours?", "2": "24/7 online", "3": "Skip", "4": "Skip"}


def test_suggestion_send_clamps_and_orders(monkeypatch) -> None:
    sent = _wire(monkeypatch)
    ok = j._send_suggestion_buttons(
        "+919811112222", "Q?", ["Most likely answer!!", "Alt two", "Alt three", "Alt four"],
        tenant_id=_TID,
    )
    assert ok and sent["vars"]["2"] == "Most likely answer!!"[:20]
    assert "Alt four" not in sent["vars"].values()


def test_suggestion_send_false_paths(monkeypatch) -> None:
    _wire(monkeypatch)
    assert j._send_suggestion_buttons("+91981", "Q?", [], tenant_id=_TID) is False
    assert j._send_suggestion_buttons(None, "Q?", ["A"], tenant_id=_TID) is False
    _wire(monkeypatch, raise_send=True)
    assert j._send_suggestion_buttons("+91981", "Q?", ["A"], tenant_id=_TID) is False


def test_walker_send_routes_gap_suggestions(monkeypatch) -> None:
    sent = _wire(monkeypatch)
    frees: list[str] = []
    import orchestrator.utils.twilio_send as ts

    monkeypatch.setattr(ts, "send_freeform_message",
                        lambda text, phone, **k: frees.append(text) or "MK1")
    q = {"kind": "gap", "prompt_en": "Hours?", "prompt_hi": "?",
         "suggestions_en": ["24/7 online", "10am-9pm"]}
    j._send("+919811112222", q, "en", tenant_id=_TID)
    assert sent["vars"]["1"] == "Hours?" and sent["vars"]["2"] == "24/7 online"
    assert frees == [], "buttons delivered — no freeform double-send"


def test_turn_brain_dynamic_buttons_deliver(monkeypatch) -> None:
    sent = _wire(monkeypatch)
    import orchestrator.utils.twilio_send as ts

    monkeypatch.setattr(ts, "send_freeform_message", lambda *a, **k: "MK1")
    j._send_turn("+919811112222", "Busiest days?", ["Weekends", "Festivals"], "en", tenant_id=None)
    assert sent["vars"]["2"] == "Weekends" and sent["vars"]["4"] == "Skip"


def test_pending_gst_card_outranks_stale_queue(monkeypatch) -> None:
    """VT-693/694 recovery: mid-queue, a pending GST identity card replaces whatever the stale
    queue holds next — identity first; the flushed queue recomposes post-card."""
    from uuid import uuid4

    tid = str(uuid4())
    state = {
        "status": "active", "cursor": 0, "last_message_sid": None,
        "question_queue": [
            {"field": "key_business_challenges", "kind": "gap",
             "prompt_en": "Challenges?", "prompt_hi": "?"},
            {"field": "number_of_employees", "kind": "gap",
             "prompt_en": "Employees?", "prompt_hi": "?"},
        ],
        "answers": {}, "skipped": [],
    }
    monkeypatch.setattr(j, "get_journey", lambda t: dict(state))
    installed: dict = {}

    def _install(t, queue, sid):
        installed["fields"] = [q["field"] for q in queue]
        state["question_queue"] = list(queue)
        state["cursor"] = 0

    monkeypatch.setattr(j, "_install_recomposed_queue", _install)
    monkeypatch.setattr(j, "_advance", lambda *a, **k: None)
    import orchestrator.onboarding.whatsapp_journey as wj

    monkeypatch.setattr(wj, "gst_identity_pending", lambda t, a: True)
    monkeypatch.setattr(
        wj, "gst_identity_card_question",
        lambda t: {"field": "gst_identity", "kind": "confirm", "draft_value": "yes",
                   "prompt_en": "Here's what I found — is this your business?",
                   "prompt_hi": "क्या यही आपका बिज़नेस है?"},
    )
    out = j.handle_reply(tid, "Consulting and BI work", "SMx1", lang="en")
    assert installed["fields"] == ["gst_identity"], "card replaces the stale queue"
    assert "is this your business" in out["reply_en"].lower()


def test_turn_brain_path_card_priority(monkeypatch) -> None:
    """VT-693 live-proven gap: the TURN-BRAIN path must also yield to a pending GST card —
    after recording this turn's answer, the card replaces the plan's next question and goes
    out as the Yes/No confirm."""
    from uuid import uuid4

    tid = str(uuid4())
    state = {
        "status": "active", "cursor": 0, "last_message_sid": None,
        "question_queue": [{"field": "key_business_challenges", "kind": "gap",
                            "prompt_en": "Challenges?", "prompt_hi": "?"}],
        "answers": {}, "skipped": [],
    }
    monkeypatch.setattr(j, "get_journey", lambda t: dict(state))
    monkeypatch.setattr(j, "_advance", lambda *a, **k: None)
    monkeypatch.setattr(j, "_append_recent_turns", lambda *a, **k: None)
    monkeypatch.setattr(j, "_capture_missed_about_gap", lambda *a, **k: None)
    monkeypatch.setattr(j, "_advance_cursor_past_answered", lambda g, a, s: 1)
    installed: dict = {}
    monkeypatch.setattr(j, "_install_recomposed_queue",
                        lambda t, q, sid: installed.__setitem__("fields", [x["field"] for x in q]))

    import orchestrator.onboarding.whatsapp_journey as wj

    monkeypatch.setattr(wj, "gst_identity_pending", lambda t, a: True)
    monkeypatch.setattr(wj, "gst_identity_card_question",
                        lambda t: {"field": "gst_identity", "kind": "confirm", "draft_value": "yes",
                                   "prompt_en": "Found Rkecom — is this your business?",
                                   "prompt_hi": "क्या यही आपका बिज़नेस है?"})

    import orchestrator.onboarding.turn_brain as tb
    from types import SimpleNamespace

    plan = SimpleNamespace(reply_text="And your team size?", buttons=(), extracted_answers={},
                           mark_confirmed=[], mark_rejected=[], done_hint=False, reasoning="")
    monkeypatch.setattr(tb, "compose_turn", lambda *a, **k: plan)
    monkeypatch.setattr(j, "populate_profile_from_draft", lambda t: {})
    import orchestrator.onboarding.draft_profile as dp

    monkeypatch.setattr(dp, "get_draft", lambda t: {"attributes": {}, "provenance": {}})

    r = j._handle_reply_with_turn_brain(tid, "Customer reach", "SMtb1", lang="en")
    assert r.get("turn_brain") is True and r.get("buttons") == ["Yes", "No", "Skip"]
    assert "is this your business" in r["reply_text"].lower()
    assert installed.get("fields") == ["gst_identity"], "card replaces the plan's queue"


def test_turn_brain_gst_identity_deterministic_pre_handling(monkeypatch) -> None:
    """VT-693 live gap #2: a typed identity decision ('This is mine, but…') on the presented
    card runs accept BEFORE the LLM turn — side effects fire, the answer records, and the
    correction in the same message still reaches the turn-brain."""
    from types import SimpleNamespace
    from uuid import uuid4

    tid = str(uuid4())
    card = {"field": "gst_identity", "kind": "confirm", "draft_value": "yes",
            "prompt_en": "Is this your business?", "prompt_hi": "?"}
    state = {"status": "active", "cursor": 0, "last_message_sid": None,
             "question_queue": [card], "answers": {}, "skipped": []}
    monkeypatch.setattr(j, "get_journey", lambda t: dict(state))
    advanced: dict = {}

    def _adv(t, cursor, answers, skipped, sid):
        advanced.update(cursor=cursor, answers=dict(answers))
        state["cursor"] = cursor
        state["answers"] = dict(answers)

    monkeypatch.setattr(j, "_advance", _adv)
    monkeypatch.setattr(j, "_append_recent_turns", lambda *a, **k: None)
    monkeypatch.setattr(j, "_capture_missed_about_gap", lambda *a, **k: None)
    monkeypatch.setattr(j, "_advance_cursor_past_answered", lambda g, a, s: state["cursor"])
    monkeypatch.setattr(j, "_install_recomposed_queue", lambda *a, **k: None)
    monkeypatch.setattr(j, "populate_profile_from_draft", lambda t: {})
    monkeypatch.setattr(j, "_complete_or_hold",
                        lambda *a, **k: {"reply_en": "next", "reply_hi": "n", "done": False})

    accepted: dict = {}
    import orchestrator.onboarding.whatsapp_journey as wj

    monkeypatch.setattr(wj, "accept_gst_identity", lambda t: accepted.__setitem__("t", t))
    monkeypatch.setattr(wj, "decline_gst_identity", lambda t: accepted.__setitem__("declined", t))
    monkeypatch.setattr(wj, "gst_identity_pending", lambda t, a: False)

    import orchestrator.onboarding.turn_brain as tb

    plan = SimpleNamespace(reply_text="Noted — BI services.", buttons=(), extracted_answers={},
                           mark_confirmed=[], mark_rejected=[], done_hint=False, reasoning="")
    monkeypatch.setattr(tb, "compose_turn", lambda *a, **k: plan)
    import orchestrator.onboarding.draft_profile as dp

    monkeypatch.setattr(dp, "get_draft", lambda t: {"attributes": {}, "provenance": {}})

    j._handle_reply_with_turn_brain(
        tid, "This is mine, but our nature of business is Business Intelligence Services",
        "SMgst1", lang="en",
    )
    assert accepted.get("t") == tid, "accept side effects MUST fire on 'this is mine'"
    assert advanced["answers"].get("gst_identity") == "yes"
    assert "declined" not in accepted
