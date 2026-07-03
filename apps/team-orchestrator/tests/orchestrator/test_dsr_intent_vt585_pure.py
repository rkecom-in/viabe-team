"""VT-585 — PURE (no-DB) tests for the DSR matcher's intent-not-substring fix.

The live harness routed "Yes connect my data" to team_dsr_acknowledgment: dsr_keywords.yaml carried
bare nouns ("my data" / "मेरा डेटा") that matched connect/use/see/share-my-data contexts, so a real
owner wiring their store was falsely told their data-DELETION request was acknowledged. The fix
requires a deletion INTENT — a standalone erasure phrase, OR a deletion verb AND a data noun together
— so a bare data mention no longer fires.

Both directions are HARD asserts (this is a DPDP compliance floor): a real deletion request must NEVER
be missed, and a deletion-absent phrase must NEVER fire. Pure matcher — runs everywhere (Rule #15
fail-not-skip), the `dbos` import is guarded for the dep-less smoke only, not a DB gate.
"""

from __future__ import annotations

import pytest

pytest.importorskip("dbos")

from orchestrator.pre_filter_gate import (  # noqa: E402
    _dsr_match,
    classify_consent_intent,
    matches_opt_out_or_dsr,
)

# Real deletion / erasure requests — MUST fire (never miss one). Covers EN, Devanagari, and the
# VT-329 code-switched Hinglish (owners mix scripts mid-sentence).
MUST_FIRE = [
    # English verb + data noun
    "delete my data",
    "please delete all my data",
    "I want my data deleted",
    "erase my data",
    "remove my information",
    "wipe my data",
    "purge my data please",
    # Hinglish (English verb, latin) + data noun
    "data delete karo",
    "data delete karo refund",
    "mera data delete karo",
    # Devanagari verb + data noun
    "मेरा डेटा हटाओ",
    "मेरा डेटा हटाओ please",
    "मेरा डेटा डिलीट करो",
    "मेरा डेटा मिटाओ",
    # Code-switched (Devanagari possessive/noun + latin verb, and vice-versa)
    "मेरा data delete karo",
    "मेरा डेटा delete karo",
    "refund मेरा डेटा delete",
    # "delete/erase me" subject-erasure (Devanagari standalone)
    "मुझे हटा दो",
    "मुझे डिलीट करो",
    # Unambiguous standalone phrases
    "GDPR erasure",
    "I want data deletion for my account",
    "forget me",
    "right to be forgotten",
    "please erase my personal data",
    # Account erasure (VT-585 follow-up): account-close IS a DSR. Restores the coverage the old bare
    # "हटाओ" gave "मेरा अकाउंट हटाओ" before the tightening (verb + account noun, any order/script).
    "delete my account",
    "please delete my account",
    "मेरा अकाउंट हटाओ",
    "अकाउंट डिलीट करो",
]

# Deletion-ABSENT — MUST NOT fire (the VT-585 defect class + benign chatter). A bare data noun with
# no deletion verb is an onboarding / read / share intent, not a DSR.
MUST_NOT_FIRE = [
    "connect my data",
    "use my data",
    "yes connect my data",
    "Yes connect my data",  # the exact live-harness defect string
    "where is my data",
    "how do you store my data",
    "can you see my data",
    "my data is on shopify",
    "import my data",
    "share my data with the team",
    "मेरा डेटा कनेक्ट करो",  # "connect my data" (Devanagari) — noun present, no deletion verb
    "hello how are you",
    "hi how are you",
    "what can you do for my business",
    # Account noun WITHOUT a deletion verb — read/connect intent, must stay inert (same rule as data).
    "where is my account balance",
    "connect my account",
    "link my shopify account",
]


@pytest.mark.parametrize("body", MUST_FIRE)
def test_real_deletion_requests_fire(body: str) -> None:
    assert _dsr_match(body) is not None, f"MISSED a real deletion request: {body!r}"
    assert matches_opt_out_or_dsr(body) is True, f"MISSED via matches_opt_out_or_dsr: {body!r}"


@pytest.mark.parametrize("body", MUST_NOT_FIRE)
def test_deletion_absent_does_not_fire(body: str) -> None:
    assert _dsr_match(body) is None, f"FALSE DSR fire (deletion-absent): {body!r}"


@pytest.mark.parametrize("body", MUST_NOT_FIRE)
def test_deletion_absent_not_flagged_by_shared_gate(body: str) -> None:
    # matches_opt_out_or_dsr is the phase-gate floor (runner / journey / shopify onboarding call it):
    # a deletion-absent body must not trip the DSR leg. (Bare STOP-style opt-out is a separate list;
    # none of these are opt-outs either.)
    assert matches_opt_out_or_dsr(body) is False, f"FALSE opt-out/DSR flag: {body!r}"


def test_bare_data_noun_needs_a_deletion_verb() -> None:
    # The crux of VT-585: the noun alone is inert; adding a deletion verb flips it.
    assert _dsr_match("my data") is None
    assert _dsr_match("मेरा डेटा") is None
    assert _dsr_match("delete my data") is not None
    assert _dsr_match("मेरा डेटा हटाओ") is not None


def test_deletion_verb_without_a_data_noun_does_not_fire() -> None:
    # A deletion verb applied to something that is NOT the owner's data must not become a DSR.
    assert _dsr_match("delete this message") is None
    assert _dsr_match("remove the extra spreadsheet row") is None
    assert _dsr_match("मैसेज हटाओ") is None  # "delete the message" — no data noun


def test_opt_out_still_routes_unchanged() -> None:
    # The DSR refactor must not touch the opt-out leg (separate list, checked first in the gate).
    for body in ("STOP", "please बंद करो", "band karo", "roko ye", "UNSUBSCRIBE"):
        assert matches_opt_out_or_dsr(body) is True, f"opt-out regressed: {body!r}"


def test_consent_affirm_is_not_a_dsr() -> None:
    # "yes connect my data" (the live defect string) reads as a consent AFFIRM (VT-583) and must
    # NOT trip the DSR leg — the two must not collide.
    assert classify_consent_intent("yes connect my data") == "affirm"
    assert _dsr_match("yes connect my data") is None
    assert classify_consent_intent("haan") == "affirm"
    assert _dsr_match("haan") is None
