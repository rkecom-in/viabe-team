#!/usr/bin/env python3
"""VT-234 — Ops Console phase-1A read-only debug view canary.

Source-substrate check (mock-friendly per brief Acceptance). Page is
team-web TSX server component; full HTTP rendering requires Next.js
dev server. This canary verifies the substrate is in place:

- A1: debug page renders step_kind for each step (substrate present)
- A2: requireFazal() gate redirects unauth → /team/ops/login
- A3: input_envelope + output_envelope JsonPretty rendered
- A4: NO Replay / Override affordances (phase-1B locked out)

Wall-clock ≤ 5s.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
PAGE = REPO / "apps/team-web/app/(app)/team/ops/runs/[runId]/debug/page.tsx"
LINKED_PAGE = REPO / "apps/team-web/app/(app)/team/ops/runs/[runId]/page.tsx"
JSON_PRETTY = REPO / "apps/team-web/components/ops/json-pretty.tsx"

RESULTS: dict[int, dict[str, Any]] = {}


def assertion(num: int, name: str, passed: bool, *,
               observed: Any = None, expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed,
                    "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def run_canary() -> int:
    if not PAGE.exists():
        print(f"PREFLIGHT FAIL — page missing: {PAGE}", file=sys.stderr)
        return 2
    if not JSON_PRETTY.exists():
        print(f"PREFLIGHT FAIL — JsonPretty missing: {JSON_PRETTY}",
              file=sys.stderr)
        return 2
    if not LINKED_PAGE.exists():
        print(f"PREFLIGHT FAIL — run replay page missing: {LINKED_PAGE}",
              file=sys.stderr)
        return 2
    print("PREFLIGHT OK")

    page_src = PAGE.read_text(encoding="utf-8")
    linked_src = LINKED_PAGE.read_text(encoding="utf-8")
    json_pretty_src = JSON_PRETTY.read_text(encoding="utf-8")

    # --- A1: step_kind rendered per step ---
    renders_step_kind = (
        "step_kind" in page_src
        and "fetchRunReplay" in page_src
        and "steps.map" in page_src
    )
    assertion(
        1,
        "Debug page renders step_kind per step via fetchRunReplay",
        renders_step_kind,
        observed={
            "step_kind_ref": "step_kind" in page_src,
            "fetchRunReplay": "fetchRunReplay" in page_src,
            "steps_map": "steps.map" in page_src,
        },
    )

    # --- A2: auth gate redirects unauth → /team/ops/login ---
    has_auth_gate = (
        "requireFazal" in page_src
        and "UnauthorizedError" in page_src
        and "/team/ops/login" in page_src
        and "redirect(" in page_src
    )
    assertion(
        2,
        "requireFazal() gate redirects unauth → /team/ops/login",
        has_auth_gate,
        observed={
            "requireFazal": "requireFazal" in page_src,
            "redirect": "redirect(" in page_src,
            "login_target": "/team/ops/login" in page_src,
        },
    )

    # --- A3: JsonPretty input_envelope + output_envelope rendered ---
    has_input_env = re.search(
        r'JsonPretty\s+label="input_envelope"', page_src,
    ) is not None
    has_output_env = re.search(
        r'JsonPretty\s+label="output_envelope"', page_src,
    ) is not None
    has_copy = "navigator.clipboard" in json_pretty_src
    has_pretty_substrate = has_input_env and has_output_env and has_copy
    assertion(
        3,
        "JsonPretty rendered for input_envelope + output_envelope + copy",
        has_pretty_substrate,
        observed={
            "input_envelope": has_input_env,
            "output_envelope": has_output_env,
            "clipboard_copy": has_copy,
        },
    )

    # --- A4: NO Replay / Override / state-changing affordances ---
    forbidden = ["Replay", "Override", "Retrigger", "Retry", "Edit step"]
    found = [w for w in forbidden if re.search(rf"\b{w}\b", page_src)]
    # Verify "Debug view" link present in upstream run page
    has_debug_link = (
        'href={`/team/ops/runs/${runId}/debug`}' in linked_src
        or 'data-element="debug-view-link"' in linked_src
    )
    pass_4 = not found and has_debug_link
    assertion(
        4,
        "No state-changing affordances + Debug view link present in run page",
        pass_4,
        observed={
            "forbidden_found": found,
            "debug_link_in_run_page": has_debug_link,
        },
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
