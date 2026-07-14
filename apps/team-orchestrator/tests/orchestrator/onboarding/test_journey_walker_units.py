"""R9 — pure-unit coverage for the onboarding walker's deterministic helpers (no DB / no LLM).

These pin the string-shaping helpers added in the R9 walker batch so they are verifiable without
a live Postgres (the DB-backed integrated behaviours live in ``test_journey.py``):

  - ``_completion_recap`` / ``_completion_message`` — one-line recap of captured fields at
    completion (item 5), with a byte-identical fallback to today's copy on empty answers;
  - ``_prefix_defer_ack`` / ``_DEFER_ACK`` — the skip defer-ack (item 1);
  - ``_is_kickoff_token`` — a re-tapped "Complete Setup" mid-journey is a NON-answer (item 6).
"""

from __future__ import annotations

import pytest

# journey imports psycopg (tenant_connection / Jsonb) at module import; skip the dep-less smoke.
pytest.importorskip("psycopg")

from orchestrator.onboarding import journey as j  # noqa: E402

# The pre-R9 completion copy — the recap fallback must reproduce it BYTE-for-BYTE on empty answers.
_LEGACY_EN = "Thanks — that's everything we need to get started. We're setting up your assistant now."


# --- item 5: completion recap ---------------------------------------------------------------------


@pytest.mark.parametrize("answers", [None, {}, {"__flow__": "profile_previewed"}, {"operating_hours": "9-9"}])
def test_completion_recap_empty_or_no_recap_field_is_blank(answers):
    # No recap-worthy field (or empty) → ('', '') so the completion falls back to today's exact copy.
    assert j._completion_recap(answers) == ("", "")


def test_completion_message_empty_is_byte_identical_to_legacy():
    assert j._completion_message()["reply_en"] == _LEGACY_EN
    assert j._completion_message({})["reply_en"] == _LEGACY_EN
    assert j._completion_message({"operating_hours": "9-9"})["reply_en"] == _LEGACY_EN


def test_completion_recap_names_captured_fields():
    en, hi = j._completion_recap({"business_type": "leather bags", "city": "Pune"})
    assert "leather bags" in en and "Pune" in en
    assert "leather bags" in hi and "Pune" in hi
    assert en.startswith(" Here's what I've noted:")


def test_completion_message_carries_recap_and_keeps_closer():
    msg = j._completion_message({"business_type": "leather bags", "city": "Pune"})
    assert "leather bags" in msg["reply_en"] and "Pune" in msg["reply_en"]
    assert msg["reply_en"].startswith("Thanks — that's everything we need to get started.")
    assert msg["reply_en"].endswith("We're setting up your assistant now.")
    assert msg["done"] is True


def test_completion_recap_collapses_business_type_and_category_to_one():
    # Only ONE business line even if both business_type and category are present.
    en, _ = j._completion_recap({"business_type": "sweets", "category": "Sweet shop", "city": "Pune"})
    assert en.count("sweets") == 1
    assert "Sweet shop" not in en, "category is suppressed when business_type already recaps the business"
    assert "Pune" in en


def test_completion_recap_caps_at_three_fields():
    en, _ = j._completion_recap(
        {"business_type": "bags", "city": "Pune", "about": "we sell bags", "website": "x.in"}
    )
    # business_type + city + about = 3; website is dropped (recap stays one short line).
    assert en.count(",") == 2


def test_completion_recap_ignores_non_string_and_blank_values():
    en, _ = j._completion_recap({"business_type": "  ", "city": None, "about": "we sell bags"})
    assert "we sell bags" in en
    assert en.count(",") == 0, "blank/None fields contribute nothing to the recap"


# --- VT-639: GST nature-of-business deflection detector -------------------------------------------


def test_gst_nature_deflection_detects_pure_tax_codes():
    # A pure GST 'nature of business' tax-activity code offered as the business type is a DEFLECTION,
    # not a description — deterministic-walker parity with the turn-brain's 'never present GST nature
    # values as business-type' rule. The scenario's own phrasing must trip it.
    assert j._is_gst_nature_deflection("Supplier of Services")
    assert j._is_gst_nature_deflection(
        "actually humara GST mein 'Supplier of Services' likha hai, wahi bata dete hain"
    )
    assert j._is_gst_nature_deflection("we're a supplier of goods")
    assert j._is_gst_nature_deflection("Works Contract")
    assert j._is_gst_nature_deflection("Input Service Distributor")


def test_gst_nature_deflection_ignores_real_descriptions():
    # A genuine description — even a rich NON-taxonomy one (VT-601 salvages these into 'about') — is
    # NOT a deflection. Narrow by design: only pure tax-activity phrases with no sector meaning.
    assert not j._is_gst_nature_deflection("hum toh bas mithai aur namkeen banate aur bechte hain")
    assert not j._is_gst_nature_deflection("Probe Traders, a hardware shop in Pune")
    assert not j._is_gst_nature_deflection("leather bags")
    # mentions GST but carries a real description → NOT a deflection (no pure tax-code phrase present)
    assert not j._is_gst_nature_deflection("we sell sweets, we're GST registered")
    # 'retail'/'wholesale' ARE usable business sectors, deliberately NOT deflection phrases
    assert not j._is_gst_nature_deflection("retail business")
    assert not j._is_gst_nature_deflection("")


def test_reprompt_gst_nature_asks_what_they_sell_both_locales():
    r = j._reprompt_gst_nature({"field": "business_type", "draft_value": "sweets"})
    assert r["done"] is False
    assert r["re_present"] is True
    assert r["reply_en"] and r["reply_hi"]
    # must NOT echo a rejection of the draft — it's a deflection, not a 'no'
    assert "sweets" not in r["reply_en"].lower()


# --- item 1: skip defer-ack -----------------------------------------------------------------------


def test_defer_ack_copy_present_both_locales():
    assert j._DEFER_ACK["en"] and j._DEFER_ACK["hi"]


def test_prefix_defer_ack_prepends_both_locales():
    out = j._prefix_defer_ack({"reply_en": "What are your hours?", "reply_hi": "समय?", "done": False})
    assert out["reply_en"].startswith(j._DEFER_ACK["en"])
    assert "What are your hours?" in out["reply_en"]
    assert out["reply_hi"].startswith(j._DEFER_ACK["hi"])
    assert out["done"] is False


def test_prefix_defer_ack_empty_hi_degrades_to_ack_only():
    out = j._prefix_defer_ack({"reply_en": "Q?", "reply_hi": "", "done": False})
    assert out["reply_hi"] == j._DEFER_ACK["hi"], "empty reply_hi → the ack alone, stripped"


# --- item 6: kickoff-token re-tap is a NON-answer --------------------------------------------------


@pytest.mark.parametrize("body", ["Complete Setup", "complete setup", "  COMPLETE SETUP  "])
def test_is_kickoff_token_matches_the_exact_button_body(body):
    assert j._is_kickoff_token(body) is True


@pytest.mark.parametrize(
    "body",
    ["complete setup please", "let's complete the setup", "setup", "9am to 9pm", "", "haan"],
)
def test_is_kickoff_token_rejects_non_exact_bodies(body):
    assert j._is_kickoff_token(body) is False
