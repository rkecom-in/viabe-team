"""VT-553 (Track D) — the HELD-OUT advice-quality measurement set.

MEASUREMENT ONLY. These cases are NOT a corpus and NOT training/seed data: the agent must NEVER read
them or be tuned against them — they exist solely to score held-out output before a capability
graduates (CL-2026-07-01-no-fixed-playbook). This is a STARTER set; the full held-out set is a
Fazal/archetype follow-up (like the seed-memory content). Each case's ``context`` is the grounding a
factual numeric claim must trace to — the no-fabricated-numbers rail checks advice against it.
"""

from __future__ import annotations

from orchestrator.advice_eval import EvalCase

HELD_OUT_CASES: list[EvalCase] = [
    EvalCase(
        case_id="winback-dormant-cohort",
        scenario=(
            "A kirana store has 38 customers who bought regularly but have not returned in 60+ days. "
            "The owner asks how to bring them back."
        ),
        context={
            "dormant_count": 38,
            "avg_days_since_last_purchase": 71,
            "top_prior_category": "household staples",
        },
    ),
    EvalCase(
        case_id="slow-weekday-footfall",
        scenario=(
            "A tea stall owner says Tuesday–Thursday mornings are slow and asks what to do."
        ),
        context={"slow_days": ["Tue", "Wed", "Thu"], "slow_window": "morning"},
    ),
    EvalCase(
        case_id="price-increase-hesitation",
        scenario=(
            "A tailoring shop owner wants to raise prices but fears losing regulars, and asks for advice."
        ),
        context={"last_price_change_months_ago": 18, "regular_customer_share": "high"},
    ),
]

__all__ = ["HELD_OUT_CASES"]
