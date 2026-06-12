"""VT-377 Gap-6 — the Devanagari proper-noun bridge for the business-plan citation
validator (no DB). Falsifiable BOTH directions per the Cowork ruling's conditions:

  - a GROUNDED Devanagari brand passes when its Latin form is in the bundle;
  - a FABRICATED lexicon brand flags when it is NOT;
  - legitimate NON-lexicon Devanagari prose passes untouched;
  - Devanagari numerals (०-९) ground exactly like ASCII;
  - and the residual is the open-world non-lexicon proper noun (documented in
    ``schema``'s docstring as the explicit BOUNDARY — asserted here so the bridge's
    limit is itself a test, not a surprise).

The lexicon is the versioned ``config/devanagari_brand_lexicon.yaml`` (condition 1) —
these tests drive it through the real loader, not a mock, so a lexicon edit that
breaks a mapping fails here.

Import guards + fictional fixtures mirror ``test_schema.py`` (CL-390 — no third-party
PII; the dep-less smoke job skips on the psycopg/dbos import).
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")
pytest.importorskip("yaml")

from orchestrator.business_plan.schema import (  # noqa: E402 — after dependency skip guards
    _brand_lexicon,
    _canon_number,
    validate_plan,
)

# --- Fixtures ---------------------------------------------------------------


def _summary_hi(text_hi: str, **overrides) -> dict:
    """A summary whose EN text is grounding-clean; the Devanagari claim under test
    rides ``text_hi`` (the field the bridge scans)."""
    base = {
        "text": "Rated 4.2.",
        "text_hi": text_hi,
        "cited_facts": ["F1"],
        "headline_metrics": {},
    }
    base.update(overrides)
    return base


def _bundle(*, value: str = "4.2 on Swiggy") -> dict:
    """A one-fact frozen bundle whose VALUE string carries the literals a plan may
    claim (the grounding walk extracts both the words and the embedded numerals)."""
    return {"F1": {"key": "rating", "value": value, "source": "platform_listings"}}


# --- the lexicon itself -----------------------------------------------------


def test_lexicon_loads_from_versioned_config():
    """Condition 1: the bridge is driven by the versioned yaml, not hardcoded
    constants — the real loader returns the closed brand set."""
    lex = _brand_lexicon()
    assert lex, "lexicon must load from config/devanagari_brand_lexicon.yaml"
    # Devanagari spelling -> canonical Latin; both Zomato variants collapse to one form.
    assert lex["स्विगी"] == "Swiggy"
    assert lex["ज़ोमैटो"] == "Zomato"
    assert lex["जोमैटो"] == "Zomato"
    assert lex["गूगल"] == "Google"


# --- direction 1: grounded Devanagari brand PASSES --------------------------


def test_grounded_devanagari_brand_passes():
    # Zomato IS in the bundle value -> the Devanagari ज़ोमैटो is grounded, no violation.
    summary = _summary_hi("ज़ोमैटो पर आपकी रेटिंग अच्छी है।")
    assert validate_plan(summary, [], _bundle(value="4.2 on Zomato")) == []


def test_grounded_devanagari_brand_variant_passes():
    # the no-nukta जोमैटो variant must also resolve to Zomato and ground.
    summary = _summary_hi("जोमैटो पर रेटिंग अच्छी है।")
    assert validate_plan(summary, [], _bundle(value="4.2 on Zomato")) == []


# --- direction 2: fabricated lexicon brand FLAGS ----------------------------


def test_fabricated_devanagari_brand_flags():
    # Zomato is a KNOWN brand but NOT in this bundle (Swiggy is) -> the realistic
    # attack: a fabricated platform smuggled in Devanagari. Must be a violation.
    summary = _summary_hi("ज़ोमैटो पर अपनी लिस्टिंग पूरी करें।")
    violations = validate_plan(summary, [], _bundle(value="4.2 on Swiggy"))
    assert any("ज़ोमैटो" in v and "summary.text_hi" in v for v in violations)


def test_fabricated_devanagari_brand_in_owner_action_hi_flags():
    """The owner_action_hi prompt is DELIVERED to the owner — a fabricated brand
    must not ride it past the gate (the VTR-smuggle path, Devanagari edition)."""
    from uuid import uuid4

    item = {
        "item_id": str(uuid4()),
        "seq": 1,
        "month": 1,
        "objective": "Reply to reviews",
        "why": "Your rating is 4.2 today.",
        "cited_facts": ["F1"],
        "owning_agent": "reputation",
        "owner_action_needed": True,
        "owner_action": "Post on the platform",
        "owner_action_hi": "इंस्टाग्राम पर पोस्ट करें",  # Instagram NOT in bundle
        "status": "proposed",
        "provenance": {
            "origin": "llm_v1",
            "editor": None,
            "prev_version": None,
            "diff_from_prev": None,
        },
    }
    summary = _summary_hi("रेटिंग 4.2 है।")
    violations = validate_plan(summary, [item], _bundle(value="4.2 on Swiggy"))
    assert any("इंस्टाग्राम" in v and "owner_action_hi" in v for v in violations)


# --- legitimate non-lexicon Devanagari prose is UNTOUCHED --------------------


def test_legitimate_devanagari_prose_passes():
    # Ordinary Hindi prose: place name + common words, none in the lexicon -> left alone.
    summary = _summary_hi("पुणे में आपके रेस्टोरेंट की बिक्री मई में घटी।")
    assert validate_plan(summary, [], _bundle()) == []


def test_real_delivery_style_hindi_prose_passes():
    """Regression guard mirroring the live delivery/seams fixtures: ordinary Hindi
    sentences with grounded ASCII numbers and NO lexicon brand must stay clean."""
    summary = _summary_hi("मई में बिक्री 12% घटी; रिव्यू 4.2 स्टार पर स्थिर रहे।")
    # 12 and 4.2 must be bundle literals or this fails for the RIGHT reason; ground them.
    bundle = _bundle(value="4.2 stars, 12% change on Swiggy")
    assert validate_plan(summary, [], bundle) == []


# --- Devanagari numerals ground like ASCII ----------------------------------


def test_devanagari_numeral_canon_matches_ascii():
    # the canonical form is script-independent: ४.९ == 4.9, ३५० == 350.
    assert _canon_number("४.९") == _canon_number("4.9") == "4.9"
    assert _canon_number("३५०") == _canon_number("350") == "350"


def test_devanagari_numeral_grounds_when_in_bundle():
    # bundle says 4.9; the plan states it in Devanagari (४.९) -> grounded, no violation.
    # EN text carries no number so the EN side can't fail for an unrelated reason.
    summary = _summary_hi("रेटिंग ४.९ है।", text="Your rating is strong.")
    assert validate_plan(summary, [], _bundle(value="4.9 rating")) == []


def test_devanagari_numeral_flags_when_not_in_bundle():
    # bundle says 4.2; a Devanagari ४.९ is a fabricated number -> violation, same as ASCII.
    summary = _summary_hi("रेटिंग ४.९ हो जाएगी।", text="Your rating is strong.")
    violations = validate_plan(summary, [], _bundle(value="4.2 rating"))
    assert any("४.९" in v and "summary.text_hi" in v for v in violations)


# --- the explicit residual (condition 3, as a test) -------------------------


def test_residual_arbitrary_devanagari_proper_noun_not_caught():
    """BOUNDARY (documented in schema.py): an arbitrary Devanagari proper noun NOT
    in the lexicon cannot be distinguished from prose against an EN-only bundle, so
    it is NOT flagged. This asserts the bridge's stated limit — if a future change
    starts flagging open-world Devanagari nouns (false positives on real Hindi), this
    test fails and the boundary must be re-documented."""
    # 'फ्लिपकार्ट' (Flipkart) is a real brand but deliberately NOT in the lexicon.
    summary = _summary_hi("फ्लिपकार्ट पर बिक्री शुरू करें।")
    assert validate_plan(summary, [], _bundle()) == []
