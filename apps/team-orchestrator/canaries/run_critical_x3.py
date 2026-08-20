"""VT-611 Package C — critical ×3 tooling (gate remediation B8/B9).

Runs EVERY scenario flagged ``critical: true`` in ``canaries/scenarios/*.json`` THREE TIMES EACH
(a fresh harness tenant per run — zero state carries between runs) and BLOCKS unless all three:

  (1) are individually harness-clean — every step PASS or XFAIL (never FAIL/XPASS/TIMEOUT). A
      critical scenario failing even 1-of-3 is a BLOCK, not a flake: an intermittent safety
      failure is a defect (B8).
  (2) are CONSISTENT with each other — the same DB-observed route (Package H1's
      ``[internal route: ...]`` signal), the same grounded cohort count (when a campaign exists),
      and the same terminal outcome (the last step's ``run_status``) across all 3 runs. A
      scenario that behaves differently run-to-run is flagged even when each run individually
      "passes" (B9) — "8"/"a handful"/"~10" across 3 runs is exactly the class this exists to
      catch, not judge-score variance.

Runs ALL flagged-critical scenarios (never an arbitrary "30" — the flagged count is whatever the
pack currently carries; VT-611.md's own "58" is stale as of the 122-scenario pack, see
canaries/convo_harness.py's Package H1 note history). If cost ever forces a cap, pass
``--only NAME`` per an EXPLICIT named allowlist rather than truncating silently.

Two-gate composition (mirrors transcript_judge.py's own architecture): this tool is the
DETERMINISTIC gate — hard harness asserts + cross-run consistency, in code. It does NOT itself
call the judge model. It writes a ``--json-report`` bundle (uniquified per-run scenario names,
``"<name> [run N/3]"``) in the SAME shape ``convo_harness.py script --json-report`` produces —
feed that bundle straight into ``canaries/transcript_judge.py`` for the qualitative verdicts
("record all 3 transcript hashes + all 3 judge verdicts" = this tool's hashes + that tool's
verdicts, both landing in the same evidence manifest).

Usage (on deployed dev):

    railway run --service vt-orchestrator-service --environment development -- \\
        uv run --directory apps/team-orchestrator python canaries/run_critical_x3.py \\
        [--scenarios-dir canaries/scenarios] [--only NAME] [--ingress-url URL] [--timeout S] \\
        [--keep-tenants] [--json-report PATH]

Exits 0 only if EVERY critical scenario is 3/3-clean AND cross-run-consistent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CANARIES = Path(__file__).resolve().parent
sys.path.insert(0, str(_CANARIES))  # allow `import convo_harness` regardless of caller's cwd

import convo_harness as ch  # noqa: E402 — after the sys.path insert


@dataclass
class RunObservation:
    """One of the 3 runs of one critical scenario."""

    scenario_name: str
    run_index: int  # 1, 2, 3
    tenant_id: str
    results: list[ch.StepResult]
    route: str  # the LAST step's DB-observed route ("sales_recovery" | "none")
    grounded_count: int | None  # cohort_size for that route's campaign, or None if no campaign
    terminal_outcome: str | None  # the LAST step's run_status
    transcript_hash: str
    # VT-728: set when the orchestrator redeployed mid-scenario (see deployed_version).
    contaminated: bool = False
    #: The deployed app version this run measured — the key --resume merges (or refuses) on.
    app_version: str | None = None


# --- pure functions (unit-tested; no DB/network) -------------------------------------------------


def discover_critical_scenarios(scenarios_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Every ``*.json`` under ``scenarios_dir`` with a truthy ``critical`` field, sorted by
    filename for a stable, reproducible run order."""
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(scenarios_dir.glob("*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        if scenario.get("critical"):
            out.append((path, scenario))
    return out


def transcript_hash(results: list[ch.StepResult]) -> str:
    """A stable sha256 over every turn's role+text across every step. INFORMATIONAL only — a
    benign wording difference between two LLM calls is expected and is NOT itself a failure; see
    ``check_cross_run_consistency`` for the signals that actually gate a divergence."""
    blob = "\n".join(f"{t.role}:{t.text}" for r in results for t in r.transcript)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def last_run_id(results: list[ch.StepResult]) -> str | None:
    """The most recent non-None run_id across the scenario's steps (a step whose turn never
    started, e.g. an ingress rejection, carries no run_id)."""
    for r in reversed(results):
        rid = r.run_id
        if rid is not None:
            return str(rid)
    return None


def check_all_3_clean(results: list[ch.StepResult]) -> list[str]:
    """B8: every step in ONE run must be PASS or XFAIL. Any FAIL/XPASS/TIMEOUT step blocks this
    run outright (an intermittent safety failure is a defect, not a flake — never averaged away)."""
    bad = [r for r in results if r.label not in ("PASS", "XFAIL")]
    if not bad:
        return []
    return [f"{len(bad)} step(s) did not clear PASS/XFAIL ({', '.join(r.label for r in bad)})"]


def check_cross_run_consistency(observations: list[RunObservation]) -> list[str]:
    """B9: group the 3 runs of the SAME scenario; require IDENTICAL route, grounded_count, and
    terminal_outcome across all of them. Any divergence blocks that scenario independent of any
    per-run PASS/XFAIL verdict — this is what catches "3/3 individually green but the manager
    actually did something different each time" (the arbitrary-N-of-3 flake class B9 exists for)."""
    failures: list[str] = []
    by_scenario: dict[str, list[RunObservation]] = {}
    for obs in observations:
        by_scenario.setdefault(obs.scenario_name, []).append(obs)
    for name, runs in by_scenario.items():
        routes = {r.route for r in runs}
        counts = {r.grounded_count for r in runs}
        outcomes = {r.terminal_outcome for r in runs}
        if len(routes) > 1:
            failures.append(
                f"{name}: route diverged across {len(runs)} runs: {[r.route for r in runs]!r}"
            )
        if len(counts) > 1:
            failures.append(
                f"{name}: grounded_count diverged across {len(runs)} runs: "
                f"{[r.grounded_count for r in runs]!r}"
            )
        if len(outcomes) > 1:
            failures.append(
                f"{name}: terminal_outcome diverged across {len(runs)} runs: "
                f"{[r.terminal_outcome for r in runs]!r}"
            )
    return failures


def build_run_summary(scenario_name: str, observations: list[RunObservation]) -> dict[str, Any]:
    """One scenario's ×3 result, JSON-serializable — persists what ``main()`` previously only
    PRINTED (route/grounded_count/terminal_outcome/transcript_hash per run + the cross-run
    consistency verdict). Needed for the VT-611 evidence manifest to cite the actual ×3 results,
    not just this tool's exit code — a manifest can't quote stdout."""
    runs = []
    for o in observations:
        bad = check_all_3_clean(o.results)
        runs.append({
            "run_index": o.run_index,
            "tenant_id": o.tenant_id,
            "route": o.route,
            "grounded_count": o.grounded_count,
            "terminal_outcome": o.terminal_outcome,
            "transcript_hash": o.transcript_hash,
            "clean": not bad,
            "block_reasons": bad,
        })
    consistency_failures = check_cross_run_consistency(observations)
    return {
        "scenario": scenario_name,
        "runs": runs,
        "consistent": not consistency_failures,
        "consistency_failures": consistency_failures,
    }


def observe_route_and_grounded_count(
    conn: Any, tenant_id: str, run_id: str | None
) -> tuple[str, int | None]:
    """The DB-observed route + grounded cohort_size (if any) for the run that produced a
    scenario's LAST step. Reuses convo_harness.py's own Package H1 helpers — single source of
    truth, no text-parsing of the transcript's ``[internal route: ...]`` marker."""
    if run_id is None:
        return "none", None
    route = ch._observed_route(conn, tenant_id, run_id)
    campaign_id = ch._campaign_id_for_run(conn, tenant_id, run_id)
    if campaign_id is None:
        return route, None
    row = conn.execute(
        "SELECT plan_json FROM campaigns WHERE tenant_id = %s AND id = %s", (tenant_id, campaign_id)
    ).fetchone()
    if row is None:
        return route, None
    plan_json = row[0] if not isinstance(row, dict) else row["plan_json"]
    cohort_size = (plan_json or {}).get("target_cohort", {}).get("cohort_size")
    return route, cohort_size


# --- orchestration (real DB/HTTP; not unit-tested directly — the logic above is) ------------------


def _setup_tenant(setup_args: list[Any], *, ingress_url: str | None, run_label: str) -> str:
    """Provision one fresh harness tenant by feeding the scenario's OWN ``setup_args`` through the
    REAL ``convo_harness setup`` CLI parser (in-process, no subprocess) — reuses 100% of the
    existing setup logic (onboarding state, --flow sentinel, --seed-lapsed-customers substrate,
    etc.) rather than re-deriving it here."""
    parser = ch.build_parser()
    argv = [
        "setup", *[str(a) for a in setup_args],
        "--name", f"convo-harness-x3-{run_label}-{uuid.uuid4().hex[:8]}",
    ]
    if ingress_url:
        argv += ["--ingress-url", ingress_url]
    ns = parser.parse_args(argv)
    ch.cmd_setup(ns)
    return str(ns.tenant_id)


def _teardown_tenant(tenant_id: str) -> None:
    ch.cmd_teardown(argparse.Namespace(tenant_id=tenant_id))


def deployed_version(dsn: str) -> str | None:
    """VT-728 — the orchestrator's currently-deployed DBOS application version, or None.

    Railway's NATIVE auto-deploy fires on EVERY push to dev — including docs-only pushes, which the
    VT-245 CI trigger-diet deliberately skips. A skipped CI run does NOT skip the deploy, so a
    harmless-looking `docs(sprint): …` push restarts the orchestrator mid-measurement, and the
    scenarios in flight report TIMEOUT / `terminal=running` / an unobserved route. Those read as
    product defects and are not: they are the measurement being cut in half.

    DBOS writes one `dbos.application_versions` row per deployed version, so comparing this value
    before and after a run tells us whether we measured one service or two.
    """
    try:
        import re as _re

        import psycopg as _psycopg

        sysdsn = _re.sub(r"/([^/?]+)(\?|$)", r"/postgres_dbos_sys\2", dsn, count=1)
        with _psycopg.connect(sysdsn, connect_timeout=10) as sc:
            row = sc.execute(
                "SELECT version_name FROM dbos.application_versions "
                "ORDER BY version_timestamp DESC LIMIT 1"
            ).fetchone()
        return None if row is None else str(row[0])
    except Exception:  # noqa: BLE001 — the guard must never fail a run; unknown version => no claim
        return None


def run_scenario_x3(
    path: Path, scenario: dict[str, Any], *,
    ingress_url: str | None, timeout: float, keep_tenants: bool,
) -> list[RunObservation]:
    dsn = ch._dsn()
    base = ch._ingress_base(ingress_url)
    secret = ch._dev_secret()
    name = str(scenario.get("name", path.stem))
    setup_args = scenario.get("setup_args", [])
    scenario_xfail = bool(scenario.get("expected_fail", False))
    steps = scenario.get("steps", [])

    version_before = deployed_version(dsn)

    observations: list[RunObservation] = []
    for i in range(1, 4):
        tenant_id = _setup_tenant(setup_args, ingress_url=ingress_url, run_label=f"{name}-{i}")
        try:
            results = ch.run_scenario_steps(
                dsn, base, secret, tenant_id, steps, timeout=timeout, scenario_xfail=scenario_xfail,
            )
            run_id = last_run_id(results)
            with ch._connect(dsn) as conn:
                route, grounded_count = observe_route_and_grounded_count(conn, tenant_id, run_id)
            terminal_outcome = results[-1].run_status if results else None
            observations.append(RunObservation(
                scenario_name=name, run_index=i, tenant_id=tenant_id, results=results,
                route=route, grounded_count=grounded_count, terminal_outcome=terminal_outcome,
                transcript_hash=transcript_hash(results),
                app_version=version_before,
            ))
        finally:
            if not keep_tenants:
                _teardown_tenant(tenant_id)
            else:
                # VT-738 — the dump MUST precede the quiesce. `_quiesce_kept_tenant` flips every
                # non-terminal manager_task to 'cancelled', and it excludes 'blocked' but NOT
                # 'running'. A step still 'running' is exactly what distinguishes the loop's
                # default-to-escalate (M1) from a dispatch that simply had not finished — so
                # quiescing first would erase the one signal this whole re-drive exists to read,
                # and it would erase it silently.
                _assert_kept_tenant_is_synthetic(dsn, tenant_id)
                _dump_forensics(dsn, tenant_id, name, i)
                _quiesce_kept_tenant(dsn, tenant_id)

    # VT-728 — CONTAMINATION CHECK. A redeploy mid-run means these observations describe two
    # different services. Say so loudly: a contaminated run must never be read as a product result,
    # in either direction (a false failure wastes a debugging cycle; a false pass is worse).
    version_after = deployed_version(dsn)
    if version_before and version_after and version_before != version_after:
        print(
            f"    !! CONTAMINATED: the orchestrator REDEPLOYED during this scenario "
            f"({version_before[:12]}… -> {version_after[:12]}…). TIMEOUT / terminal=running / "
            f"unobserved-route results below are measurement artifacts, NOT product behavior. "
            f"Re-run on a stable service before drawing any conclusion.",
        )
        for obs in observations:
            obs.contaminated = True
    return observations


#: VT-738 — the tables that answer "why did this turn not delegate". Ordered so the decision
#: trail reads top-down: what triage decided, what the route was, what the task did, what ran.
_FORENSIC_QUERIES: dict[str, str] = {
    "tm_audit_log": (
        "SELECT event_kind, actor, summary, decision, created_at FROM tm_audit_log "
        "WHERE tenant_id = %(t)s ORDER BY created_at"
    ),
    "manager_tasks": (
        "SELECT id, status, terminal_outcome, source_message_ref, created_at, updated_at "
        "FROM manager_tasks WHERE tenant_id = %(t)s ORDER BY created_at"
    ),
    # Column names verified against information_schema on dev 2026-08-10 — the first cut guessed
    # `attempt` (it is `step_seq`/`version`) and `pipeline_runs.created_at` (it is `started_at`),
    # and both queries silently lost their table until the dump reported the UndefinedColumn.
    "manager_task_steps": (
        "SELECT id, task_id, step_seq, kind, status, specialist, version, created_at, updated_at "
        "FROM manager_task_steps WHERE tenant_id = %(t)s ORDER BY created_at"
    ),
    "pipeline_runs": (
        # run_type='manager_dispatch' + status='escalated' is the M1 signature; final_outcome and
        # error_summary carry the rest of the why.
        "SELECT id, run_type, status, final_outcome, error_summary, started_at, ended_at "
        "FROM pipeline_runs WHERE tenant_id = %(t)s ORDER BY started_at"
    ),
    "campaigns": (
        "SELECT id, run_id, status, created_at FROM campaigns "
        "WHERE tenant_id = %(t)s ORDER BY created_at"
    ),
    "incidents": (
        "SELECT id, incident_kind, severity, detail, created_at FROM incidents "
        "WHERE tenant_id = %(t)s ORDER BY created_at"
    ),
}


def _dump_forensics(dsn: str, tenant_id: str, scenario_name: str, run_index: int) -> None:
    """Persist the decision trail for one kept tenant BEFORE anything mutates it.

    The gate's tenants are reaped an hour after the run (`_REAP_AGE_HOURS = 1`), which is why the
    last delegation investigation had to be done by reading transcripts and guessing: the rows that
    would have answered it were already gone. This writes them to disk while they still exist.

    Fail-soft, and deliberately so — a forensics failure must not fail a 40-minute measurement. But
    it prints what it lost rather than skipping quietly, because a dump that silently wrote nothing
    would send the next person back to guessing from transcripts again.
    """
    out_dir = _CANARIES / "_reports" / "vt738_forensics"
    payload: dict[str, Any] = {
        "scenario": scenario_name, "run_index": run_index, "tenant_id": tenant_id,
    }
    try:
        import psycopg as _psycopg

        out_dir.mkdir(parents=True, exist_ok=True)
        with _psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            for table, sql in _FORENSIC_QUERIES.items():
                try:
                    rows = conn.execute(sql, {"t": tenant_id}).fetchall()
                    cols = [d[0] for d in (conn.execute(sql, {"t": tenant_id}).description or [])]
                    payload[table] = [dict(zip(cols, r, strict=False)) for r in rows]
                except Exception as exc:  # noqa: BLE001 — one missing table must not lose the rest
                    payload[table] = {"error": type(exc).__name__}
                    print(f"    [forensics] {table} failed ({type(exc).__name__})")
        path = out_dir / f"{scenario_name}-{run_index}-{tenant_id[:8]}.json"
        path.write_text(json.dumps(payload, indent=1, default=str))
        counts = {k: len(v) for k, v in payload.items() if isinstance(v, list)}
        print(f"    [forensics] {path.name}: {counts}")
    except Exception as exc:  # noqa: BLE001 — never fail the measurement on hygiene
        print(f"    [forensics] DUMP FAILED for {tenant_id[:8]}… ({type(exc).__name__}) — "
              "this tenant's decision trail is NOT recoverable after the reaper")


def _assert_kept_tenant_is_synthetic(dsn: str, tenant_id: str) -> None:
    """Refuse to leave a REAL-number tenant alive. Clau's ask, and it is not theoretical: dev has
    `TEAM_TWILIO_MOCK_MODE` off, so dev sends reach real WhatsApp. `--keep-tenants` skips the
    teardown that would otherwise stop a tenant, so a mis-targeted run could leave something live
    that can message a real person. The harness mints its own synthetic tenants, so this should
    never fire — which is exactly why it should be loud if it ever does.

    Raises rather than warning: keeping a live-number tenant alive is not a hygiene issue.
    """
    import psycopg as _psycopg

    protected = {v for v in (os.environ.get("FAZAL_TENANT_ID"),) if v}
    if tenant_id in protected:
        raise SystemExit(
            f"VT-738 REFUSING --keep-tenants on {tenant_id[:8]}…: that is a Fazal-owned tenant"
        )
    with _psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        row = conn.execute(
            "SELECT business_name FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    name = str(row[0]) if row else ""
    if not name.startswith("convo-harness-"):
        raise SystemExit(
            f"VT-738 REFUSING --keep-tenants on {tenant_id[:8]}… (business_name={name!r}): only "
            "harness-minted tenants may be left alive, because dev sends real WhatsApp"
        )


def _quiesce_kept_tenant(dsn: str, tenant_id: str) -> None:
    """--keep-tenants keeps the DATA for forensics but must not keep the LIVENESS: an active
    manager_task with an unanswered approval sits with no runnable step until the VT-557/560
    reaper flips it 'blocked' and fires an orphaned_task WARNING to the ops Telegram (Fazal got
    paged by exactly this, 2026-07-11). Same recipe as teardown's #53 cancel: flip the tenant's
    PENDING/ENQUEUED manager_task workflows to CANCELLED on the DBOS system DB (recovery skips
    CANCELLED), then settle its still-active manager_tasks rows 'cancelled'. Data untouched.
    Fail-soft: a quiesce failure must never fail the measurement run."""
    try:
        import re as _re

        import psycopg as _psycopg

        sysdsn = _re.sub(r"/([^/?]+)(\?|$)", r"/postgres_dbos_sys\2", dsn, count=1)
        with _psycopg.connect(sysdsn, autocommit=True, connect_timeout=10) as sc:
            n_wf = sc.execute(
                "UPDATE dbos.workflow_status SET status = 'CANCELLED' "
                "WHERE workflow_uuid LIKE %s AND status IN ('PENDING', 'ENQUEUED')",
                (f"manager_task:{tenant_id}:%",),
            ).rowcount
        with _psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            n_tasks = conn.execute(
                "UPDATE manager_tasks SET status = 'cancelled', terminal_outcome = 'cancelled', "
                "updated_at = now() WHERE tenant_id = %s "
                "AND status NOT IN ('completed', 'cancelled', 'blocked', 'rejected', 'failed')",
                (tenant_id,),
            ).rowcount
        if n_wf or n_tasks:
            print(
                f"    [keep-tenants] quiesced tenant {tenant_id[:8]}…: "
                f"{n_wf} workflow(s) cancelled, {n_tasks} task(s) settled"
            )
    except Exception as exc:  # noqa: BLE001 — quiesce is hygiene; never fail the run on it
        print(f"    [keep-tenants] quiesce skipped ({type(exc).__name__})")


def _write_json_report(path: str, path_stem: str, scenario: dict[str, Any], obs: RunObservation) -> None:
    """Append one run's bundle entry — SAME shape ``convo_harness.py script --json-report``
    produces (reuses its ``_build_json_report``/``_append_json_report``), with the scenario name
    uniquified per run so ``transcript_judge.py`` (fed this same bundle downstream) scores each of
    the 3 runs as its own entry rather than colliding on one shared name."""
    uniquified = dict(scenario)
    uniquified["name"] = f"{scenario.get('name', path_stem)} [run {obs.run_index}/3]"
    steps = scenario.get("steps", [])
    summary = {
        "passed": sum(1 for r in obs.results if r.label == "PASS"),
        "xfailed": sum(1 for r in obs.results if r.label == "XFAIL"),
        "xpassed": sum(1 for r in obs.results if r.label == "XPASS"),
        "failed": sum(1 for r in obs.results if r.label == "FAIL"),
        "timed_out": sum(1 for r in obs.results if r.label == "TIMEOUT"),
    }
    entry = ch._build_json_report(uniquified, path_stem, obs.tenant_id, steps, obs.results, summary)
    entry["transcript_hash"] = obs.transcript_hash
    entry["run_index"] = obs.run_index
    # VT-729b — stamp the SERVICE this run measured. The bundle is append-only, so without this a
    # later attempt's entries sit indistinguishably beside an earlier one's: reading 186 entries as
    # "62 of 79 scenarios" is exactly the arithmetic that produced a wrong completion figure in a
    # brief. --resume uses this to decide what is genuinely done, and to refuse to merge segments
    # that measured different code.
    entry["app_version"] = obs.app_version
    ch._append_json_report(path, entry)


def completed_scenarios(report_path: str, current_version: str | None) -> tuple[set[str], list[str]]:
    """VT-729b — scenarios a prior segment already finished, and the ones deliberately NOT reused.

    A scenario counts as done only when the bundle holds THREE entries for it, every one clean, and
    every one stamped with the SAME app version the service is running now. Anything else re-runs.

    The version equality is the load-bearing part. A resumed pack straddles time, so if the service
    redeployed between segments the segments describe different services — the same disease the
    in-run contamination guard catches, one level up. Rather than merge and annotate, this refuses
    to reuse those scenarios at all: a merged number that reads as one clean run is precisely the
    green nobody traced.

    Returns ``(reusable_scenario_names, skipped_reasons)``.
    """
    try:
        with open(report_path, encoding="utf-8") as fh:
            entries = json.load(fh)
    except Exception:  # noqa: BLE001 — no readable prior bundle → nothing to resume, run everything
        return set(), []

    by_scenario: dict[str, list[dict[str, Any]]] = {}
    for entry in entries if isinstance(entries, list) else []:
        raw = str(entry.get("name", ""))
        base = raw.split(" [run ", 1)[0]
        by_scenario.setdefault(base, []).append(entry)

    reusable: set[str] = set()
    skipped: list[str] = []
    for name, rows in by_scenario.items():
        indices = {r.get("run_index") for r in rows if r.get("run_index")}
        if indices != {1, 2, 3}:
            skipped.append(f"{name}: only runs {sorted(i for i in indices if i)} recorded")
            continue
        versions = {r.get("app_version") for r in rows}
        if versions != {current_version} or current_version is None:
            skipped.append(
                f"{name}: measured on a different service version ({sorted(str(v)[:12] for v in versions)})"
            )
            continue
        summaries = [r.get("summary") or {} for r in rows]
        if any(s.get("failed") or s.get("timed_out") or s.get("xpassed") for s in summaries):
            skipped.append(f"{name}: prior segment was not clean")
            continue
        reusable.add(name)
    return reusable, skipped


# The step deadline MUST stay above the product's own maximum in-turn wait, or the harness measures
# its own deadline instead of the system. ``runner._D1_INTURN_WAIT_MAX_POLLS × _D1_INTURN_WAIT_POLL_S``
# is ~96s: when a turn starts an async manager_task, the sync run deliberately waits that long for the
# task's real reply to land IN-TURN (T9 inc-3 raised it from 15s precisely so a fast task answers in
# ONE beat instead of "I'm on it" + a delayed reply). A 90s deadline sits UNDER that floor, so any turn
# whose task does not answer quickly is recorded TIMEOUT by construction — which is exactly how
# sr_consequential_bulk_send_requires_approval read as a defect: step 1 always "timed out", while the
# same turn driven in isolation at a longer window produced a campaign draft + approval request in one
# beat, with zero customer messages sent. 180s = the 96s wait + the turn's own work + headroom.
_DEFAULT_STEP_TIMEOUT_S = 180.0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="run_critical_x3", description=__doc__)
    p.add_argument("--scenarios-dir", default=str(_CANARIES / "scenarios"))
    p.add_argument(
        "--only",
        default=None,
        help="comma-separated EXPLICIT allowlist of critical scenario names to run. The module "
             "docstring already called for an allowlist rather than silent truncation; this accepts "
             "the list it describes, so a targeted re-gate (e.g. the scenarios a settlement fix "
             "touches) is one run instead of N runs whose results have to be stitched by hand.",
    )
    p.add_argument("--ingress-url", default=None, help="deployed dev orchestrator base URL")
    p.add_argument("--timeout", type=float, default=_DEFAULT_STEP_TIMEOUT_S)
    p.add_argument(
        "--keep-tenants", action="store_true",
        help="skip teardown (debug — inspect the synthetic tenants after the run)",
    )
    p.add_argument("--json-report", default=None, help="bundle path for transcript_judge.py")
    p.add_argument(
        "--resume", action="store_true",
        help="skip scenarios the --json-report bundle already records as 3/3 clean ON THE SAME "
             "deployed app version. A ten-hour serial run has now been lost twice to environmental "
             "faults (a session stop, a DNS failure); this turns that into losing the scenario that "
             "was in flight. Refuses to reuse anything measured on a different service version.",
    )
    p.add_argument(
        "--summary-json", default=None,
        help="write the persisted per-run route/grounded_count/terminal_outcome/transcript_hash + "
             "cross-run-consistency verdict (JSON list, one entry per scenario) — for the VT-611 "
             "evidence manifest, which can't quote this tool's stdout",
    )
    args = p.parse_args(argv)

    scenarios_dir = Path(args.scenarios_dir)
    pairs = discover_critical_scenarios(scenarios_dir)
    if args.only:
        wanted = [n.strip() for n in args.only.split(",") if n.strip()]
        pairs = [(path, s) for path, s in pairs if s.get("name") in wanted]
        found = {s.get("name") for _p, s in pairs}
        # A typo'd name must not silently shrink a targeted re-gate into a smaller one that then
        # reports "all clean" — the missing scenario is exactly the one you meant to re-prove.
        missing = [n for n in wanted if n not in found]
        if missing:
            print(
                f"run_critical_x3: no critical scenario named {missing!r} "
                f"(matched {sorted(found)})",
                file=sys.stderr,
            )
            return 2

    resumed_names: set[str] = set()
    if args.resume:
        if not args.json_report:
            print("run_critical_x3: --resume requires --json-report", file=sys.stderr)
            return 2
        current = deployed_version(ch._dsn())
        resumed_names, skipped_reasons = completed_scenarios(args.json_report, current)
        print(f"=== RESUMED RUN — reusing {len(resumed_names)} scenario(s) from a prior segment ===")
        print(f"    current app version: {str(current)[:12]}…")
        for reason in skipped_reasons:
            print(f"    re-running {reason}")
        pairs = [(path, s) for path, s in pairs if str(s.get("name", path.stem)) not in resumed_names]

    print(f"=== VT-611 Package C: {len(pairs)} critical scenario(s), ×3 each ===")

    blocked: list[str] = []
    contaminated: list[str] = []
    summaries: list[dict[str, Any]] = []
    for path, scenario in pairs:
        name = str(scenario.get("name", path.stem))
        print(f"\n--- {name} ---")
        obs = run_scenario_x3(
            path, scenario, ingress_url=args.ingress_url, timeout=args.timeout,
            keep_tenants=args.keep_tenants,
        )
        for o in obs:
            bad = check_all_3_clean(o.results)
            if bad and o.contaminated:
                # VT-728: the service redeployed mid-run. Report it as CONTAMINATED, never as a
                # BLOCK — a measurement artifact recorded as a product defect sends the next
                # session chasing a bug that was never there. It still exits non-zero.
                contaminated.append(f"{name} run {o.run_index}/3: {'; '.join(bad)}")
                print(f"    run {o.run_index}/3: CONTAMINATED (redeploy mid-run) — {'; '.join(bad)}")
            elif bad:
                blocked.append(f"{name} run {o.run_index}/3: {'; '.join(bad)}")
                print(f"    run {o.run_index}/3: BLOCK — {'; '.join(bad)}")
            else:
                print(
                    f"    run {o.run_index}/3: clean (route={o.route}, "
                    f"grounded_count={o.grounded_count}, terminal={o.terminal_outcome})"
                )
            if args.json_report:
                _write_json_report(args.json_report, str(path), scenario, o)

        consistency_failures = check_cross_run_consistency(obs)
        for f in consistency_failures:
            # A redeploy mid-scenario makes cross-run divergence expected, not informative.
            if any(o.contaminated for o in obs):
                contaminated.append(f)
                print(f"    CROSS-RUN DIVERGENCE (CONTAMINATED — redeploy mid-run): {f}")
            else:
                blocked.append(f)
                print(f"    CROSS-RUN DIVERGENCE: {f}")
        summaries.append(build_run_summary(name, obs))

    if resumed_names:
        print(
            f"\n!! RESUMED RUN: {len(resumed_names)} scenario(s) came from a PRIOR segment and were "
            f"not re-driven here. This is not a single continuous pack; report it as resumed."
        )
    print(
        f"\n=== summary: {len(pairs)} critical scenario(s), {len(blocked)} block(s)"
        + (f", {len(contaminated)} CONTAMINATED ===" if contaminated else " ===")
    )
    for b in blocked:
        print(f"  - {b}")
    if contaminated:
        print(
            "  !! the orchestrator redeployed mid-run. These are measurement artifacts, not "
            "product results — re-run on a stable service:"
        )
        for c in contaminated:
            print(f"  - [contaminated] {c}")
    if args.json_report:
        print(f"    json-report: appended to {args.json_report} — feed into transcript_judge.py next")
    if args.summary_json:
        with open(args.summary_json, "w", encoding="utf-8") as fh:
            json.dump(summaries, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"    summary-json: wrote {args.summary_json} — for the evidence manifest")

    return 0 if not (blocked or contaminated) else 1


if __name__ == "__main__":
    sys.exit(main())
