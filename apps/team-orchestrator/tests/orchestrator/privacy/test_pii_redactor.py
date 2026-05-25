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
