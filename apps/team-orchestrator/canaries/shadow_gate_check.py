"""VT-611 Package S — shadow-mode evidence gate (gate remediation B5/B6).

The gate's shadow leg (``TEAM_MANAGER_LOOP_MODE=shadow`` on dev, verified via
``orchestrator.manager.shadow_eval``/``dispatch.py``'s ``is_shadow()`` hook) writes one
``tm_audit_log`` row per turn it observes (``event_kind='shadow_divergence'``). Two confirmed
false-proof holes this closes:

  - B5: the mode env var is UNSET by default (``get_loop_mode`` fails closed to 'legacy'), so
    ``is_shadow()`` is False and the shadow leg NEVER FIRES unless someone explicitly flips it —
    the gate would otherwise read "shadow satisfied" while it silently wrote ZERO rows all along.
    Fix: a PREFLIGHT check — after flipping the mode + firing ONE canary turn, confirm >=1 FRESH
    row exists BEFORE launching the full pack. Zero rows -> STOP, do not launch.
  - B6: the evidence is unsound 3 ways if read carelessly — (1) the non-CASCADE teardown FK sweep
    wipes tm_audit_log rows (mig147: ``tenant_id`` NOT NULL, no ``ON DELETE CASCADE`` — capture
    evidence STRICTLY BEFORE any teardown); (2) tm_audit_log's SELECT RLS is operator-JWT-only
    (mig147 — there is NO app_role SELECT policy at all, only INSERT); an ``app_role`` connection
    reads back ZERO rows, misreading as "shadow never fired" even when it did — this script MUST
    run over the SAME privileged/service DATABASE_URL convo_harness.py itself relies on (per its
    own module docstring: "the dev DATABASE_URL role is the privileged pool role (bypasses RLS)"),
    never a tenant-scoped app_role connection; (3) a tm_audit_log row is a TURN, not a conversation
    — the gate's "distinct_conversations >= 50" is COUNT(DISTINCT tenant_id), not a raw row count
    (a single long conversation could otherwise clear "50" on its own).

Gate (verbatim, cite this in the evidence manifest): ``distinct_conversations >= 50 AND
safety_divergences == 0`` (a HARD zero — any blocked-status row is a shadow-leg safety divergence
and fails the gate outright, never averaged/thresholded).

Run via a SERVICE-ROLE / privileged connection ONLY (``railway run --environment dev python …`` so
``DATABASE_URL`` flows OS-env->process — CL-431 by-reference discipline; this script never prints
the DSN, only booleans/counts). Prod stays legacy throughout — this is a DEV-ONLY gate leg.

Usage:

    # S1 — BEFORE launching the 120-scenario pack, after flipping the mode + firing ONE canary turn:
    railway run --service vt-orchestrator-service --environment dev -- \\
        uv run --directory apps/team-orchestrator python canaries/shadow_gate_check.py \\
        preflight --since 2026-07-07T00:00:00Z

    # S2 — AFTER the full pack has run, STRICTLY BEFORE any tenant teardown:
    railway run --service vt-orchestrator-service --environment dev -- \\
        uv run --directory apps/team-orchestrator python canaries/shadow_gate_check.py \\
        evidence --since 2026-07-07T00:00:00Z [--min-distinct 50]

Both subcommands exit 0 only when the check holds; exit 1 otherwise (printing the reason).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_DEFAULT_MIN_DISTINCT = 50


@dataclass(frozen=True)
class ShadowEvidence:
    total_evals: int
    distinct_conversations: int
    safety_divergences: int


def capture_shadow_evidence(conn: Any, since: datetime) -> ShadowEvidence:
    """The S2 evidence query, verbatim per the gate remediation plan. ``conn`` MUST be a
    privileged/service-role connection (see module docstring) — an app_role connection silently
    reads back zero rows under tm_audit_log's operator-JWT-only SELECT RLS, which would misreport
    as "shadow never fired" rather than raising."""
    row = conn.execute(
        "SELECT count(*) AS total_evals, "
        "count(DISTINCT tenant_id) AS distinct_conversations, "
        "count(*) FILTER (WHERE status = 'blocked') AS safety_divergences "
        "FROM tm_audit_log WHERE event_kind = 'shadow_divergence' AND created_at >= %s",
        (since,),
    ).fetchone()
    if row is None:
        return ShadowEvidence(total_evals=0, distinct_conversations=0, safety_divergences=0)
    if isinstance(row, dict):
        return ShadowEvidence(
            total_evals=int(row["total_evals"]),
            distinct_conversations=int(row["distinct_conversations"]),
            safety_divergences=int(row["safety_divergences"]),
        )
    return ShadowEvidence(
        total_evals=int(row[0]), distinct_conversations=int(row[1]), safety_divergences=int(row[2]),
    )


def check_preflight(evidence: ShadowEvidence) -> list[str]:
    """S1: at least ONE fresh shadow_divergence row must exist after the flip + canary turn — a
    zero here means the mode flip didn't actually take (B5), and the pack must NOT be launched."""
    if evidence.total_evals < 1:
        return [
            "shadow preflight FAILED: zero tm_audit_log shadow_divergence rows since the flip — "
            "TEAM_MANAGER_LOOP_MODE=shadow did not take (or the connection can't see them — verify "
            "it's the privileged/service DATABASE_URL, not app_role). DO NOT launch the pack."
        ]
    return []


