#!/usr/bin/env python3
"""VT-495 KnowYourGST name→GSTIN scraper canary (Rule #15 — real ScrapingBee call, fail-not-skip).

Subshell-source `.viabe/secrets/scrapingbee.env`:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/scrapingbee.env
      set +a
      ./.venv/bin/python canaries/vt495_knowyourgst.py
    )

Exits 0 iff the REAL ScrapingBee-rendered knowyourgst.com by-name search for "RKECOM Services Pvt
Ltd" returns the "RKECOM SERVICES (OPC) PRIVATE LIMITED" registry row with a valid-shape GSTIN, via
the matching layer (search_company_by_similar_name). GSTIN is a PUBLIC GST record — safe to print.
The ScrapingBee API key is read from env and NEVER printed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_GSTIN_RE = re.compile(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]")
RESULTS: dict[int, dict[str, Any]] = {}


def assertion(num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _preflight() -> None:
    if not os.environ.get("SCRAPINGBEE_API_KEY", "").strip():
        print("PREFLIGHT FAIL — missing SCRAPINGBEE_API_KEY. Source the secret in a subshell.", file=sys.stderr)
        sys.exit(2)
    print("PREFLIGHT OK — SCRAPINGBEE_API_KEY present (value not shown)")


def run_canary() -> int:
    _preflight()

    from orchestrator.integrations.methods.knowyourgst import (
        _FORM_URL,
        _QUERY_FIELD_SELECTOR,
        _SUBMIT_SELECTOR,
        KnowYourGSTScraper,
    )
    from orchestrator.integrations.methods.knowyourgst_match import search_company_by_similar_name

    print(f"\nReverse-engineered search: POST form {_FORM_URL}")
    print(f"  driven via ScrapingBee js_scenario: fill {_QUERY_FIELD_SELECTOR} → click {_SUBMIT_SELECTOR} → wait")
    print("  result-row selectors: <a href=/gst-number-search/..><h5>NAME</h5></a>"
          " + <span class=black-text><strong>STATE</strong>,<strong>GSTIN</strong></span>\n")

    scraper = KnowYourGSTScraper()

    # Assertion 1 — raw scraper.search() on the normalized query returns a parsed row.
    raw_err = None
    raw_rows: list[dict[str, str]] = []
    try:
        raw_rows = scraper.search("rkecom")
    except Exception as exc:  # noqa: BLE001 — search() should never raise; surface if it does
        raw_err = f"{type(exc).__name__}: {exc}"
    raw_ok = bool(raw_rows) and any("RKECOM" in (r.get("company_name") or "").upper() for r in raw_rows)
    assertion(
        1,
        "Real ScrapingBee scrape of knowyourgst by-name 'rkecom' returns a parsed RKECOM row",
        raw_ok,
        observed={"rows": raw_rows, "error": raw_err},
        expected="≥1 row whose company_name contains RKECOM",
    )

    # Assertion 2 — the matching layer maps the typed name → the registered RKECOM company + GSTIN.
    match_err = None
    matched: list[dict[str, str]] = []
    try:
        matched = search_company_by_similar_name(scraper, "RKECOM Services Pvt Ltd")
    except Exception as exc:  # noqa: BLE001
        match_err = f"{type(exc).__name__}: {exc}"
    top = matched[0] if matched else {}
    gstin = (top.get("gst_number") or "").strip().upper()
    name_ok = "RKECOM" in (top.get("company_name") or "").upper()
    gstin_ok = bool(_GSTIN_RE.fullmatch(gstin))
    match_ok = bool(matched) and name_ok and gstin_ok
    assertion(
        2,
        "search_company_by_similar_name('RKECOM Services Pvt Ltd') → registered RKECOM row + valid GSTIN shape",
        match_ok,
        observed={"matched": matched, "top_gstin_shape_ok": gstin_ok, "error": match_err},
        expected="company_name contains RKECOM AND gst_number matches the 15-char GSTIN shape",
    )

    if gstin_ok:
        print(f"\n>>> RKeCom GSTIN found (public record): {gstin}")

    print("\n=== AUDIT ARTIFACT — matching-layer output ===")
    print(json.dumps(matched, ensure_ascii=False, indent=2))

    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print("\nALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
