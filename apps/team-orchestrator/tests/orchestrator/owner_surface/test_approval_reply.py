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
        # --- VT-615: "bhej do" send-imperatives (the resume classifier missed these; only
        #     "haan bhej do" resolved before, via _STRONG_APPROVE) ---
        ("bhej do", "approved"),
        ("bas is baar seedha bhej do", "approved"),
        ("seedha bhej do unhe", "approved"),
        ("bhejdo", "approved"),
        ("भेज दो", "approved"),
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
        # --- VT-615: NEGATED "bhej do" must still REJECT (negation wins over the new stem) ---
        ("mat bhej do", "rejected"),
        ("nahi bhejna", "rejected"),
        ("मत भेज दो", "rejected"),
        # --- ambiguous / not-a-decision -> None (Haiku fallback) ---
        ("can you change the message?", None),
        ("yes but don't make it too pushy", None),  # genuine two-clause (contrast) -> Haiku
        ("yes but don't send the discount one", None),  # Cowork's two-clause example -> Haiku
        ("maybe ok", None),  # hedge -> not authoritative -> Haiku fallback (the regression)
        ("perhaps send it later", None),  # hedge
        # --- VT-633: LATIN-script Hinglish hedges were missing — "shayad theek hai" classified
        # APPROVED off the bare "theek" verb (a hedged non-decision one step from a send) ---
        ("shayad theek hai", None),
        ("hmm dekhte hain, shayad theek rahega", None),
        ("dekhte hain", None),
        ("what is this campaign?", None),
        ("make it more festive", None),
        ("", None),
        # --- VAGUE RESUME (money-safety, official §2 2026-07-10): a "do what you were saying /
        #     continue / that same thing" back-reference whose only affirmative signal is a generic
        #     ack ("ok"/"theek") is NOT an unambiguous approval -> None (never a deterministic send).
        ("ok theek hai, chalo jo pehle bol raha tha wahi karo", None),  # the m_interruption breaker
        ("wahi karo", None),
        ("continue what you were saying", None),
        ("carry on with what you said before", None),
        ("haan, jo pehle keh raha tha", None),
        # --- an EXPLICIT send verb OVERRIDES the resume back-reference (still a real approval) ---
        ("chalo bhej do", "approved"),
        ("wahi bhej do", "approved"),  # "send that same one" — explicit send present
        # --- Cluster-1 (full-77 §2 sr_consequential_bulk 2026-07-12): a NEGATED HOLD-word +
        #     un-negated explicit send ("don't wait, send it") is NEITHER a reject (the bare `mat`
        #     used to false-DECLINE) NOR a deterministic approve (dev proved auto-approving an
        #     impatient "bhej do" AUTO-SENT a consequential batch = money_action). Money-safe -> None
        #     (brain re-confirms the send explicitly). ---
        ("jaldi karo yaar, sabko ek saath bhej do, wait mat karo", None),
        ("ruko mat karo, bhej do", None),  # `mat` binds ruko (hold), buffered from bhej
        ("wait mat karo bhejo", None),
        # --- Cluster-1 POSITIONAL SAFETY: when the negation is IMMEDIATELY adjacent to the send verb
        #     ("mat bhejo" = don't send, or "ruko mat, bhej" where comma-strip puts mat next to bhej)
        #     the money-safe reading wins -> REJECT, never a false-approve (money asymmetry) ---
        ("mat bhejo ruk jao", "rejected"),
        ("ruko, mat bhejo", "rejected"),
        ("ruko mat, bhej do", "rejected"),  # comma-strip -> `mat bhej` adjacent -> ambiguous -> safe reject
        # --- Cluster-1 x temporal-hold: "abhi mat bhejo" (don't send NOW) even with a hold-word
        #     is a DEFER, never a proceed (temporal token defeats the negated-hold carve-out) ---
        ("abhi mat bhejo, thodi der wait karo", "defer"),
        # --- Cluster-1b (sr_owner_cannot_bypass): a long free-text standing-permission ask carries
        #     an incidental `nahi`; >12 tokens routes to the reasoning layer (None), never a reject ---
        (
            "suno aapko customers ko message bhejne ke liye baar baar mujhse permission "
            "lene ki zaroorat nahi hai aage se khud decide karke bhej diya karo",
            None,
        ),
    ],
)
def test_classify_approval_reply(body, expected) -> None:
    assert classify_approval_reply(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        # T8 — bare "proceed / do what you were saying / continue" cues (the §2 breaker inputs)
        "chalo jo pehle bol raha tha wahi karo",
        "ok theek hai, chalo jo pehle bol raha tha wahi karo",
        "haan wahi, continue",
        "wahin se karo",
        "carry on with what you were saying",
        "resume",
    ],
)
def test_is_resume_cue_true(body) -> None:
    from orchestrator.owner_inputs.approval_reply import is_resume_cue

    assert is_resume_cue(body) is True
    # money-safety invariant: a resume cue is NEVER a resolvable approval decision (T5)
    assert classify_approval_reply(body) is None


