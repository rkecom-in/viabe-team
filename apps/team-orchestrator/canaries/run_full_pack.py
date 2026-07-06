"""VT-611 — full-pack runner: every scenario in canaries/scenarios/*.json, ONE run each.

Complements ``run_critical_x3.py`` (which runs ONLY the flagged-``critical`` scenarios, 3x each,
for the cross-run-consistency gate) — this tool runs the FULL scenario pack exactly once each, on
a fresh harness tenant per scenario (teardown after, unless ``--keep-tenants``), and writes the
``--json-report`` bundle ``canaries/transcript_judge.py`` judges.

Also enforces the two GATE DEFINITION checks that don't need the judge model:
  - domain floors (manager>=40 / onboarding>=25 / integration>=25 / sr_autonomy_rails>=30) —
    mechanical, from the scenario's own ``domain`` field. A shortfall is an authoring gap, never
    silently reclassified.
  - harness-clean count: every step must be PASS or XFAIL (a FAIL/XPASS/TIMEOUT step is a finding).
    ``expected_fail`` scenarios are NOT excluded from this count (GATE DEFINITION) — they must
    resolve XFAIL or XPASS like any other scenario, never silently dropped.

Diagnostic-first (Fazal/team-lead RUN directive, 2026-07-06): this tool does NOT stop on the first
failure. It runs every scenario, collects every result, and reports the complete picture — a
failing scenario is a FINDING for the evidence manifest (a real manager gap, or a scenario bug),
not a reason to abort the run. A non-zero exit on the FIRST run is expected; that is the point of
running it before enforce promotion.

Usage (on deployed dev):

    railway run --service vt-orchestrator-service --environment development -- \\
        uv run --directory apps/team-orchestrator python canaries/run_full_pack.py \\
        [--scenarios-dir canaries/scenarios] [--only NAME] [--ingress-url URL] [--timeout S] \\
        [--keep-tenants] [--json-report PATH]

Exits 0 only when every domain floor is met AND every scenario is harness-clean.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

_CANARIES = Path(__file__).resolve().parent
sys.path.insert(0, str(_CANARIES))  # allow `import convo_harness` regardless of caller's cwd

import convo_harness as ch  # noqa: E402 — after the sys.path insert

DOMAIN_FLOORS: dict[str, int] = {
    "manager": 40, "onboarding": 25, "integration": 25, "sr_autonomy_rails": 30,
}


# --- pure functions (unit-tested; no DB/network) -------------------------------------------------


def discover_all_scenarios(scenarios_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Every ``*.json`` under ``scenarios_dir``, sorted by filename for a stable, reproducible run
    order — unlike ``run_critical_x3.discover_critical_scenarios``, no ``critical`` filter."""
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(scenarios_dir.glob("*.json")):
        out.append((path, json.loads(path.read_text(encoding="utf-8"))))
    return out


def check_domain_floors(pairs: list[tuple[Path, dict[str, Any]]]) -> list[str]:
    """GATE DEFINITION's mechanical domain-floor check, over whatever the pack ACTUALLY contains
    (never a hardcoded expectation of the total) — an author shortfall is reported, never papered
    over by reclassifying a scenario's domain."""
    counts: dict[str, int] = {}
    for _path, scenario in pairs:
        domain = str(scenario.get("domain", "?"))
        counts[domain] = counts.get(domain, 0) + 1
    failures: list[str] = []
    for domain, floor in DOMAIN_FLOORS.items():
        actual = counts.get(domain, 0)
        if actual < floor:
            failures.append(f"domain floor MISSED: {domain}={actual} < {floor}")
    return failures


def build_pack_summary(
    pairs: list[tuple[Path, dict[str, Any]]], per_scenario: list[dict[str, Any]],
) -> dict[str, Any]:
    """JSON-serializable pack-level summary — persists what ``main()`` previously only PRINTED
    (domain counts/floor gaps + each scenario's clean/finding verdict). The evidence manifest can't
    quote stdout, so this is what it cites for domain floors + the harness-clean count."""
    domain_counts: dict[str, int] = {}
    for _path, scenario in pairs:
        domain = str(scenario.get("domain", "?"))
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
    return {
        "domain_counts": domain_counts,
        "domain_floors": DOMAIN_FLOORS,
        "domain_floor_failures": check_domain_floors(pairs),
        "scenarios": per_scenario,
    }


def check_harness_clean(results: list[ch.StepResult]) -> list[str]:
    """Every step in this (single) run must be PASS or XFAIL — mirrors
    ``run_critical_x3.check_all_3_clean``'s per-run logic, named for the single-run context this
    tool runs in (no ×3 here; that cross-run consistency gate lives in ``run_critical_x3.py``)."""
    bad = [r for r in results if r.label not in ("PASS", "XFAIL")]
    if not bad:
        return []
    return [f"{len(bad)} step(s) did not clear PASS/XFAIL ({', '.join(r.label for r in bad)})"]


# --- orchestration (real DB/HTTP; not unit-tested directly — the logic above is) ------------------


