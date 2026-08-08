"""VT-725 — derive the floor from the measured relevant/irrelevant separation.

Fits on the DEVELOPMENT split only; the VALIDATION split is reported afterwards as a held-out check
and is never used to pick the number. Reports AUC first: if the scorer cannot separate the two
classes at all, no floor is a good floor and that has to be said before any number is chosen.

Bias is toward PRECISION per the mandate — a false card entering the Manager's context is worse than
a miss — so the chosen floor is the smallest threshold meeting a precision target, not the F1 peak.
"""

from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

COMP = json.loads(Path(sys.argv[1]).read_text())
LAB = json.loads(Path(sys.argv[2]).read_text())
TOP_K = 8
MAX_PER_CLUSTER = 2
PRECISION_TARGET = float(sys.argv[3]) if len(sys.argv) > 3 else 0.60

WEIGHTS = [("semantic", 0.38), ("lexical", 0.24), ("entity", 0.10), ("authority", 0.12),
           ("applicability", 0.08), ("confidence", 0.05), ("recency", 0.03)]


def score(row: dict) -> float:
    pairs = [(w, row[name]) for name, w in WEIGHTS if row[name] is not None]
    return sum(w * v for w, v in pairs) / sum(w for w, _ in pairs)


labels_by_case = {c["case_id"]: c for c in LAB["cases"]}
cases = []
for case in COMP["cases"]:
    lab = labels_by_case[case["case_id"]]
    rows = []
    for row, label in zip(case["cards"], lab["labels"], strict=True):
        rows.append({"score": score(row), "label": label, "applicable": row["applicable"],
                     "claim": row["claim"]})
    cases.append({"case_id": case["case_id"], "split": case["split"], "rows": rows})


def auc(rows: list[dict]) -> float | None:
    """Mann-Whitney AUC over cards the engine actually scores (inapplicable never reach the floor)."""
    pos = [r["score"] for r in rows if r["label"] and r["applicable"]]
    neg = [r["score"] for r in rows if not r["label"] and r["applicable"]]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


print("=== DOES THE SCORER SEPARATE RELEVANT FROM IRRELEVANT? (AUC, applicable cards only)")
for case in cases:
    rows = case["rows"]
    a = auc(rows)
    pos = [r["score"] for r in rows if r["label"] and r["applicable"]]
    neg = [r["score"] for r in rows if not r["label"] and r["applicable"]]
    lost = sum(1 for r in rows if r["label"] and not r["applicable"])
    print(
        f"{case['case_id']:34s} [{case['split'][:3]}] AUC={a:.3f}  "
        f"relevant n={len(pos)} med={st.median(pos):.4f} max={max(pos):.4f}  "
        f"irrelevant n={len(neg)} med={st.median(neg):.4f} max={max(neg):.4f}  "
        f"relevant_lost_to_applicability={lost}"
    )

dev = [c for c in cases if c["split"] == "development"]
val = [c for c in cases if c["split"] == "validation"]
dev_rows = [r for c in dev for r in c["rows"] if r["applicable"]]
print(f"\nDEV pooled AUC = {auc(dev_rows):.3f}   (n_rel={sum(r['label'] for r in dev_rows)}, "
      f"n_irrel={sum(1 for r in dev_rows if not r['label'])})")


def sweep(case_list: list[dict], floor: float) -> dict:
    """Apply the real gate: floor, then top_k. Precision is measured on what is actually INJECTED."""
    tp = fp = fn = 0
    injected_total = 0
    cases_with_cards = 0
    for case in case_list:
        eligible = [r for r in case["rows"] if r["applicable"] and r["score"] >= floor]
        eligible.sort(key=lambda r: -r["score"])
        injected = eligible[:TOP_K]
        injected_total += len(injected)
        cases_with_cards += 1 if injected else 0
        tp += sum(1 for r in injected if r["label"])
        fp += sum(1 for r in injected if not r["label"])
        fn += sum(1 for r in case["rows"] if r["label"] and r not in injected)
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    return {"floor": floor, "tp": tp, "fp": fp, "fn": fn, "precision": precision,
            "recall": recall, "injected": injected_total, "cases_with_cards": cases_with_cards,
            "cases": len(case_list)}


