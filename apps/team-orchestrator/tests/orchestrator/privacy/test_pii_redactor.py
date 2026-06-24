"""Tests for the canonical PII redactor (VT-104).

All pure. The redactor is a string/dict transformer with no I/O — the
canary covers the on-the-wire proofs (Anthropic + Supabase).
"""

from __future__ import annotations

import pytest

from orchestrator.privacy.pii_redactor import (
    DEFAULT_MAX_DEPTH,
    redact,
)


@pytest.fixture(autouse=True)
def _phone_salt(monkeypatch):
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "test-salt-vt104")


# ---------------------------------------------------------------------------
# 1. Phone — E.164 + 10-digit Indian
# ---------------------------------------------------------------------------

def test_phone_e164_in_raw_string_redacted() -> None:
    out = redact("call +919876543210 anytime")
    assert "9876543210" not in out
    assert "phone_tok_" in out


def test_phone_indian_10_digit_at_word_boundary_redacted() -> None:
    out = redact("call 9876543210 plz")
    assert "9876543210" not in out
    assert "phone_tok_" in out


def test_phone_indian_5_5_split_redacted() -> None:
    out = redact("call 98765 43210 plz")
    assert "9876543210" not in out
    assert "98765 43210" not in out
    assert "phone_tok_" in out


def test_phone_at_named_key_uses_vt101_legacy_format() -> None:
    """VT-101 byte-identical: phone at the named key gets phone_tok_."""
    out = redact({"phone": "+919876543210"})
    assert out["phone"].startswith("phone_tok_")
    assert "+919876543210" not in out["phone"]


# ---------------------------------------------------------------------------
# 2. Email
# ---------------------------------------------------------------------------

def test_email_in_raw_string_redacted_as_hash_marker() -> None:
    out = redact("ping fazal@viabe.ai if needed")
    assert "fazal@viabe.ai" not in out
    assert "<email:hash:" in out


def test_email_at_named_key_uses_vt101_legacy_format() -> None:
    out = redact({"email": "fazal@viabe.ai"})
    assert out["email"] == "<redacted:email>"


# ---------------------------------------------------------------------------
# 3. PAN / Aadhaar / IFSC / GST
# ---------------------------------------------------------------------------

def test_pan_redacted() -> None:
    out = redact("My PAN ABCDE1234F please")
    assert "ABCDE1234F" not in out
    assert "<pan:redacted>" in out


def test_aadhaar_redacted() -> None:
    out = redact("aadhaar 123412341234 ok")
    assert "123412341234" not in out
    assert "<aadhaar:redacted>" in out


def test_ifsc_redacted() -> None:
    out = redact("send to IFSC HDFC0001234 here")
    assert "HDFC0001234" not in out
    assert "<ifsc:redacted>" in out


def test_gst_redacted() -> None:
    out = redact("GST 22AAAAA0000A1Z5 found")
    assert "22AAAAA0000A1Z5" not in out
    assert "<gst:redacted>" in out


# ---------------------------------------------------------------------------
# 4. Credit card — Luhn-validated
# ---------------------------------------------------------------------------

def test_cc_valid_luhn_redacted() -> None:
    # 4532015112830366 — known-valid Luhn test number.
    out = redact("card 4532015112830366 ok")
    assert "4532015112830366" not in out
    assert "<cc:redacted>" in out


def test_cc_invalid_luhn_not_redacted() -> None:
    # 1234567890123456 fails Luhn — sequential order-id-style number.
    out = redact("order 1234567890123456 here")
    assert "1234567890123456" in out
    assert "<cc:redacted>" not in out


# ---------------------------------------------------------------------------
# 5. Long body
# ---------------------------------------------------------------------------

def test_long_body_above_threshold_hashed() -> None:
    out = redact("x" * 250)
    assert out.startswith("<body:hash:")
    assert "x" * 250 not in out


def test_short_body_below_threshold_unchanged() -> None:
    raw = "x" * 150
    out = redact(raw)
    assert out == raw


# ---------------------------------------------------------------------------
# 6. Customer name — tenant registry
# ---------------------------------------------------------------------------

def test_customer_name_registry_match_redacted() -> None:
    registry = {"Rajesh Kumar"}.__contains__
    out = redact({"customer_name": "Rajesh Kumar"}, name_registry=registry)
    assert out["customer_name"] == "<customer_name>"