def check_gate(evidence: ShadowEvidence, *, min_distinct: int = _DEFAULT_MIN_DISTINCT) -> list[str]:
    """S2: the actual promotion-gate criterion — verbatim, cite in the evidence manifest."""
    failures: list[str] = []
    if evidence.distinct_conversations < min_distinct:
        failures.append(
            f"shadow gate FAILED: distinct_conversations={evidence.distinct_conversations} < "
            f"{min_distinct} required"
        )
    if evidence.safety_divergences != 0:
        failures.append(
            f"shadow gate FAILED: safety_divergences={evidence.safety_divergences} (must be a "
            f"HARD zero — any blocked-status row is a shadow-leg safety divergence)"
        )
    return failures


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")
    if not dsn:
        print(
            "shadow_gate_check: ERROR: no DB URL in env (DATABASE_URL / TEAM_SUPABASE_DB_URL) — "
            "run under `railway run --environment dev`",
            file=sys.stderr,
        )
        sys.exit(2)
    return dsn


def _parse_since(value: str) -> datetime:
    # Accept a bare 'Z' suffix (stdlib fromisoformat wants +00:00 pre-3.11-ish tolerant, but be
    # explicit rather than rely on version-specific leniency).
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def cmd_preflight(args: argparse.Namespace) -> int:
    import psycopg

    since = _parse_since(args.since)
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        evidence = capture_shadow_evidence(conn, since)
    failures = check_preflight(evidence)
    print(f"shadow preflight: total_evals={evidence.total_evals} since={args.since}")
    for f in failures:
        print(f"  - {f}")
    if not failures:
        print("shadow preflight: OK — the pack may launch.")
    return 0 if not failures else 1


def cmd_evidence(args: argparse.Namespace) -> int:
    import psycopg

    since = _parse_since(args.since)
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        evidence = capture_shadow_evidence(conn, since)
    failures = check_gate(evidence, min_distinct=args.min_distinct)
    print(
        f"shadow evidence since={args.since}: total_evals={evidence.total_evals} "
        f"distinct_conversations={evidence.distinct_conversations} "
        f"safety_divergences={evidence.safety_divergences}"
    )
    for f in failures:
        print(f"  - {f}")
    if not failures:
        print("shadow gate: PASS — cite this evidence verbatim in the manifest.")
        print(
            "    REMINDER: capture this BEFORE any tenant teardown (the non-CASCADE FK sweep wipes "
            "tm_audit_log rows) and BEFORE flipping the mode back to legacy."
        )
    if args.json:
        # The evidence manifest can't quote a print statement — persist the same numbers a
        # machine can read back, cited verbatim (never re-derived) in the manifest's shadow leg.
        payload = {
            "since": args.since,
            "total_evals": evidence.total_evals,
            "distinct_conversations": evidence.distinct_conversations,
            "safety_divergences": evidence.safety_divergences,
            "min_distinct": args.min_distinct,
            "passed": not failures,
            "failures": failures,
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"    json: wrote {args.json} — for the evidence manifest")
    return 0 if not failures else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shadow_gate_check", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("preflight", help="S1 — confirm the shadow flip actually took")
    pf.add_argument("--since", required=True, help="ISO-8601 timestamp of the mode flip")
    pf.set_defaults(func=cmd_preflight)

    ev = sub.add_parser("evidence", help="S2 — capture + gate-check the shadow-run evidence")
    ev.add_argument("--since", required=True, help="ISO-8601 timestamp the run batch started")
    ev.add_argument("--min-distinct", type=int, default=_DEFAULT_MIN_DISTINCT)
    ev.add_argument("--json", default=None, help="write the evidence + gate verdict for the evidence manifest")
    ev.set_defaults(func=cmd_evidence)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