print("\n=== DEV SPLIT FLOOR SWEEP (gate = floor then top_k=8, precision measured on INJECTED cards)")
print(f"{'floor':>6s} {'inj':>4s} {'tp':>3s} {'fp':>3s} {'fn':>3s} {'prec':>6s} {'rec':>6s} {'cases_with_cards':>17s}")
grid = [round(x * 0.005, 3) for x in range(0, 141)]
rows_out = []
for f in grid:
    s = sweep(dev, f)
    rows_out.append(s)
    if abs(f * 200 - round(f * 200)) < 1e-9 and round(f * 1000) % 25 == 0:
        p = f"{s['precision']:.3f}" if s["precision"] is not None else "  n/a"
        r = f"{s['recall']:.3f}" if s["recall"] is not None else "  n/a"
        coverage = f"{s['cases_with_cards']}/{s['cases']}"
        print(f"{f:6.3f} {s['injected']:4d} {s['tp']:3d} {s['fp']:3d} {s['fn']:3d} {p:>6s} {r:>6s} "
              f"{coverage:>17s}")

print("\n=== FINE GRID over the live range (dev)")
print(f"{'floor':>6s} {'inj':>4s} {'tp':>3s} {'fp':>3s} {'fn':>3s} {'prec':>6s} {'rec':>6s} {'cov':>5s}")
for f in [round(0.200 + i * 0.005, 3) for i in range(0, 21)]:
    s = sweep(dev, f)
    p = f"{s['precision']:.3f}" if s["precision"] is not None else "  n/a"
    r = f"{s['recall']:.3f}" if s["recall"] is not None else "  n/a"
    print(f"{f:6.3f} {s['injected']:4d} {s['tp']:3d} {s['fp']:3d} {s['fn']:3d} {p:>6s} {r:>6s} "
          f"{s['cases_with_cards']}/{s['cases']:>3d}")

print("\n=== HELD-OUT VALIDATION at the same floors (never used to pick)")
print(f"{'floor':>6s} {'inj':>4s} {'tp':>3s} {'fp':>3s} {'prec':>6s} {'rec':>6s} {'cov':>5s}")
for f in [0.200, 0.225, 0.240, 0.250, 0.255, 0.260, 0.270]:
    s = sweep(val, f)
    p = f"{s['precision']:.3f}" if s["precision"] is not None else "  n/a"
    r = f"{s['recall']:.3f}" if s["recall"] is not None else "  n/a"
    print(f"{f:6.3f} {s['injected']:4d} {s['tp']:3d} {s['fp']:3d} {p:>6s} {r:>6s} "
          f"{s['cases_with_cards']}/{s['cases']:>3d}")

viable = [s for s in rows_out
          if s["precision"] is not None and s["precision"] >= PRECISION_TARGET
          and s["cases_with_cards"] == s["cases"]]
print(f"\n=== FLOORS MEETING precision >= {PRECISION_TARGET} AND every dev case still retrieving")
if not viable:
    best = max((s for s in rows_out if s["precision"] is not None), key=lambda s: s["precision"])
    print(f"  NONE. Best attainable precision = {best['precision']:.3f} at floor {best['floor']:.3f} "
          f"({best['cases_with_cards']}/{best['cases']} cases retrieving)")
else:
    chosen = min(viable, key=lambda s: s["floor"])
    highest = max(viable, key=lambda s: s["floor"])
    print(f"  range: {chosen['floor']:.3f} .. {highest['floor']:.3f}")
    for s in (chosen, highest):
        print(f"    floor={s['floor']:.3f} inj={s['injected']} prec={s['precision']:.3f} "
              f"rec={s['recall']:.3f} cases_with_cards={s['cases_with_cards']}/{s['cases']}")
