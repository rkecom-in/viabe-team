#!/usr/bin/env python3
"""VT-403 — `no-raw-railway-variables` gate.

Forbids raw ``railway variables`` (and ``railway variables --json``) invocations in committed
executable files. Reading a secret store and letting its output reach stdout is how CC leaked
secret VALUES twice (RESEND_API_KEY, PROD ANTHROPIC_API_KEY) — CL-431 / VT-403. All env inspection
must route through ``scripts/env_presence.py`` (names → booleans, never a value).

Scans git-tracked ``*.py`` / ``*.sh`` / ``*.yml`` / ``*.yaml`` (not prose .md, not the gitignored
.running signals). The one sanctioned site that may contain the string is the helper itself, plus
this checker. Exit 1 on any other occurrence.
"""

from __future__ import annotations

import re
import subprocess
import sys

_PATTERN = re.compile(r"railway\s+variables\b", re.IGNORECASE)
_SCAN_SUFFIXES = (".py", ".sh", ".yml", ".yaml")
_ALLOWLIST = frozenset(
    {
        # The sanctioned single site that reads `railway variables --json` internally and emits
        # only booleans (VT-403).
        "scripts/env_presence.py",
        # This checker names the forbidden pattern.
        "scripts/check_no_raw_railway_variables.py",
    }
)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", *(f"*{s}" for s in _SCAN_SUFFIXES)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def main() -> int:
    violations: list[str] = []
    for rel in _tracked_files():
        if rel in _ALLOWLIST:
            continue
        try:
            with open(rel, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if _PATTERN.search(line):
                        violations.append(f"{rel}:{n}: raw `railway variables` — use scripts/env_presence.py")
        except (OSError, UnicodeDecodeError):
            continue

    if violations:
        print(
            "::error::no-raw-railway-variables (VT-403): raw `railway variables` reaches stdout and "
            "leaks secret VALUES. Route env inspection through scripts/env_presence.py (names → booleans).",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print("no-raw-railway-variables: ok (env inspection routes through scripts/env_presence.py).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
