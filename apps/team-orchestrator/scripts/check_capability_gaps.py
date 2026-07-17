#!/usr/bin/env python3
"""VT-669 — the CAPABILITY-GAP gate ("fail loudly", Fazal 2026-07-18).

The framework enforces tool SAFETY (deny-list) and, at boot, tool REACHABILITY for what a specialist
DECLARES it needs. Neither tells us the common-tool ACTION surface is INCOMPLETE. Fazal's ruling:
don't hide the holes behind green checks — name them and fail LOUD until each is built.

This gate reads ``open_capability_gaps()`` from the tool catalog (the self-updating "what's still
missing" list) and:
  - exits 1 while ANY capability gap is open, printing each gap + the board row that closes it;
  - exits 0 once every tracked gap has been built/promoted.

It is INTENTIONALLY red today (the sufficiency frontier is real and unfinished). It is a REPORT /
on-demand gate — deliberately NOT wired into the blocking pre-push suite, because these are
capability EXPANSIONS (follow-on VT rows), not regressions, and blocking every push on the shared
tree until they land would sabotage unrelated work. Run it to see the frontier:

    cd apps/team-orchestrator && uv run python scripts/check_capability_gaps.py

Requires the orchestrator env (importing the catalog pulls the tool surfaces / langchain), so run it
under ``uv run`` — not the dep-less pre-push smoke.
"""

from __future__ import annotations

import sys


def main() -> int:
    from orchestrator.agent_framework.tool_catalog import (
        KNOWN_CAPABILITY_GAPS,
        open_capability_gaps,
    )

    open_gaps = open_capability_gaps()
    tracked = len(KNOWN_CAPABILITY_GAPS)
    closed = tracked - len(open_gaps)

    if not open_gaps:
        print(
            f"capability-gaps: ok — all {tracked} tracked capability gap(s) built/promoted "
            "(common-tool surface is sufficiency-complete)."
        )
        return 0

    print(
        "::error::capability-gaps (VT-669): the common-tool ACTION surface is INCOMPLETE — "
        f"{len(open_gaps)} of {tracked} capability gap(s) OPEN "
        f"({closed} closed). A specialist's job depends on tools that do not exist / are not common "
        "yet. Each is on the board; build it, then the gap auto-closes.",
        file=sys.stderr,
    )
    for g in open_gaps:
        print(f"\n  ● [{g.followon_vt}] {g.title}", file=sys.stderr)
        print(f"      needed by : {', '.join(g.needed_by)}", file=sys.stderr)
        print(f"      probe     : {g.kind.value} {list(g.probe_names)}", file=sys.stderr)
        print(f"      why       : {g.reason}", file=sys.stderr)
    print(
        "\n  Fail-loud by design (Fazal 2026-07-18). Close each row above to turn this gate green.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