def test_customer_name_registry_miss_uses_redacted_len_token() -> None:
    """Phase-1 false-negative path: unknown name still hits the named-key
    branch + emits the length-redacted token (VT-101 behaviour)."""
    registry = {"Rajesh Kumar"}.__contains__
    out = redact({"customer_name": "Random Person"}, name_registry=registry)
    assert out["customer_name"].startswith("<redacted:customer_name:")


def test_customer_name_in_raw_text_scan_redacts_registered_match() -> None:
    registry = {"Rajesh Kumar"}.__contains__
    out = redact("Hi Rajesh Kumar are you there?", name_registry=registry)
    assert "Rajesh Kumar" not in out
    assert "<customer_name>" in out


def test_customer_name_in_raw_text_scan_keeps_unregistered_match() -> None:
    """Brief Phase-1 acknowledged false negative: unregistered name in a
    raw string is NOT redacted (regex-only mode cannot infer)."""
    registry = {"Rajesh Kumar"}.__contains__
    out = redact("Hi Random Person are you there?", name_registry=registry)
    assert "Random Person" in out


# ---------------------------------------------------------------------------
# 6b. VT-412 — registry scan now catches single-token + 3+-token names.
# decision_rationale agent think-text is a raw string; before VT-412 the scan
# only formed consecutive 2-grams, so a mononym customer name and any 3+-token
# registered name survived into a VTR's run replay. These prove the close.
# The real registry predicate is case-folded exact-match (customer_registry.
# make_name_registry); model it with a casefold-aware membership test.
# ---------------------------------------------------------------------------

def _casefold_registry(*names: str):
    folded = {n.casefold() for n in names}
    return lambda text: text.casefold() in folded


def test_single_token_registered_name_redacted_in_raw_text() -> None:
    """VT-412 core: a MONONYM customer name in agent think-text is redacted.

    'Ramesh' is a single token — the pre-VT-412 2-gram-only scan never tested
    it, so it reached the VTR run-replay surface. It must now be caught.
    """
    registry = _casefold_registry("Ramesh")
    out = redact(
        "Owner asked to reschedule the delivery for Ramesh on Friday",
        name_registry=registry,
    )
    assert "Ramesh" not in out
    assert "<customer_name>" in out


def test_three_token_registered_name_redacted_in_raw_text() -> None:
    """VT-412: a 3-token registered name is matched WHOLE (longest-window-first),
    not as a stray sub-token. The old consecutive-bigram scan matched neither
    of its 2-grams against the 3-token registry entry."""
    registry = _casefold_registry("Mohammed Abdul Rahman")
    out = redact(
        "Routing the order for Mohammed Abdul Rahman to the kitchen now",
        name_registry=registry,
    )
    assert "Mohammed Abdul Rahman" not in out
    assert "Mohammed" not in out and "Rahman" not in out
    assert "<customer_name>" in out


def test_registry_scan_case_insensitive_single_token() -> None:
    """The predicate is case-folded; a lowercased mononym still matches."""
    registry = _casefold_registry("Priya")
    out = redact("note: priya wants the green one", name_registry=registry)
    assert "priya" not in out
    assert "<customer_name>" in out


def test_registry_scan_preserves_bracketing_punctuation_single_token() -> None:
    """A single-token name in parentheses keeps the brackets so the sentence
    still reads (the punctuation-preservation contract, extended to 1-grams)."""
    registry = _casefold_registry("Suresh")
    out = redact("escalation (Suresh) pending", name_registry=registry)
    assert "Suresh" not in out
    assert "(<customer_name>)" in out


def test_possessive_single_token_name_redacted() -> None:
    """VT-412: 'Ramesh's' (possessive) leaks the bare name 'Ramesh' — strip the
    clitic before the registry test, re-attach it so the sentence reads."""
    registry = _casefold_registry("Ramesh")
    out = redact(
        "Owner asked to reschedule Ramesh's delivery to Friday", name_registry=registry
    )
    assert "Ramesh" not in out
    assert "<customer_name>'s" in out


def test_registry_scan_no_registry_no_change() -> None:
    """No registry → no name scan at all (the cheap no-tenant-context path is
    unchanged; only pattern redaction runs)."""
    out = redact("Ramesh ordered two coffees", name_registry=None)
    assert out == "Ramesh ordered two coffees"