def _setup_tenant(setup_args: list[Any], *, ingress_url: str | None, run_label: str) -> str:
    """Provision one fresh harness tenant via the REAL ``convo_harness setup`` CLI parser
    (in-process, no subprocess) — same reuse pattern as ``run_critical_x3._setup_tenant``."""
    parser = ch.build_parser()
    argv = [
        "setup", *[str(a) for a in setup_args],
        "--name", f"convo-harness-pack-{run_label}-{uuid.uuid4().hex[:8]}",
    ]
    if ingress_url:
        argv += ["--ingress-url", ingress_url]
    ns = parser.parse_args(argv)
    ch.cmd_setup(ns)
    return str(ns.tenant_id)


def _teardown_tenant(tenant_id: str) -> None:
    ch.cmd_teardown(argparse.Namespace(tenant_id=tenant_id))


def run_one_scenario(
    path: Path, scenario: dict[str, Any], *,
    ingress_url: str | None, timeout: float, keep_tenants: bool,
) -> tuple[str, list[ch.StepResult]]:
    dsn = ch._dsn()
    base = ch._ingress_base(ingress_url)
    secret = ch._dev_secret()
    name = str(scenario.get("name", path.stem))
    setup_args = scenario.get("setup_args", [])
    scenario_xfail = bool(scenario.get("expected_fail", False))
    steps = scenario.get("steps", [])

    tenant_id = _setup_tenant(setup_args, ingress_url=ingress_url, run_label=name)
    try:
        results = ch.run_scenario_steps(
            dsn, base, secret, tenant_id, steps, timeout=timeout, scenario_xfail=scenario_xfail,
        )
    finally:
        if not keep_tenants:
            _teardown_tenant(tenant_id)
    return tenant_id, results


def _write_json_report(
    path: str, path_stem: str, scenario: dict[str, Any], tenant_id: str, results: list[ch.StepResult],
) -> None:
    steps = scenario.get("steps", [])
    summary = {
        "passed": sum(1 for r in results if r.label == "PASS"),
        "xfailed": sum(1 for r in results if r.label == "XFAIL"),
        "xpassed": sum(1 for r in results if r.label == "XPASS"),
        "failed": sum(1 for r in results if r.label == "FAIL"),
        "timed_out": sum(1 for r in results if r.label == "TIMEOUT"),
    }
    entry = ch._build_json_report(scenario, path_stem, tenant_id, steps, results, summary)
    ch._append_json_report(path, entry)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="run_full_pack", description=__doc__)
    p.add_argument("--scenarios-dir", default=str(_CANARIES / "scenarios"))
    p.add_argument("--only", default=None, help="run only the named scenario (debug)")
    p.add_argument("--ingress-url", default=None, help="deployed dev orchestrator base URL")
    p.add_argument("--timeout", type=float, default=90.0)
    p.add_argument(
        "--keep-tenants", action="store_true",
        help="skip teardown (debug — inspect the synthetic tenants after the run)",
    )
    p.add_argument("--json-report", default=None, help="bundle path for transcript_judge.py")
    p.add_argument(
        "--summary-json", default=None,
        help="write domain counts/floor gaps + each scenario's clean/finding verdict — for the "
             "VT-611 evidence manifest, which can't quote this tool's stdout",
    )
    args = p.parse_args(argv)

    scenarios_dir = Path(args.scenarios_dir)
    pairs = discover_all_scenarios(scenarios_dir)
    floor_failures = check_domain_floors(pairs)
    if args.only:
        pairs = [(path, s) for path, s in pairs if s.get("name") == args.only]
        if not pairs:
            print(f"run_full_pack: no scenario named {args.only!r}", file=sys.stderr)
            return 2

    print(f"=== VT-611 full pack: {len(pairs)} scenario(s) ===")
    for f in floor_failures:
        print(f"  DOMAIN FLOOR: {f}")

    findings: list[str] = []
    per_scenario: list[dict[str, Any]] = []
    for path, scenario in pairs:
        name = str(scenario.get("name", path.stem))
        print(f"\n--- {name} ---")
        tenant_id, results = run_one_scenario(
            path, scenario, ingress_url=args.ingress_url, timeout=args.timeout,
            keep_tenants=args.keep_tenants,
        )
        bad = check_harness_clean(results)
        if bad:
            findings.append(f"{name}: {'; '.join(bad)}")
            print(f"    FINDING — {'; '.join(bad)}")
        else:
            print("    clean (every step PASS or XFAIL)")
        per_scenario.append({
            "name": name, "domain": scenario.get("domain"), "tenant_id": tenant_id,
            "clean": not bad, "block_reasons": bad,
        })
        if args.json_report:
            _write_json_report(args.json_report, str(path), scenario, tenant_id, results)

    print(
        f"\n=== summary: {len(pairs)} scenario(s), {len(findings)} finding(s), "
        f"{len(floor_failures)} domain-floor gap(s) ==="
    )
    for f in findings:
        print(f"  - {f}")
    for f in floor_failures:
        print(f"  - {f}")
    if args.json_report:
        print(f"    json-report: {args.json_report} — feed into transcript_judge.py next")
    if args.summary_json:
        summary = build_pack_summary(pairs, per_scenario)
        with open(args.summary_json, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"    summary-json: wrote {args.summary_json} — for the evidence manifest")

    return 0 if not findings and not floor_failures else 1


if __name__ == "__main__":
    sys.exit(main())
