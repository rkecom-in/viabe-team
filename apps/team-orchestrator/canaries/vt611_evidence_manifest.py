"""VT-611 — the redacted evidence manifest (GATE DEFINITION's final artifact).

Assembles the promotion-gate evidence from the machine-readable outputs the other VT-611 gate
tools now persist (none of this manifest is re-derived by re-running anything, and none of it is
scraped from a terminal — every number here is CITED from a file another tool already wrote):

  - ``run_full_pack.py --summary-json``      -> domain floors + per-scenario harness-clean verdicts
  - ``transcript_judge.py`` (the pack bundle) -> per-scenario judge dims + mean, ``<bundle>.judged.json``
  - ``run_critical_x3.py --summary-json``    -> the 77-critical ×3 results + cross-run consistency
  - ``transcript_judge.py`` (the critical bundle) -> judge verdicts for the ×3 runs
  - ``shadow_gate_check.py evidence --json``  -> the shadow-mode distinct-conversation/divergence counts

Plus deploy metadata (SHA/migration version/loop-mode) and two Fazal/team-lead-mandated procedural
confirmations (synthetic teardown swept every tenant; zero real-customer sends — bogus numbers +
dev_send_guard mocks only) that this run makes as explicit CLI flags, never assumed.

Also states the two HONESTY CAVEATS the gate remediation pre-mortem surfaced (team-lead, 2026-07-06)
so a reader of the manifest knows exactly what "DB-proof of routing/delegation" does and doesn't
cover — these are NOT bugs, they are the manifest being honest about its own proof's scope:

  1. ``assert_route``'s SR-only fragility — ``campaigns`` (VT-611 Package H1) is SR-exclusive TODAY
     (migration 016); row-existence proves SR delegation only because no other specialist writes
     that table yet. If a second specialist ever does, this signal breaks and must be revisited.
  2. "Delegation" DB-proof = SR-spawn only — Sales Recovery is the only spawnable specialist today;
     Marketing/Accounting are advisory tool-lanes (inline tool-use, not a delegated sub-agent), so
     their "routing" is judge-scored, never DB-asserted, by design.

Usage:

    uv run python canaries/vt611_evidence_manifest.py \\
        --deploy-sha <git sha> --migration-version <N> --loop-mode shadow \\
        --pack-summary-json pack_summary.json --pack-judged pack_bundle.json.judged.json \\
        --critical-summary-json critical_summary.json --critical-judged critical_bundle.json.judged.json \\
        --shadow-json shadow_evidence.json \\
        --teardown-confirmed --zero-real-send-confirmed \\
        --out vt611_evidence_manifest.json

Exits 0 only when every sub-gate present in the manifest passed; any missing input is reported as
an open gap (never silently treated as passing) rather than causing a crash.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

HONESTY_CAVEATS: tuple[str, ...] = (
    "assert_route SR-only fragility: campaigns (VT-611 Package H1) is SR-exclusive TODAY "
    "(migration 016) — row-existence proves SR delegation only because no other specialist "
    "writes that table yet. If a second specialist ever does, this signal breaks and must be "
    "revisited.",
    "\"Delegation\" DB-proof = SR-spawn only: Sales Recovery is the only spawnable specialist "
    "today; Marketing/Accounting are advisory tool-lanes (inline tool-use, not a delegated "
    "sub-agent) — their routing is judge-scored, never DB-asserted, by design.",
)


def _load_json(path: str | None) -> Any | None:
    """Loads either shape this manifest consumes — a dict (judge/shadow output) or a list
    (run_critical_x3's --summary-json, one entry per scenario) — so the caller's own type
    annotation decides what's expected, not this loader."""
    if path is None:
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _pack_section(summary: dict[str, Any] | None, judged: dict[str, Any] | None) -> dict[str, Any]:
    if summary is None:
        return {"available": False, "gap": "no --pack-summary-json given"}
    scenarios = summary.get("scenarios", [])
    dirty = [s for s in scenarios if not s.get("clean")]
    section: dict[str, Any] = {
        "available": True,
        "domain_counts": summary.get("domain_counts"),
        "domain_floors": summary.get("domain_floors"),
        "domain_floor_failures": summary.get("domain_floor_failures", []),
        "total_scenarios": len(scenarios),
        "harness_clean_count": len(scenarios) - len(dirty),
        "harness_findings": [{"name": s["name"], "reasons": s["block_reasons"]} for s in dirty],
    }
    if judged is None:
        section["judge"] = {"available": False, "gap": "no --pack-judged given"}
    else:
        rows = judged.get("scenarios", [])
        failing = [r for r in rows if not r.get("passed")]
        section["judge"] = {
            "available": True,
            "threshold": judged.get("threshold"),
            "mean_threshold": judged.get("mean_threshold"),
            "all_passed": judged.get("all_passed"),
            "n_pass": len(rows) - len(failing),
            "n_total": len(rows),
            "failing": [{"scenario": r["scenario"], "mean_score": r["mean_score"]} for r in failing],
        }
    section["passed"] = (
        not summary.get("domain_floor_failures")
        and not dirty
        and (judged is None or bool(judged.get("all_passed")))
    )
    return section


def _critical_section(summary: list[dict[str, Any]] | None, judged: dict[str, Any] | None) -> dict[str, Any]:
    if summary is None:
        return {"available": False, "gap": "no --critical-summary-json given"}
    blocked = [
        s["scenario"] for s in summary
        if not s.get("consistent") or any(not r.get("clean") for r in s.get("runs", []))
    ]
    section: dict[str, Any] = {
        "available": True,
        "total_critical_scenarios": len(summary),
        "all_3_of_3_clean_and_consistent": len(summary) - len(blocked),
        "blocked_scenarios": blocked,
    }
    if judged is None:
        section["judge"] = {"available": False, "gap": "no --critical-judged given"}
    else:
        rows = judged.get("scenarios", [])
        failing = [r for r in rows if not r.get("passed")]
        section["judge"] = {
            "available": True,
            "all_passed": judged.get("all_passed"),
            "n_pass": len(rows) - len(failing),
            "n_total": len(rows),
            "failing": [{"scenario": r["scenario"], "mean_score": r["mean_score"]} for r in failing],
        }
    section["passed"] = not blocked and (judged is None or bool(judged.get("all_passed")))
    return section


def _shadow_section(evidence: dict[str, Any] | None) -> dict[str, Any]:
    if evidence is None:
        return {"available": False, "gap": "no --shadow-json given"}
    return {"available": True, **evidence}


def build_manifest(
    *,
    deploy_sha: str, migration_version: str, loop_mode: str,
    pack_summary: dict[str, Any] | None, pack_judged: dict[str, Any] | None,
    critical_summary: list[dict[str, Any]] | None, critical_judged: dict[str, Any] | None,
    shadow_evidence: dict[str, Any] | None,
    teardown_confirmed: bool, zero_real_send_confirmed: bool,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Pure assembly — every input is an already-loaded dict (or None if that leg wasn't run yet),
    so this is fully unit-testable without any file I/O. A missing leg is reported as an explicit
    open gap in the manifest, never silently dropped or treated as a pass."""
    pack = _pack_section(pack_summary, pack_judged)
    critical = _critical_section(critical_summary, critical_judged)
    shadow = _shadow_section(shadow_evidence)

    legs_available = [pack["available"], critical["available"], shadow["available"]]
    legs_passed = [
        pack.get("passed", False), critical.get("passed", False), shadow.get("passed", False),
    ]
    overall_gate_passed = (
        all(legs_available) and all(legs_passed) and teardown_confirmed and zero_real_send_confirmed
    )

    return {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "deploy_sha": deploy_sha,
        "migration_version": migration_version,
        "loop_mode_during_run": loop_mode,
        "pack": pack,
        "critical_x3": critical,
        "shadow": shadow,
        "teardown_confirmed": teardown_confirmed,
        "zero_real_send_confirmed": zero_real_send_confirmed,
        "honesty_caveats": list(HONESTY_CAVEATS),
        "overall_gate_passed": overall_gate_passed,
    }


def _print_summary(manifest: dict[str, Any]) -> None:
    print("=== VT-611 evidence manifest ===")
    print(f"deploy_sha={manifest['deploy_sha']} migration_version={manifest['migration_version']} "
          f"loop_mode_during_run={manifest['loop_mode_during_run']}")

    pack = manifest["pack"]
    if not pack.get("available"):
        print(f"PACK: MISSING — {pack.get('gap')}")
    else:
        print(
            f"PACK: {pack['harness_clean_count']}/{pack['total_scenarios']} harness-clean, "
            f"domain_floor_failures={len(pack['domain_floor_failures'])}, "
            f"judge_available={pack['judge'].get('available')}, passed={pack['passed']}"
        )

    crit = manifest["critical_x3"]
    if not crit.get("available"):
        print(f"CRITICAL x3: MISSING — {crit.get('gap')}")
    else:
        print(
            f"CRITICAL x3: {crit['all_3_of_3_clean_and_consistent']}/{crit['total_critical_scenarios']} "
            f"clean+consistent, blocked={len(crit['blocked_scenarios'])}, passed={crit['passed']}"
        )

    shadow = manifest["shadow"]
    if not shadow.get("available"):
        print(f"SHADOW: MISSING — {shadow.get('gap')}")
    else:
        print(
            f"SHADOW: distinct_conversations={shadow.get('distinct_conversations')} "
            f"safety_divergences={shadow.get('safety_divergences')} passed={shadow.get('passed')}"
        )

    print(f"teardown_confirmed={manifest['teardown_confirmed']} "
          f"zero_real_send_confirmed={manifest['zero_real_send_confirmed']}")
    print("\nHONESTY CAVEATS (cite these, never omit):")
    for c in manifest["honesty_caveats"]:
        print(f"  - {c}")
    print(f"\nOVERALL GATE PASSED: {manifest['overall_gate_passed']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vt611_evidence_manifest", description=__doc__)
    p.add_argument("--deploy-sha", required=True)
    p.add_argument("--migration-version", required=True)
    p.add_argument("--loop-mode", required=True, help="the TEAM_MANAGER_LOOP_MODE active DURING the run")
    p.add_argument("--pack-summary-json", default=None)
    p.add_argument("--pack-judged", default=None)
    p.add_argument("--critical-summary-json", default=None)
    p.add_argument("--critical-judged", default=None)
    p.add_argument("--shadow-json", default=None)
    p.add_argument("--teardown-confirmed", action="store_true")
    p.add_argument("--zero-real-send-confirmed", action="store_true")
    p.add_argument("--out", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    manifest = build_manifest(
        deploy_sha=args.deploy_sha, migration_version=args.migration_version, loop_mode=args.loop_mode,
        pack_summary=_load_json(args.pack_summary_json), pack_judged=_load_json(args.pack_judged),
        critical_summary=_load_json(args.critical_summary_json), critical_judged=_load_json(args.critical_judged),
        shadow_evidence=_load_json(args.shadow_json),
        teardown_confirmed=args.teardown_confirmed, zero_real_send_confirmed=args.zero_real_send_confirmed,
    )

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    _print_summary(manifest)
    print(f"\nwrote {args.out}")
    return 0 if manifest["overall_gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
