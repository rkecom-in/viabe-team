"""Baseline vs treatment on the O11 dimension means.

`mean_score` cannot carry this comparison: a single hard failure zeroes a case's `overall`
regardless of how it scored, and the hard-failure detector is producing false positives (see the
VT-725 signal). Worse, the failure mode is ARM-BIASED — a treatment answer carrying more grounded
figures has a larger numeric surface for the detector to trip on, so `mean_score` would penalise the
arm for using the knowledge it was given.

Dimension means are computed per dimension across cases and are NOT zeroed by hard failures, so they
compare what the judge actually thought of the reasoning. Both are reported; neither is hidden.
"""

from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[4] / ".viabe" / "o11-treatment-run-2026-08-09"
DIMS = [
    "decision_correctness", "applicability", "evidence_grounding", "risk_calibration",
    "regulatory_financial_safety", "feasibility", "specialist_selection",
    "cross_functional_judgment", "tradeoff_recognition", "appropriate_uncertainty",
]


def load(split: str, arm: str) -> dict | None:
    path = BASE / f"scored_{split}_{arm}.json"
    return json.loads(path.read_text()) if path.exists() else None


for split in ("development", "validation"):
    base, treat = load(split, "baseline"), load(split, "treatment")
    if not base or not treat:
        print(f"{split}: missing bundle(s) — skipped")
        continue
    print(f"\n=== {split.upper()}  (n={base['case_count']} cases)")
    print(f"{'dimension':32s} {'baseline':>9s} {'treatment':>10s} {'delta':>8s}")
    wins = losses = 0
    for dim in DIMS:
        b, t = base["dimension_means"][dim], treat["dimension_means"][dim]
        d = t - b
        wins += d > 0.001
        losses += d < -0.001
        flag = "  <<" if abs(d) >= 0.05 else ""
        print(f"{dim:32s} {b:9.3f} {t:10.3f} {d:+8.3f}{flag}")
    bm = sum(base["dimension_means"][d] for d in DIMS) / len(DIMS)
    tm = sum(treat["dimension_means"][d] for d in DIMS) / len(DIMS)
    print(f"{'MEAN OF DIMENSION MEANS':32s} {bm:9.3f} {tm:10.3f} {tm - bm:+8.3f}")
    print(f"  dimensions improved: {wins}/{len(DIMS)}   regressed: {losses}/{len(DIMS)}")
    print(f"  hard-failure cases: baseline {base['hard_failure_count']} / "
          f"treatment {treat['hard_failure_count']}   "
          f"(mean_score: {base['mean_score']:.4f} -> {treat['mean_score']:.4f})")
    for label, bundle in (("baseline", base), ("treatment", treat)):
        for case in bundle["cases"]:
            if case["hard_failures"]:
                print(f"    {label:9s} {case['case_id']}: {case['hard_failures']}")
