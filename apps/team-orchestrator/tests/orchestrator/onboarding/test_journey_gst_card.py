"""VT-695 — the FORMATTED GST identity card (dep-less units).

The semicolon-blob confirm becomes a structured Content-object card (``journey_gst_card``:
static multi-line body, 5 single-line variables, fixed Yes/No/Skip). Covered here:
the per-field ``card_vars`` on the question, the address cleaner, the dedicated sender,
and the routing on BOTH send paths (walker ``_send`` + turn-brain ``_send_turn``) with
fallback to the blob confirm object on any card failure.
"""
from __future__ import annotations

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

from orchestrator.onboarding import journey as j  # noqa: E402
from orchestrator.onboarding import whatsapp_journey as wj  # noqa: E402

_TID = "33333333-3333-3333-3333-333333333333"

_ATTRS = {
    "legal_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
    "constitution": "Private Limited Company",
    "principal_address": "A/403, DHEERAJ HERITAGE RESI, SANTACRUZ WEST NEAR JUHU, MUMBAI, Mumbai, Maharashtra, 400054",
    "nature_of_business": ["Supplier of Services", "Others"],
    "gstin_candidate": "27AAKCR3738B1ZE",
}


def _patch_draft(monkeypatch, attrs=_ATTRS):
    import orchestrator.onboarding.draft_profile as dp

    monkeypatch.setattr(dp, "get_draft", lambda t: {"attributes": dict(attrs)})


def _wire(monkeypatch, *, sids=None, raise_send=False):
    """Route content_sid_for by template NAME so tests can assert WHICH object carried the send."""
    sent: dict = {}
    import orchestrator.templates_registry as tr
    import orchestrator.utils.twilio_send as ts

    table = {"journey_gst_card": "HXcard", "onboarding_confirm_yesno": "HXconfirm"}
    if sids is not None:
        table = sids
    monkeypatch.setattr(tr, "content_sid_for", lambda name, lang="en": table.get(name))

    def _send(sid, phone, *, content_variables=None, tenant_id=None, surface=None):
        if raise_send:
            raise RuntimeError("transport down")
        sent.update({"sid": sid, "vars": content_variables})
        return "MKDEVx"

    monkeypatch.setattr(ts, "send_interactive_message", _send)
    return sent


# --- the cleaner + the question's card_vars ---------------------------------------------------


def test_clean_addr_titlecases_and_dedupes() -> None:
    out = wj._clean_addr(_ATTRS["principal_address"])
    assert "MUMBAI" not in out and "Mumbai" in out, "ALLCAPS title-cased"
    assert out.count("Mumbai") == 1, "duplicate city segment dropped"
    assert "A/403" in out and "400054" in out, "unit + pincode stay verbatim"


def test_card_question_carries_card_vars(monkeypatch) -> None:
    _patch_draft(monkeypatch)
    card = wj.gst_identity_card_question(_TID)
    v = card["card_vars"]
    assert v["1"] == "RKECOM SERVICES (OPC) PRIVATE LIMITED"
    assert v["2"] == "Private Limited Company"
    assert "Dheeraj Heritage" in v["3"] and "\n" not in v["3"], "single-line cleaned address"
    assert v["4"].startswith("Supplier of Services")
    assert v["5"] == "…B1ZE" and "27AAKCR3738B1ZE" not in v["5"], "tail only, never the full GSTIN"
    assert card["prompt_en"], "blob prompt stays as the send fallback"


def test_card_vars_dash_when_field_missing(monkeypatch) -> None:
    attrs = {k: v for k, v in _ATTRS.items() if k not in ("constitution", "gstin_candidate")}
    _patch_draft(monkeypatch, attrs)
    v = wj.gst_identity_card_question(_TID)["card_vars"]
    assert v["2"] == "—" and v["5"] == "—", "missing fields render as a dash, never empty"


# --- the dedicated sender ---------------------------------------------------------------------


def test_send_gst_card_uses_card_object(monkeypatch) -> None:
    sent = _wire(monkeypatch)
    ok = j._send_gst_card("+919811112222", {"1": "N", "2": "C", "3": "A", "4": "B", "5": "…Z"}, "en")
    assert ok is True and sent["sid"] == "HXcard"
    assert sent["vars"] == {"1": "N", "2": "C", "3": "A", "4": "B", "5": "…Z"}


def test_send_gst_card_false_paths(monkeypatch) -> None:
    _wire(monkeypatch)
    assert j._send_gst_card(None, {"1": "N"}, "en") is False
    assert j._send_gst_card("+91981", None, "en") is False
    assert j._send_gst_card("+91981", {"1": " "}, "en") is False, "no name → no card"
    _wire(monkeypatch, sids={})
    assert j._send_gst_card("+91981", {"1": "N"}, "en") is False, "no SID registered"
    _wire(monkeypatch, raise_send=True)
    assert j._send_gst_card("+91981", {"1": "N"}, "en") is False, "transport failure → fallback"


# --- routing: walker _send --------------------------------------------------------------------


def _card_q(monkeypatch):
    _patch_draft(monkeypatch)
    return wj.gst_identity_card_question(_TID)


def test_walker_send_routes_gst_card(monkeypatch) -> None:
    sent = _wire(monkeypatch)
    j._send("+919811112222", _card_q(monkeypatch), "en", tenant_id=_TID)
    assert sent["sid"] == "HXcard", "gst_identity confirm rides the formatted card object"
    assert set(sent["vars"]) == {"1", "2", "3", "4", "5"}


def test_walker_send_falls_back_to_blob_confirm(monkeypatch) -> None:
    sent = _wire(monkeypatch, sids={"onboarding_confirm_yesno": "HXconfirm"})
    j._send("+919811112222", _card_q(monkeypatch), "en", tenant_id=_TID)
    assert sent["sid"] == "HXconfirm", "card SID missing → the blob confirm object still delivers"
    assert "Is this your business?" in sent["vars"]["1"]


def test_walker_send_other_confirms_untouched(monkeypatch) -> None:
    sent = _wire(monkeypatch)
    q = {"field": "city", "kind": "confirm", "prompt_en": "Mumbai — correct?", "prompt_hi": "?"}
    j._send("+919811112222", q, "en", tenant_id=_TID)
    assert sent["sid"] == "HXconfirm", "non-gst confirms keep the generic Yes/No object"


# --- routing: turn-brain _send_turn -----------------------------------------------------------


def test_send_turn_card_vars_win(monkeypatch) -> None:
    sent = _wire(monkeypatch)
    j._send_turn(
        "+919811112222", "blob text", ["Yes", "No", "Skip"], "en",
        card_vars={"1": "N", "2": "C", "3": "A", "4": "B", "5": "…Z"},
    )
    assert sent["sid"] == "HXcard", "card-priority injection delivers the formatted card"


def test_send_turn_without_card_vars_keeps_confirm(monkeypatch) -> None:
    sent = _wire(monkeypatch)
    j._send_turn("+919811112222", "blob text", ["Yes", "No", "Skip"], "en")
    assert sent["sid"] == "HXconfirm"


def test_send_turn_card_failure_falls_back(monkeypatch) -> None:
    sent = _wire(monkeypatch, sids={"onboarding_confirm_yesno": "HXconfirm"})
    j._send_turn(
        "+919811112222", "blob text", ["Yes", "No", "Skip"], "en",
        card_vars={"1": "N", "2": "C", "3": "A", "4": "B", "5": "…Z"},
    )
    assert sent["sid"] == "HXconfirm", "no card SID → Yes/No blob confirm still goes out"


