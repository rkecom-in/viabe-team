"""VT-648 — LLM-primary send-intent classifier + hard-stop veto + flag wiring (the MONEY GATE).

Pure unit tests: the LLM ``text_call`` is a STUB (no live Anthropic), consent is stubbed. The live
adversarial acceptance suite (``canaries/run_send_intent_suite.py``) exercises the real LLM.

Money invariant under test: the ONLY path to ``"approved"`` is a grounded, confident, un-vetoed LLM
``approve``; every other path (veto / hold / low-confidence / ungrounded cue / LLM error / empty /
no-consent) is a NON-approve. A false approve fires an irreversible unconsented send.
"""

from __future__ import annotations

import json

import pytest

# Dep-less CI 'test' job: owner_inputs/__init__ -> writer -> anthropic. Skip when absent.
pytest.importorskip("anthropic")

from orchestrator.owner_inputs.send_intent import (
    classify_send_intent,
    decide_send_intent_enforce,
    get_send_intent_mode,
    send_intent_hard_stop,
)


def _stub(payload: dict) -> object:
    """A text_call stub mirroring structured_text_call's signature; returns ``payload`` as JSON."""

    def _call(tier, *, system, user, max_tokens, agent, call_site, tenant_id=None):  # noqa: ANN001, ANN202
        return json.dumps(payload)

    return _call


def _raw_stub(raw: str) -> object:
    def _call(tier, *, system, user, max_tokens, agent, call_site, tenant_id=None):  # noqa: ANN001, ANN202
        return raw

    return _call


def _boom_stub() -> object:
    def _call(tier, *, system, user, max_tokens, agent, call_site, tenant_id=None):  # noqa: ANN001, ANN202
        raise RuntimeError("LLM down")

    return _call


_TID = "00000000-0000-0000-0000-000000000001"
_YES = lambda _t: True  # noqa: E731 — consent stub


# --------------------------------------- flag (3-state, fail-closed) --------------------------------
@pytest.mark.parametrize(
    ("val", "expected"),
    [
        (None, "off"),
        ("", "off"),
        ("off", "off"),
        ("OFF", "off"),
        ("shadow", "shadow"),
        ("Enforce", "enforce"),
        ("garbage", "off"),  # fail-closed
        ("on", "off"),  # not a valid mode -> fail-closed
    ],
)
def test_flag_fail_closed(monkeypatch, val, expected) -> None:
    if val is None:
        monkeypatch.delenv("TEAM_SEND_INTENT_LLM", raising=False)
    else:
        monkeypatch.setenv("TEAM_SEND_INTENT_LLM", val)
    assert get_send_intent_mode() == expected


# --------------------------------------- hard-stop veto (safe direction only) -----------------------
@pytest.mark.parametrize(
    "body",
    ["mat bhejo", "मत भेजो", "don't send", "do not send it", "please don't send this", "nahi bhejo"],
)
def test_veto_negated_send_rejects(body) -> None:
    assert send_intent_hard_stop(body) == "rejected"


@pytest.mark.parametrize(
    "body",
    [
        "bhej do",  # a clean send — NOT vetoed (LLM decides)
        "kya bhej du",  # a question — NOT vetoed (LLM decides)
        "wait mat karo, bhej do",  # negation binds the HOLD, not the send -> NOT vetoed
        "abhi mat bhejna",  # 'bhejna' not in _EXPLICIT_SEND -> not a hard-stop; LLM decides
        "socho phir batao",
    ],
)
def test_veto_no_hardstop_lets_llm_decide(body) -> None:
    assert send_intent_hard_stop(body) is None


def test_veto_never_approves() -> None:
    # The veto's type can ONLY be 'rejected' | 'hold' | None — never an approval.
    for body in ["mat bhejo", "bhej do", "STOP", "haan bhej do"]:
        assert send_intent_hard_stop(body) in ("rejected", "hold", None)


# --------------------------------------- classify_send_intent (parse + ground) ----------------------
def test_classify_grounded_approve() -> None:
    r = classify_send_intent(
        "bhej do",
        tenant_id=_TID,
        text_call=_stub({"decision": "approve", "cited_cue": "bhej do", "confidence": 0.95}),
        consent_check=_YES,
    )
    assert r is not None and r.decision == "approve" and r.grounded is True and r.confidence == 0.95


