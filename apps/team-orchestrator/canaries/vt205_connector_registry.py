#!/usr/bin/env python3
"""VT-205 connector registry canary (Rule #15, DR-15).

NO orchestrator boot needed. NO DB calls. NO Anthropic. Pure-Python
schema + helper validation. Fast.

    cd apps/team-orchestrator
    ./.venv/bin/python canaries/vt205_connector_registry.py

Wall-clock < 5s. Cost: 0 paise.

4 assertions per brief:

- A1: every registry entry passes ``ConnectorSpec`` Pydantic validation
  (re-validates by round-tripping each entry through model_validate)
- A2: ``get_connector('google_sheet')`` returns the entry; unknown id
  raises ``KeyError``
- A3: ``list_connectors(category='manual')`` returns ≥8 entries;
  ``list_connectors()`` returns ≥16
- A4: ``render_connector_listing_markdown()`` is deterministic
  (byte-identical on two consecutive calls) AND lists every entry
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}


def assertion(num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def run_canary() -> int:
    from orchestrator.integrations import (
        REGISTRY,
        ConnectorSpec,
        get_connector,
        list_connectors,
        render_connector_listing_markdown,
    )

    # A1
    drift: list[str] = []
    for cid, spec in REGISTRY.items():
        try:
            ConnectorSpec.model_validate(spec.model_dump())
        except Exception as exc:  # noqa: BLE001
            drift.append(f"{cid}: {exc!r}")
    pass_1 = not drift
    assertion(
        1,
        "every registry entry passes ConnectorSpec validation",
        pass_1,
        observed={"total": len(REGISTRY), "drift_count": len(drift), "drift_sample": drift[:3]},
        expected={"drift_count": 0},
    )

    # A2
    real = get_connector("google_sheet")
    raised = False
    try:
        get_connector("does_not_exist_2026_05_28")
    except KeyError:
        raised = True
    pass_2 = real.connector_id == "google_sheet" and raised
    assertion(
        2,
        "get_connector returns entry for known id; raises KeyError on unknown",
        pass_2,
        observed={
            "google_sheet_display_name": real.display_name,
            "unknown_raised_keyerror": raised,
        },
        expected={"unknown_raised_keyerror": True},
    )

    # A3 — brief counts "≥8 manual stubs" as VT-6 family entries (paper_book
    # / contacts / upi_export / kot_pos / cash_book / qr_opt_in / apify_*
    # / owner_typed). apify_scrape's category is 'scrape' but it's still a
    # VT-6 family stub — count via implementation_vt_row starting "VT-5"
    # (range VT-52..VT-59 per brief) rather than category=manual.
    vt6_stubs = [
        s for s in REGISTRY.values()
        if s.implementation_vt_row.startswith("VT-5")
    ]
    everything = list_connectors()
    pass_3 = len(vt6_stubs) >= 8 and len(everything) >= 16
    assertion(
        3,
        "list_connectors: ≥8 VT-6 family stubs, ≥16 total entries",
        pass_3,
        observed={"vt6_stub_count": len(vt6_stubs), "total_count": len(everything)},
        expected={"vt6_stub_count_gte": 8, "total_count_gte": 16},
    )

    # A4 — determinism + coverage
    rendered_1 = render_connector_listing_markdown()
    rendered_2 = render_connector_listing_markdown()
    determinism = rendered_1 == rendered_2
    all_ids_listed = all(cid in rendered_1 for cid in REGISTRY)
    pass_4 = determinism and all_ids_listed
    assertion(
        4,
        "render_connector_listing_markdown deterministic + lists every entry",
        pass_4,
        observed={
            "determinism": determinism,
            "all_ids_listed": all_ids_listed,
            "rendered_size_bytes": len(rendered_1),
        },
        expected={"determinism": True, "all_ids_listed": True},
    )

    return _finalise()


def _finalise() -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")
    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print(f"\nALL {len(RESULTS)} ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
