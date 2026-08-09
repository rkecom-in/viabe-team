"""VT-725/O11 — the fabricated-number gate must fire on invention, not on arithmetic.

A hit here zeroes a case's entire score. Measured against the O11 development set the check was
flagging correct arithmetic, unit-suffixed restatements, numbers inside context KEY names and lakh
notation: one case scored 0.85-0.95 on all ten dimensions and came out 0.0. It was also ARM-BIASED —
an answer that used MORE of the supplied material had a larger numeric surface to trip on.

These tests pin the false-positive classes that were fixed AND, just as importantly, that genuinely
invented figures still die. A gate loosened until it never fires is worse than no gate, because it
reads as a passing check.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from orchestrator.advice_eval import find_fabricated_numbers  # noqa: E402

CONTEXT = {
    "facts": {
        "cash_on_hand_inr": 420000,
        "supplier_payments_due_in_30_days_inr": 610000,
        "verified_receivables_due_in_30_days_inr": 330000,
        "average_collection_days": 52,
        "gross_margin_percent": 11,
    }
}


def flagged(advice: str, context=None) -> list[str]:
    return find_fabricated_numbers(advice, context if context is not None else CONTEXT)


# --- the gate must still have teeth -------------------------------------------------------------

def test_an_invented_figure_is_still_caught() -> None:
    assert "875000" in flagged("You should expect ₹8,75,000 of inflow next month.")


def test_an_invented_rate_is_still_caught() -> None:
    assert "23%" in flagged("Lenders will charge you about 23% on this.")


def test_a_plausible_but_unsupplied_number_is_still_caught() -> None:
    """The dangerous case: a figure that LOOKS like it came from the brief and did not."""

    assert "45" in flagged("Your customers pay in about 45 days on average.")


def test_loosening_did_not_ground_everything() -> None:
    """Guards the whole point — a gate that never fires is worse than no gate."""

    noise = "Numbers: 8123, 9471, 6528, and 37% and 44% and ₹9,99,999."
    assert len(flagged(noise)) >= 5


# --- the false-positive classes that were zeroing real cases ------------------------------------

def test_a_supplied_number_wearing_its_unit_is_not_fabricated() -> None:
    """`gross_margin_percent: 11` grounds "11%". This alone was zeroing cases."""

    assert flagged("At 11% gross margin you cannot absorb that.") == []


def test_a_number_living_inside_a_context_key_is_grounded() -> None:
    """`supplier_payments_due_in_30_days_inr` grounds 30 — `\\b\\d{2,}\\b` cannot match inside `_30_`."""

    assert flagged("Over the next 30 days the position is tight.") == []


def test_arithmetic_over_two_supplied_facts_is_reasoning_not_fabrication() -> None:
    assert flagged("₹4,20,000 cash plus ₹3,30,000 receivables is ₹7,50,000 of inflow.") == []


def test_a_difference_of_two_supplied_facts_is_grounded() -> None:
    # 750000 - 610000 = 140000, itself derived from a sum; the direct 610000-420000 = 190000 too.
    assert flagged("That leaves you ₹1,90,000 short against suppliers.") == []


def test_doubling_one_supplied_fact_is_grounded() -> None:
    """"Two months of ad spend" — the self-pair, which the first version of the rule excluded."""

    assert flagged("Two months of spend is ₹4,80,000.", {"facts": {"monthly_ad_spend_inr": 240000}}) == []


def test_lakh_notation_is_the_same_number_in_the_register_owners_read() -> None:
    for spelling in ("₹4.2L", "₹4.2 lakh", "₹4.2 lakhs"):
        assert flagged(f"You hold {spelling} in cash.") == [], spelling


def test_the_bare_letter_magnitude_requires_a_currency_prefix() -> None:
    """So "42 L" of something that is not money is never silently read as lakh.

    (A single-digit "4.2" is not a claim at all — `_CLAIM_RE` only treats 2+ digit bare numbers as
    significant — so the check needs a two-digit figure to be testing what it says it tests.)
    """

    assert "42" in flagged("Dose it at 42 L per batch.", {"facts": {"cash_on_hand_inr": 4200000}})


def test_crore_scales_too() -> None:
    assert flagged("Revenue is ₹1.2 crore.", {"facts": {"annual_revenue_inr": 12000000}}) == []


def test_an_explicitly_allowed_claim_is_never_flagged() -> None:
    assert find_fabricated_numbers(
        "Budget about ₹7,000 for it.", CONTEXT, allowed_numeric_claims=("7000",)
    ) == []
