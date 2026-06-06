"""VT-83 — weekly-approval reply classifier + template-params regression.

The classifier tests are pure (no DB / no Anthropic). The resolve + _build_approval_request
imports are LOCAL (inside the tests) so dep-less smoke collection never pulls heavy deps
(the depless-smoke-import-trap lesson).
"""

from __future__ import annotations

import pytest

# Dep-less CI 'test' job (uv --no-project): owner_inputs/__init__ -> writer -> anthropic.
# Skip the module cleanly when anthropic is absent; the full real-PG suite runs it.
pytest.importorskip("anthropic")

from orchestrator.owner_inputs.approval_reply import classify_approval_reply


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # --- clear APPROVE (EN) ---
        ("yes", "approved"),
        ("approve", "approved"),
        ("ok send it", "approved"),
        ("go ahead, send", "approved"),
        ("sure", "approved"),
        # --- clear APPROVE (HI / Hinglish) ---
        ("हाँ", "approved"),
        ("जी भेजो", "approved"),
        ("haan", "approved"),
        ("theek hai", "approved"),
        ("bhejo", "approved"),
        # --- clear REJECT (EN) ---
        ("no", "rejected"),
        ("reject", "rejected"),
        ("skip this week", "rejected"),
        ("stop", "rejected"),
        # --- REJECT via negated send-verb (the Pillar-7 keystone) ---
        ("don't send", "rejected"),
        ("do not send it", "rejected"),
        ("please don't send this", "rejected"),
        # --- REJECT via NEGATED approve-word (the #345 bounce — must be deterministic) ---
        ("do not approve", "rejected"),
        ("don't approve", "rejected"),
        ("won't approve", "rejected"),
        ("not ok", "rejected"),
        ("no don't approve it", "rejected"),
        # --- REJECT (HI / Hinglish) ---
        ("नहीं", "rejected"),
        ("मत भेजो", "rejected"),
        ("nahi", "rejected"),
        ("mat bhejo", "rejected"),
        # --- ambiguous / not-a-decision -> None (Haiku fallback) ---
        ("can you change the message?", None),
        ("yes but don't make it too pushy", None),  # genuine two-clause (contrast) -> Haiku
        ("yes but don't send the discount one", None),  # Cowork's two-clause example -> Haiku
        ("maybe ok", None),  # hedge -> not authoritative -> Haiku fallback (the regression)
        ("perhaps send it later", None),  # hedge
        ("what is this campaign?", None),
        ("make it more festive", None),
        ("", None),
    ],
)
def test_classify_approval_reply(body, expected) -> None:
    assert classify_approval_reply(body) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # --- DEFER (VT-334) — EN ---
        ("later", "defer"),
        ("let's do it later", "defer"),
        ("next week", "defer"),  # the next+week BIGRAM (not bare "next")
        # --- DEFER — HI / Hinglish (incl. the nukta + non-nukta हफ़्ते/हफ्ते) ---
        ("baad mein", "defer"),
        ("बाद में", "defer"),
        ("agle hafte pls", "defer"),
        ("अगले हफ़्ते", "defer"),
        ("अगले हफ्ते", "defer"),  # नुक़ता-less variant
        # --- precedence: reject > defer (any negation still wins) ---
        ("no, next week", "rejected"),
        ("skip, do it later", "rejected"),  # reject keyword beats defer
        # --- precedence: defer > approve ("approve the idea, but later") ---
        ("ok but later", "defer"),
        ("haan but next week", "defer"),
    ],
)
def test_classify_defer(body, expected) -> None:
    assert classify_approval_reply(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        # Bare "next"/"अगले" must NOT defer on an APPROVING reply (Cowork #377 bounce) — defer
        # needs the next+WEEK bigram, so these stay 'approved'.
        "approve the next campaign",
        "yes, send the next one",
        "अगले campaign ko approve karo",
        "अगला wala bhejo",
    ],
)
def test_bare_next_does_not_defer_on_approval(body) -> None:
    assert classify_approval_reply(body) == "approved"


def test_defer_no_negation_handling_known_limitation() -> None:
    """VT-334 ACCEPTED limitation (Cowork 20260606T103500Z): defer keywords get no negation
    handling, so a send-now phrasing that merely CONTAINS 'later' classifies as defer. This is
    the FAIL-SAFE direction — the campaign is delayed + the owner re-asked, never an unconsented
    send. Recorded here deliberately; VT-329 may add full negation treatment for defer."""
    assert classify_approval_reply("send now instead of later") == "defer"


def test_resolve_uses_deterministic_first_no_llm() -> None:
    """A CLEAR reply resolves deterministically — the Haiku classify_fn is NOT called."""
    ar = pytest.importorskip("orchestrator.agent.approval_resume")  # dep-less: skip
    resolve_decision_from_reply = ar.resolve_decision_from_reply

    calls: list[str] = []

    def _never(text: str):  # noqa: ANN202
        calls.append(text)
        raise AssertionError("classify_fn must not be called on a clear reply")

    assert resolve_decision_from_reply("नहीं", tenant_id="t", classify_fn=_never) == "rejected"
    assert resolve_decision_from_reply("yes send it", tenant_id="t", classify_fn=_never) == "approved"
    assert calls == []


def test_resolve_ambiguous_falls_through_to_haiku() -> None:
    """An ambiguous reply falls through to the injected classifier (Haiku in prod)."""
    from types import SimpleNamespace

    ar = pytest.importorskip("orchestrator.agent.approval_resume")  # dep-less: skip
    resolve_decision_from_reply = ar.resolve_decision_from_reply

    seen: list[str] = []

    def _stub(text: str):  # noqa: ANN202
        seen.append(text)
        return SimpleNamespace(classification="approval", confidence=0.9)

    out = resolve_decision_from_reply("make it more festive", tenant_id="t", classify_fn=_stub)
    assert out == "approved"
    assert seen == ["make it more festive"]  # the ambiguous text reached the fallback


def test_build_approval_request_populates_template_params() -> None:
    """Gap #1 regression: the params are no longer EMPTY (the blank-message bug)."""
    from types import SimpleNamespace
    from uuid import uuid4

    collapse = pytest.importorskip("orchestrator.collapse")  # dep-less: skip
    _build_approval_request = collapse._build_approval_request

    plan = SimpleNamespace(
        target_cohort=SimpleNamespace(cohort_label="60-90 day dormant", cohort_size=87),
        expected_arrr=SimpleNamespace(low_paise=1_500_000, high_paise=3_000_000),
    )
    req = _build_approval_request(plan=plan, campaign_id=uuid4())
    params = req["template_params"]
    assert params != {}  # NOT the old blank
    assert params["1"] == "60-90 day dormant"
    assert params["2"] == "recovery"
    assert params["3"] == "15,000–30,000"  # paise -> ₹ range