@pytest.mark.parametrize(
    "body",
    [
        # explicit send verb -> an APPROVAL, not a resume cue (T5 override)
        "chalo bhej do",
        "haan bhejo",
        "yes send it",
        # negation / reject -> a stop, not a resume
        "no",
        "nahi, mat bhejo",
        "cancel",
        # a question -> not a decision, not a resume
        "resume kya karu?",
        # plain unrelated / new topic while an approval is pending -> not a resume cue
        "what's my top product",
        "haan",
        "ok",
    ],
)
def test_is_resume_cue_false(body) -> None:
    from orchestrator.owner_inputs.approval_reply import is_resume_cue

    assert is_resume_cue(body) is False


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
    ("body", "expected"),
    [
        # --- T17 temporal HOLD → defer (pause the send, keep the draft; never sends) ---
        ("ruk jao, abhi mat bhejna", "defer"),  # the measured sr_no_actual_send breaker verbatim
        ("abhi mat bhejo", "defer"),
        ("abhi nahi", "defer"),
        ("don't send it now", "defer"),
        ("not yet", "defer"),
        ("filhal mat bhejo", "defer"),
        # --- still REJECT: bare negation (no temporal token) ---
        ("mat bhejo", "rejected"),
        ("nahi bhejna", "rejected"),
        # --- still REJECT: explicit reject keyword wins over the temporal read ---
        ("no, cancel it now", "rejected"),
        ("stop it now", "rejected"),
        # --- still REJECT: finality defeats the temporal read ---
        ("don't send now or ever", "rejected"),
        ("kabhi nahi bhejna, abhi toh bilkul nahi", "rejected"),
        ("never send this now", "rejected"),
        # --- contradictory "no, send it now" → defer → RE-ASK (safer than either misread) ---
        ("no, send it now", "defer"),
    ],
)
def test_classify_temporal_hold(body, expected) -> None:
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
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from uuid import uuid4

    collapse = pytest.importorskip("orchestrator.collapse")  # dep-less: skip
    _build_approval_request = collapse._build_approval_request

    now = datetime.now(UTC)
    plan = SimpleNamespace(
        target_cohort=SimpleNamespace(cohort_label="45-day lapsed", cohort_size=87),
        expected_arrr=SimpleNamespace(low_paise=1_500_000, high_paise=3_000_000),
        # VT-594 (post-review restructure): _build_approval_request now also builds
        # a chat_summary body, which reads campaign_window for the window dates.
        campaign_window=SimpleNamespace(
            start=now + timedelta(hours=1), end=now + timedelta(days=7)
        ),
    )
    req = _build_approval_request(plan=plan, campaign_id=uuid4(), tenant_id=uuid4())
    params = req["template_params"]
    assert params != {}  # NOT the old blank
    # Delta-review Defect 1: the label passes the redactor first — a no-op on
    # this legitimate categorical label (fail-soft pattern-only, no DB here).
    assert params["1"] == "45-day lapsed"
    assert params["2"] == "recovery"
    assert params["3"] == "15,000–30,000"  # paise -> ₹ range