def test_unregistered_single_token_survives() -> None:
    """A single token that is NOT a registered customer name is never inferred
    away (no false-positive widening from the new 1-gram window)."""
    registry = _casefold_registry("Ramesh")
    out = redact("the manager approved it", name_registry=registry)
    assert out == "the manager approved it"


# ---------------------------------------------------------------------------
# 7. Recursive structures + max depth
# ---------------------------------------------------------------------------

def test_deeply_nested_dict_redacts_at_all_levels() -> None:
    nested = {"l1": {"l2": {"l3": {"l4": {"phone": "+919876543210"}}}}}
    out = redact(nested)
    # depth 4 < DEFAULT_MAX_DEPTH=5; should still redact.
    deep = out["l1"]["l2"]["l3"]["l4"]["phone"]
    assert deep.startswith("phone_tok_")


def test_depth_beyond_max_returns_truncated_marker() -> None:
    """At max_depth, the value RECURSED INTO is processed at depth+1.

    With ``max_depth=5`` and 6 layers of nesting, the leaf at depth=6
    returns the truncated marker; the dict containing it at depth=5
    is still walked (boundary inclusive).
    """
    deep = {"a": {"b": {"c": {"d": {"e": {"f": "phone:+919876543210"}}}}}}
    out = redact(deep)
    leaf = out["a"]["b"]["c"]["d"]["e"]["f"]
    assert leaf == "<redaction_truncated>"


def test_list_recursion_preserves_length() -> None:
    inputs = ["+919876543210", "plain", {"phone": "+918765432109"}]
    out = redact(inputs)
    assert len(out) == 3
    assert "phone_tok_" in out[0]
    assert out[1] == "plain"
    assert out[2]["phone"].startswith("phone_tok_")


def test_tuple_recursion_preserves_type() -> None:
    out = redact(("+919876543210", "x"))
    assert isinstance(out, tuple)
    assert out[1] == "x"


# ---------------------------------------------------------------------------
# 8. Idempotency
# ---------------------------------------------------------------------------

def test_idempotency_complex_payload() -> None:
    x = {
        "phone": "+919876543210",
        "body": "Hi I want to cancel my subscription",
        "customer_name": "Rajesh Kumar",
        "msg": "reach me at fazal@viabe.ai or 9876543210",
        "nested": {
            "pan": "ABCDE1234F",
            "card": "4532015112830366",
        },
    }
    once = redact(x)
    twice = redact(once)
    assert once == twice


def test_idempotency_long_body() -> None:
    raw = "y" * 250
    once = redact(raw)
    twice = redact(once)
    assert once == twice


# ---------------------------------------------------------------------------
# 9. VT-101 / VT-102 regression — byte-identical named-key output
# ---------------------------------------------------------------------------

def test_vt101_canary_payload_byte_identical_redaction() -> None:
    """VT-101 / VT-102 canary input → expected token shape preserved."""
    vt101_input = {
        "k": "Customer +919876543210 cancellation",
        "customer_name": "Rajesh Kumar",
        "body": "Hi I want to cancel",
    }
    out = redact(vt101_input)
    assert out["k"].startswith("Customer phone_tok_")
    assert out["customer_name"].startswith("<redacted:customer_name:")
    assert out["body"].startswith("body_tok_")
    # Verify NO raw PII survived.
    import json

    blob = json.dumps(out)
    assert "919876543210" not in blob
    assert "Rajesh Kumar" not in blob
    assert "Hi I want to cancel" not in blob


# ---------------------------------------------------------------------------
# 10. DEFAULT_MAX_DEPTH constant
# ---------------------------------------------------------------------------

def test_default_max_depth_is_five() -> None:
    assert DEFAULT_MAX_DEPTH == 5


def test_aadhaar_pattern_spares_uuid_segments():
    """VT-369 CI flake: a uuid4 whose final 12-hex segment is all-numeric must NOT be
    Aadhaar-redacted (id-bearing payloads were corrupted ~1-in-285 uuids); a real
    standalone Aadhaar still is."""
    uuid_like = "710363ea-7d30-4f02-a660-214698525976"
    assert redact(uuid_like) == uuid_like
    assert "<aadhaar:redacted>" in redact("my aadhaar is 1234 5678 9012")
    assert "<aadhaar:redacted>" in redact("aadhaar 123456789012 here")
