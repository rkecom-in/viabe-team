#!/usr/bin/env python3
"""VT-98 — aggregate landing metrics computer (PHASE-1 SCAFFOLD; not run in production yet).

Post-launch this computes the social-proof aggregate metrics (total ARRR recovered, tenants
serviced, average day-39 success rate) from production data and writes them into
`data/social-proof.json`'s `metrics` array. Phase 1 has NO data → it emits nothing.

Pillar 7 (no fabrication) + privacy: EVERY aggregate is k-anonymity gated at K_ANON_MIN (≥10,
the VT-8.3 admission threshold) — an aggregate computed over fewer than K_ANON_MIN tenants is
SUPPRESSED, never published. On empty/insufficient data the script runs clean and emits [].
"""
from __future__ import annotations

from typing import Any

K_ANON_MIN = 10  # VT-8.3 admission threshold — never publish an aggregate below this cohort size.


def compute_metrics(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return the publishable metrics, or [] if the cohort is below k-anonymity. `rows` is the
    per-tenant production aggregate input (empty in Phase 1)."""
    n = len(rows)
    if n < K_ANON_MIN:
        # Below k-anonymity → suppress ALL aggregates (no fabrication, no re-identification risk).
        return []
    total_arrr = sum(int(r.get("arrr_recovered_paise", 0)) for r in rows)
    success = [float(r["day39_success"]) for r in rows if "day39_success" in r]
    avg_success = round(sum(success) / len(success) * 100) if success else 0
    return [
        {"label": "recovered for our customers", "value": f"₹{total_arrr // 100:,}"},
        {"label": "shops served", "value": str(n)},
        {"label": "average day-39 success", "value": f"{avg_success}%"},
    ]


def main() -> int:
    # Phase 1: no production data source wired → empty cohort → emit nothing (clean exit).
    rows: list[dict[str, Any]] = []
    metrics = compute_metrics(rows)
    print(f"computed {len(metrics)} publishable metric(s) (k-anon ≥ {K_ANON_MIN})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