def test_classify_ungrounded_cue_marked_not_grounded() -> None:
    # cited_cue is NOT a substring of the reply -> hallucination -> grounded False.
    r = classify_send_intent(
        "bhej do",
        tenant_id=_TID,
        text_call=_stub({"decision": "approve", "cited_cue": "send everything now", "confidence": 0.99}),
        consent_check=_YES,
    )
    assert r is not None and r.grounded is False


def test_classify_fence_wrapped_json_parses() -> None:
    raw = '```json\n{"decision": "hold", "cited_cue": "kya bhej du", "confidence": 0.6}\n```'
    r = classify_send_intent("kya bhej du", tenant_id=_TID, text_call=_raw_stub(raw), consent_check=_YES)
    assert r is not None and r.decision == "hold" and r.grounded is True


@pytest.mark.parametrize("raw", ["not json at all", "", "{bad json", "[1,2,3]", '{"decision": "maybe"}'])
def test_classify_malformed_returns_none(raw) -> None:
    r = classify_send_intent("bhej do", tenant_id=_TID, text_call=_raw_stub(raw), consent_check=_YES)
    assert r is None


def test_classify_no_consent_returns_none_no_transmit() -> None:
    called: list = []

    def _never(*a, **k):  # noqa: ANN002, ANN003, ANN202
        called.append(1)
        return "{}"

    r = classify_send_intent("bhej do", tenant_id=_TID, text_call=_never, consent_check=lambda _t: False)
    assert r is None
    assert called == []  # the body was NEVER transmitted


def test_classify_empty_body_returns_none() -> None:
    assert classify_send_intent("   ", tenant_id=_TID, text_call=_stub({}), consent_check=_YES) is None


def test_classify_llm_error_returns_none() -> None:
    assert classify_send_intent("bhej do", tenant_id=_TID, text_call=_boom_stub(), consent_check=_YES) is None


# --------------------------------------- decide_send_intent_enforce (money invariant) ---------------
def _decide(body, payload):  # noqa: ANN001, ANN202
    return decide_send_intent_enforce(
        body, tenant_id=_TID, text_call=_stub(payload), consent_check=_YES
    )


def test_enforce_grounded_confident_approve_approves() -> None:
    assert _decide("bhej do", {"decision": "approve", "cited_cue": "bhej do", "confidence": 0.95}) == "approved"


def test_enforce_ungrounded_approve_holds() -> None:
    # An approve whose cue is not in the text is a hallucination -> HOLD (None), never a send.
    assert _decide("bhej do", {"decision": "approve", "cited_cue": "xyz", "confidence": 0.99}) is None


def test_enforce_low_confidence_approve_holds() -> None:
    assert _decide("bhej do", {"decision": "approve", "cited_cue": "bhej do", "confidence": 0.4}) is None


def test_enforce_hold_returns_none() -> None:
    assert _decide("kya bhej du", {"decision": "hold", "cited_cue": "kya bhej du", "confidence": 0.9}) is None


def test_enforce_grounded_reject_rejects() -> None:
    assert _decide("cancel it", {"decision": "reject", "cited_cue": "cancel it", "confidence": 0.9}) == "rejected"


def test_enforce_ungrounded_reject_holds() -> None:
    # Even a reject must be grounded; an ungrounded reject holds (still a non-approve — money-safe).
    assert _decide("cancel it", {"decision": "reject", "cited_cue": "nope", "confidence": 0.9}) is None


def test_enforce_veto_negated_send_rejects_without_llm() -> None:
    called: list = []

    def _never(*a, **k):  # noqa: ANN002, ANN003, ANN202
        called.append(1)
        return "{}"

    out = decide_send_intent_enforce("mat bhejo", tenant_id=_TID, text_call=_never, consent_check=_YES)
    assert out == "rejected"
    assert called == []  # the hard-stop veto short-circuits BEFORE the LLM


def test_enforce_llm_error_holds_never_approves() -> None:
    out = decide_send_intent_enforce("bhej do", tenant_id=_TID, text_call=_boom_stub(), consent_check=_YES)
    assert out is None  # LLM error -> HOLD, NEVER approve


def test_enforce_no_consent_holds() -> None:
    out = decide_send_intent_enforce("bhej do", tenant_id=_TID, text_call=_stub({}), consent_check=lambda _t: False)
    assert out is None


