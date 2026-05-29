#!/usr/bin/env python3
"""VT-238 — Ops Console terminal-style live stream canary.

Source-substrate check. Page is team-web TSX client component; full
visual + Realtime verification needs Next.js dev server + Supabase
project. This canary verifies substrate is in place:

- A1: StreamTerminalView component exists + terminal sentinels
      (bg-gray-900, font-mono, terminal-search, terminal-rows)
- A2: card sentinels removed from live stream surface (StreamRowList
      no longer imported in stream-feed)
- A3: filter dimensions preserved (FilterSidebar + QuickFilterPills
      still mounted in StreamFeed)
- A4: history view still uses StreamRowList (LOCK 2 — history
      UNCHANGED)

Wall-clock ≤ 5s.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
TERMINAL = REPO / "apps/team-web/components/ops/stream-terminal-view.tsx"
FEED = REPO / "apps/team-web/components/ops/stream-feed.tsx"
HISTORY = REPO / "apps/team-web/components/ops/stream-history-view.tsx"

RESULTS: dict[int, dict[str, Any]] = {}


def assertion(num: int, name: str, passed: bool, *, observed: Any = None,
               expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed,
                    "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def run_canary() -> int:
    for p in (TERMINAL, FEED, HISTORY):
        if not p.exists():
            print(f"PREFLIGHT FAIL — missing: {p}", file=sys.stderr)
            return 2
    print("PREFLIGHT OK")

    term_src = TERMINAL.read_text(encoding="utf-8")
    feed_src = FEED.read_text(encoding="utf-8")
    history_src = HISTORY.read_text(encoding="utf-8")

    # --- A1: terminal sentinels present ---
    terminal_ok = all(
        s in term_src
        for s in (
            "bg-gray-900",
            "font-mono",
            "terminal-search",
            "terminal-rows",
            "Resume tailing",
            'data-component="stream-terminal-view"',
            "JsonPretty",
        )
    )
    assertion(
        1,
        "StreamTerminalView present with terminal sentinels",
        terminal_ok,
        observed={
            "bg_gray_900": "bg-gray-900" in term_src,
            "font_mono": "font-mono" in term_src,
            "search": "terminal-search" in term_src,
            "resume_tailing": "Resume tailing" in term_src,
            "json_pretty": "JsonPretty" in term_src,
        },
    )

    # --- A2: card sentinels removed from live feed ---
    feed_no_row_list = (
        "StreamRowList" not in feed_src
        and "stream-row-list" not in feed_src
        and "StreamTerminalView" in feed_src
    )
    assertion(
        2,
        "StreamRowList removed from StreamFeed (terminal swap applied)",
        feed_no_row_list,
        observed={
            "row_list_absent": "StreamRowList" not in feed_src,
            "terminal_view_imported": "StreamTerminalView" in feed_src,
        },
    )

    # --- A3: filter dimensions preserved ---
    filters_ok = (
        "FilterSidebar" in feed_src
        and "QuickFilterPills" in feed_src
        and 'data-filter="tenant"' in feed_src
        and 'data-filter="step-kind"' in feed_src
        and 'data-filter="status"' in feed_src
    )
    assertion(
        3,
        "Filter dimensions preserved (tenant/step-kind/status + sidebar + pills)",
        filters_ok,
        observed={
            "sidebar": "FilterSidebar" in feed_src,
            "pills": "QuickFilterPills" in feed_src,
            "tenant_filter": 'data-filter="tenant"' in feed_src,
            "step_kind_filter": 'data-filter="step-kind"' in feed_src,
            "status_filter": 'data-filter="status"' in feed_src,
        },
    )

    # --- A4: history view UNCHANGED (still uses StreamRowList) ---
    history_unchanged = "StreamRowList" in history_src
    assertion(
        4,
        "History view still uses StreamRowList (LOCK 2 unchanged)",
        history_unchanged,
        observed={"history_row_list": "StreamRowList" in history_src},
    )

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)",
              file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
