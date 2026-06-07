"""VT-279 — VTR/OWNER escalation route classifier (pure, deterministic, dep-less).

Runs in the dep-less smoke (keyword_match is stdlib-only). Verifies the CL-426 routing + the
fail-safe precedence: identity/authority → OWNER (never the VTR), knowledge-gap → VTR, ambiguous →
OWNER. Boundary-safe + i18n (the VT-329 lessons carry over via keyword_match).
"""

from __future__ import annotations

import pytest

from orchestrator.owner_surface.vtr_classifier import classify_escalation_route, is_confident


@pytest.mark.parametrize(
    "text",
    [
        "should I approve this campaign?",          # approval = authority
        "what discount can we offer?",              # discount (wins over the 'what' cue)
        "please process a refund for this order",   # refund
        "always message at 9am or never after 8pm?",  # always/never preference
        "block this customer from campaigns",       # exclude/block
        "daam kya rakhe?",                          # Hinglish pricing (daam) — authority
        "कीमत क्या होगी?",                            # Devanagari pricing
    ],
)
def test_authority_routes_to_owner(text):
    route, reason = classify_escalation_route(text)
    assert route == "owner", f"{text!r} → {reason}"


@pytest.mark.parametrize(
    "text",
    [
        "how does the ledger reconciliation work?",
        "what is the standard process here?",
        "unclear which onboarding step applies",
        "I'm not sure about the policy for this",
        "कैसे करें यह काम?",                          # Hinglish/Devanagari 'how to'
    ],
)
def test_knowledge_gap_routes_to_vtr(text):
    route, reason = classify_escalation_route(text)
    assert route == "vtr" and reason == "knowledge_gap", f"{text!r} → {route}/{reason}"


def test_identity_forces_owner():
    """A phone/identity present → OWNER (VT-281: the VTR must never receive raw customer identity) —
    even when the text is otherwise a 'how-to' knowledge question."""
    route, reason = classify_escalation_route("how do I handle customer +91 98765 43210's request?")
    assert route == "owner" and reason == "identity_present"


def test_owner_precedence_over_knowledge():
    """An authority signal WINS over a knowledge cue (mixed text) — fail-safe to OWNER."""
    route, _ = classify_escalation_route("I don't know how to price this new service")  # 'price' wins
    assert route == "owner"


def test_ambiguous_defaults_to_owner():
    route, reason = classify_escalation_route("the agent hit an unexpected state")
    assert route == "owner" and reason == "ambiguous_default"
    assert is_confident(reason) is False  # the hook a future LLM tie-breaker would gate on


def test_boundary_safe_no_substring_false_fire():
    """'disapprove' must NOT fire the 'approve' authority keyword (boundary-safe) — it has no other
    signal, so it falls to the ambiguous OWNER default (still owner, but via the default, proving
    'approve' didn't substring-match)."""
    _, reason = classify_escalation_route("the team expressed disapprovingly vague feedback")
    assert reason == "ambiguous_default"


def test_empty_text_is_owner():
    route, reason = classify_escalation_route(None)
    assert route == "owner" and reason == "ambiguous_default"
