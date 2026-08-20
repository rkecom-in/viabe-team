"""Recompute O11 hard failures with the fixed fabricated-number gate, WITHOUT re-judging.

The judge's per-dimension scores are already in the scored bundles and the gate change does not
touch them. Recomputing from those saved scores is exact, costs nothing, and — the real reason —
removes judge sampling noise from the before/after, so any movement is attributable to the gate fix
alone rather than to a second roll of the judge.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from orchestrator.advice_eval import DatasetSplit, find_fabricated_numbers, load_dataset  # noqa: E402

REPO = Path(__file__).resolve().parents[4]
SCORED = REPO / ".viabe" / "o11-treatment-run-2026-08-09"
CASES_DIR = REPO / "apps" / "team-orchestrator" / "canaries" / "o11"


def main(responses_dir: Path) -> int:
    print(f"{'split/arm':22s} {'mean_score':>22s} {'hard-fail cases':>17s}")
    for split in ("development", "validation"):
        cases = {
            case.case_id: case
            for case in load_dataset(CASES_DIR / split, split=DatasetSplit(split))
        }
        for arm in ("baseline", "treatment"):
            scored = json.loads((SCORED / f"scored_{split}_{arm}.json").read_text())
            bundle = json.loads((responses_dir / f"{split}_{arm}.json").read_text())
            decisions = {r["case_id"]: r["decision"] for r in bundle["responses"]}

            overalls, failing = [], 0
            for entry in scored["cases"]:
                case = cases[entry["case_id"]]
                fabricated = find_fabricated_numbers(
                    decisions[entry["case_id"]],
                    case.agent_view(),
                    allowed_numeric_claims=(
                        *case.allowed_numeric_claims,
                        *(calc.result for calc in case.deterministic_calculations),
                    ),
                )
                dimensions = list(entry["scores"].values())
                mean = sum(d["score"] for d in dimensions) / len(dimensions)
                overalls.append(0.0 if fabricated else mean)
                failing += 1 if fabricated else 0
                if fabricated:
                    print(f"    still flagged: {entry['case_id']}: {fabricated}")

            before, after = scored["mean_score"], sum(overalls) / len(overalls)
            print(
                f"{split[:3]}/{arm:9s} {before:9.4f} -> {after:9.4f} "
                f"{scored['hard_failure_count']} -> {failing:<3d}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
