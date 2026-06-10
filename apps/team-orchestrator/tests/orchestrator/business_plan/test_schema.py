"""VT-368 Gap-4 — pure behavioral tests for ``orchestrator.business_plan.schema``
(no DB): the citation/grounding validator, the downgrade stripper, and the
deterministic no-LLM degrade template.

The import guards mirror the house dep-less-smoke pattern: ``schema`` imports the
enums from ``store``, which pulls ``orchestrator.db`` → the dbos/psycopg stack,
so collection must skip (not fail) in the dep-less CI smoke job.

All fixture data is fictional (CL-390 — no third-party PII).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

from orchestrator.business_plan.schema import (  # noqa: E402 — after dependency skip guards
    degrade_template,
    strip_violations,
    validate_plan,
)

# --- Fixtures ---------------------------------------------------------------


def _bundle() -> dict:
    """A frozen fact bundle: the ONLY literals a plan may claim."""
    return {
        "F1": {"key": "swiggy_rating", "value": "4.2/5 on Swiggy", "source": "platform_listings"},
        "F2": {"key": "monthly_orders", "value": 350, "source": "owner_input"},
        "F3": {"key": "repeat_rate", "value": "18%", "source": "analytics"},
        "F4": {"key": "avg_ticket", "value": "₹450", "source": "owner_input"},
    }


def _item(**overrides) -> dict:
    """A grounded, fully-shaped roadmap item; override per test."""
    base = {
        "item_id": str(uuid4()),
        "seq": 1,
        "month": 1,
        "objective": "Lift the repeat rate above 18%",
        "why": "The repeat rate is 18% today and orders sit at 350 a month.",
        "cited_facts": ["F2", "F3"],
        "owning_agent": "retention",
        "owner_action_needed": False,
        "owner_action": None,
        "owner_action_hi": None,
        "status": "proposed",
        "provenance": {
            "origin": "llm_v1",
            "editor": None,
            "prev_version": None,
            "diff_from_prev": None,
        },
    }
    base.update(overrides)
    return base


def _summary(**overrides) -> dict:
    base = {
        "text": (
            "Your shop gets 350 orders a month at an average ticket of ₹450. "
            "The repeat rate is 18% and the rating on Swiggy is 4.2."
        ),
        "text_hi": "आपकी दुकान को हर महीने 350 ऑर्डर मिलते हैं और रेटिंग 4.2 है।",
        "cited_facts": ["F1", "F2", "F3", "F4"],
        "headline_metrics": {"monthly_orders": 350, "repeat_rate": "18%"},
    }
    base.update(overrides)
    return base


def _roadmap() -> list[dict]:
    return [
        _item(seq=1, month=1),
        _item(
            seq=2,
            month=2,
            objective="Reply to every review on Swiggy in month 2",
            why="The rating on Swiggy is 4.2 today.",
            cited_facts=["F1"],
            owning_agent="reputation",
        ),
    ]


# --- validate_plan ----------------------------------------------------------


def test_clean_plan_passes():
    assert validate_plan(_summary(), _roadmap(), _bundle()) == []


def test_fabricated_citation_id_in_item_caught():
    roadmap = _roadmap()
    roadmap[0]["cited_facts"] = ["F2", "F9"]
    violations = validate_plan(_summary(), roadmap, _bundle())
    assert any("F9" in v and "roadmap[0]" in v for v in violations)


def test_fabricated_citation_id_in_summary_caught():
    summary = _summary(cited_facts=["F1", "F404"])
    violations = validate_plan(summary, _roadmap(), _bundle())
    assert any("F404" in v and "summary" in v for v in violations)


def test_uncited_number_caught():
    # A rating that is NOT a bundle literal — fabricated improvement claim.
    roadmap = _roadmap()
    roadmap[1]["why"] = "The rating on Swiggy will climb to 4.8 soon."
    violations = validate_plan(_summary(), roadmap, _bundle())
    assert any("4.8" in v and "roadmap[1].why" in v for v in violations)


def test_uncited_number_in_summary_caught():
    summary = _summary(text="Your shop gets 350 orders a month. Sales will triple to 1050 soon.")
    violations = validate_plan(summary, _roadmap(), _bundle())
    assert any("1050" in v and "summary.text" in v for v in violations)


def test_uncited_platform_name_caught():
    # Mid-sentence proper noun absent from every bundle value.
    roadmap = _roadmap()
    roadmap[0]["why"] = "Your listing on Zomato is incomplete and orders sit at 350 a month."
    violations = validate_plan(_summary(), roadmap, _bundle())
    assert any("Zomato" in v and "roadmap[0].why" in v for v in violations)


def test_grounded_platform_and_currency_pass():
    # 'Swiggy' and ₹450 ARE bundle literals — no violation despite mid-sentence use.
    roadmap = _roadmap()
    roadmap[0]["why"] = "The average ticket on Swiggy is ₹450 today."
    roadmap[0]["cited_facts"] = ["F1", "F4"]
    assert validate_plan(_summary(), roadmap, _bundle()) == []


def test_month_axis_numbers_exempt():
    # "in month 2" is roadmap structure, not a factual claim — never a violation.
    roadmap = _roadmap()
    roadmap[0]["objective"] = "Lift the repeat rate above 18% in month 2"
    assert validate_plan(_summary(), roadmap, _bundle()) == []


def test_bad_owning_agent_caught():
    roadmap = _roadmap()
    roadmap[0]["owning_agent"] = "marketing"
    violations = validate_plan(_summary(), roadmap, _bundle())
    assert any("owning_agent 'marketing'" in v for v in violations)


def test_bad_status_caught():
    roadmap = _roadmap()
    roadmap[1]["status"] = "paused"
    violations = validate_plan(_summary(), roadmap, _bundle())
    assert any("status 'paused'" in v for v in violations)


def test_non_dense_seq_caught():
    roadmap = _roadmap()
    roadmap[1]["seq"] = 3  # 1, 3 — gap
    violations = validate_plan(_summary(), roadmap, _bundle())
    assert any("seq" in v and "dense" in v for v in violations)


def test_month_out_of_range_caught():
    roadmap = _roadmap()
    roadmap[0]["month"] = 7
    violations = validate_plan(_summary(), roadmap, _bundle())
    assert any("month 7" in v for v in violations)


def test_objective_length_and_empty_why_caught():
    roadmap = _roadmap()
    roadmap[0]["objective"] = "x" * 121
    roadmap[1]["why"] = ""
    violations = validate_plan(_summary(), roadmap, _bundle())
    assert any("objective exceeds 120" in v for v in violations)
    assert any("roadmap[1]: why empty" in v for v in violations)


def test_duplicate_and_empty_item_id_caught():
    roadmap = _roadmap()
    roadmap[1]["item_id"] = roadmap[0]["item_id"]
    violations = validate_plan(_summary(), roadmap, _bundle())
    assert any("duplicate item_id" in v for v in violations)

    roadmap2 = _roadmap()
    roadmap2[0]["item_id"] = ""
    violations2 = validate_plan(_summary(), roadmap2, _bundle())
    assert any("item_id empty" in v for v in violations2)


# --- strip_violations -------------------------------------------------------


def test_strip_drops_bad_item_and_reseqs():
    roadmap = _roadmap()
    bad = _item(
        seq=2,
        month=3,
        objective="Launch on Zomato and triple orders to 1050",
        why="Zomato dominates the market.",
        cited_facts=["F1"],
        owning_agent="acquisition",
    )
    roadmap.insert(1, bad)
    roadmap[2]["seq"] = 3
    summary = _summary()

    new_summary, new_roadmap, remaining = strip_violations(summary, roadmap, _bundle())

    assert [it["item_id"] for it in new_roadmap] == [roadmap[0]["item_id"], roadmap[2]["item_id"]]
    assert [it["seq"] for it in new_roadmap] == [1, 2]
    assert remaining == []
    # Inputs not mutated.
    assert len(roadmap) == 3 and roadmap[1]["seq"] == 2


def test_strip_removes_offending_sentences_bilingually():
    summary = _summary(
        text="Your shop gets 350 orders a month. Sales will triple to 1050 next month.",
        text_hi="आपकी दुकान को हर महीने 350 ऑर्डर मिलते हैं। बिक्री 1050 हो जाएगी।",
        cited_facts=["F2", "F9"],  # F9 fabricated — must be dropped
    )
    new_summary, new_roadmap, remaining = strip_violations(summary, _roadmap(), _bundle())

    assert "1050" not in new_summary["text"] and "350" in new_summary["text"]
    assert "1050" not in new_summary["text_hi"] and "350" in new_summary["text_hi"]
    assert new_summary["cited_facts"] == ["F2"]
    assert remaining == []
    assert len(new_roadmap) == 2


def test_strip_on_clean_plan_is_lossless():
    summary, roadmap = _summary(), _roadmap()
    new_summary, new_roadmap, remaining = strip_violations(summary, roadmap, _bundle())
    assert remaining == []
    assert new_summary["text"] == summary["text"]
    assert [it["item_id"] for it in new_roadmap] == [it["item_id"] for it in roadmap]


# --- degrade_template -------------------------------------------------------


def test_degrade_template_only_bundle_literals_and_empty_roadmap():
    bundle = _bundle()
    summary, roadmap = degrade_template(bundle, business_name="Sharma Snacks Corner")

    assert roadmap == []
    assert sorted(summary["cited_facts"]) == sorted(bundle)
    assert summary["headline_metrics"] == {
        "swiggy_rating": "4.2/5 on Swiggy",
        "monthly_orders": 350,
        "repeat_rate": "18%",
        "avg_ticket": "₹450",
    }
    # Every bundle value literal surfaces verbatim in BOTH language mirrors.
    for literal in ("4.2/5 on Swiggy", "350", "18%", "₹450"):
        assert literal in summary["text"]
        assert literal in summary["text_hi"]
    assert "Sharma Snacks Corner" in summary["text"]
    # Nothing fabricated: the degrade output passes its own validator.
    assert validate_plan(summary, roadmap, bundle) == []


def test_degrade_template_bilingual_fields_present():
    summary, roadmap = degrade_template(_bundle())
    assert roadmap == []
    assert summary["text"].strip()
    assert summary["text_hi"].strip()
    # The Hindi mirror is actually Hindi (Devanagari present).
    assert any("ऀ" <= ch <= "ॿ" for ch in summary["text_hi"])


def test_degrade_template_empty_bundle():
    summary, roadmap = degrade_template({}, business_name=None)
    assert roadmap == []
    assert summary["cited_facts"] == []
    assert summary["headline_metrics"] == {}
    assert validate_plan(summary, roadmap, {}) == []