def test_enforce_a_confident_grounded_approve_is_the_only_way_to_approve() -> None:
    # Sweep the decision space: only decision='approve' + grounded + confident yields 'approved'.
    for decision in ("approve", "reject", "hold"):
        for grounded_cue in ("bhej do", "not-in-text"):
            for conf in (0.4, 0.95):
                out = _decide("bhej do", {"decision": decision, "cited_cue": grounded_cue, "confidence": conf})
                if decision == "approve" and grounded_cue == "bhej do" and conf >= 0.7:
                    assert out == "approved"
                else:
                    assert out != "approved"


# --------------------------------------- flag wiring in resolve_decision_from_reply -----------------
def test_resolve_off_is_deterministic_unchanged(monkeypatch) -> None:
    """flag=off: resolve_decision_from_reply is byte-for-byte the deterministic path — the LLM is
    NEVER consulted, even for a customer-send approval."""
    ar = pytest.importorskip("orchestrator.agent.approval_resume")
    import orchestrator.owner_inputs.send_intent as si

    monkeypatch.delenv("TEAM_SEND_INTENT_LLM", raising=False)

    # PROOF the LLM path is not touched in off mode: make it explode if called.
    def _explode(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("enforce/LLM path must NOT run in off mode")

    monkeypatch.setattr(si, "decide_send_intent_enforce", _explode)
    monkeypatch.setattr(si, "shadow_log_send_intent", _explode)

    # a clear deterministic reject resolves deterministically (no LLM).
    assert ar.resolve_decision_from_reply("नहीं", tenant_id=_TID, approval_type="campaign_send") == "rejected"
    # off PRESERVES the deterministic result byte-for-byte — INCLUDING the known pre-existing breach
    # (the keyword classifier reads "kya bhej du" = "should I send?" as an approval; VT-648 fixes this
    # ONLY under enforce, per the shadow-first gated rollout). Documented, not accepted long-term.
    assert ar.resolve_decision_from_reply("kya bhej du", tenant_id=_TID, approval_type="campaign_send") == "approved"


def test_resolve_enforce_uses_llm_for_customer_send(monkeypatch) -> None:
    """flag=enforce + a customer-send approval type: the decision comes from the LLM+veto path."""
    ar = pytest.importorskip("orchestrator.agent.approval_resume")
    import orchestrator.owner_inputs.send_intent as si

    monkeypatch.setenv("TEAM_SEND_INTENT_LLM", "enforce")
    # force the enforce path to a known result regardless of prompt (stub the classifier)
    monkeypatch.setattr(
        si, "classify_send_intent",
        lambda text, **k: si.SendIntentResult("approve", "bhej do", 0.95, True),
    )
    out = ar.resolve_decision_from_reply("bhej do", tenant_id=_TID, approval_type="campaign_send")
    assert out == "approved"


def test_resolve_enforce_does_not_touch_non_customer_send(monkeypatch) -> None:
    """flag=enforce but a NON-customer-send approval type: the LLM send-intent gate does NOT apply;
    the deterministic path handles it unchanged."""
    ar = pytest.importorskip("orchestrator.agent.approval_resume")

    monkeypatch.setenv("TEAM_SEND_INTENT_LLM", "enforce")
    # 'yes' resolves deterministically; approval_type None (non customer-send) keeps the old path.
    assert ar.resolve_decision_from_reply("yes", tenant_id=_TID, approval_type=None) == "approved"


def test_resolve_shadow_returns_deterministic(monkeypatch) -> None:
    """flag=shadow: the deterministic decision is returned unchanged; the LLM shadow is fail-soft."""
    ar = pytest.importorskip("orchestrator.agent.approval_resume")
    import orchestrator.owner_inputs.send_intent as si

    monkeypatch.setenv("TEAM_SEND_INTENT_LLM", "shadow")
    # even if the LLM would APPROVE, shadow must NOT change the deterministic decision. Use a reply the
    # deterministic path holds as None ("hmm socho" — no approve/reject signal) + an LLM stub that
    # would approve: shadow logs the disagreement but returns the deterministic None (no behavior change).
    monkeypatch.setattr(
        si, "classify_send_intent",
        lambda text, **k: si.SendIntentResult("approve", text, 0.99, True),
    )
    assert ar.resolve_decision_from_reply("hmm socho", tenant_id=_TID, approval_type="campaign_send") is None
