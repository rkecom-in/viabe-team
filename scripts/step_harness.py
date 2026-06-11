#!/usr/bin/env python3
"""VT-374 §9 step harness — replay ONE recorded pipeline step. CC/Fazal OPS tool ONLY.

This is dev tooling: clarity over cleverness. It is NEVER a VTR-facing surface — it
reads RAW pipeline_steps envelopes through the service DSN (bypassing the I1
de-identification tier by design, plan §9). Output may therefore contain unredacted
tenant data for step kinds the writers do not redact.

What it does
------------
Reads the pipeline_steps row for (--run-id, --step), resolves the step in the
run-control REGISTRY (key = (workflow_kind, step_name)), and replays it:

* **stub (default)** — NO application code runs. The harness resolves the step's
  implementing callable (see DISPATCH below), prints the recorded input envelope, and
  "invokes" a stub that returns the recorded output envelope verbatim. Safe by
  construction: the only side effect is the read.
* **--live** — actually imports and calls the step's implementing function with
  arguments rebuilt from the recorded input envelope. Refused for:
    - steps registered ``pause_deny=True`` (I6 compliance paths — opt-out/DSR);
    - steps whose implementing module is in the gate manifest (F14 — send/consent/
      approval gates are structurally non-replayable);
    - steps with no live adapter in DISPATCH (mid-workflow state that cannot be
      reconstructed from an envelope — use the /rerun ops API instead);
    - any DB whose VT-362 ``app_environment`` sentinel is not exactly 'dev'
      (same sentinel ``apply_migrations.guard_environment`` enforces; the harness
      additionally refuses a MISSING sentinel — it never stamps one).

Why DISPATCH lives here and not in registry.py
----------------------------------------------
The REGISTRY (orchestrator/run_control/registry.py) deliberately carries no code
references — it is declarative substrate consumed by the executor and the ops API.
The (workflow_kind, step_name) -> module:callable mapping is a HARNESS concern, so it
is maintained here as a local DISPATCH table, with the seam evidence documented in
comments (file:line from the VT-374 STEP-0 report). Drift between REGISTRY and
DISPATCH is reported at lookup time and by --list.

inputs_redacted_at_write
------------------------
When the registry marks a step ``inputs_redacted_at_write=True``, the stored input
envelope was PII-redacted BEFORE it hit the table. Replaying it is unrepresentative
for any step that consumes the redacted fields — the harness prints a loud stderr
warning either mode.

Usage
-----
Run from the orchestrator venv (the live path imports orchestrator modules)::

    cd apps/team-orchestrator
    uv run python ../../scripts/step_harness.py --list
    uv run python ../../scripts/step_harness.py --run-id <uuid> --step deliver_parts
    uv run python ../../scripts/step_harness.py --run-id <uuid> --step deliver_parts \
        --live --pin version=3

Requires DATABASE_URL (or TEAM_SUPABASE_DB_URL) — a direct Postgres DSN with a
service/BYPASSRLS role (pipeline_steps is FORCE-RLS; a tenant-scoped role sees nothing).

Exit codes: 0 ok · 2 usage · 3 refusal (live gates / sentinel) · 4 row/registry lookup.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

# step_harness.py -> scripts -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[1]
_ORCH_SRC = _REPO_ROOT / "apps" / "team-orchestrator" / "src"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_NOT_FOUND = 4


class HarnessError(RuntimeError):
    """Lookup / data problem — exit 4."""


class HarnessRefusal(RuntimeError):
    """A --live gate refused the replay — exit 3."""


# ---------------------------------------------------------------------------
# run-control imports (lazy: sys.path bootstrap + a clear error if the package
# is missing — the harness must not crash with a bare ImportError mid-build).
# ---------------------------------------------------------------------------


def _load_run_control() -> tuple[dict[tuple[str, str], Any], frozenset[str]]:
    """Return (REGISTRY, GATE_MODULES) from orchestrator.run_control."""
    if str(_ORCH_SRC) not in sys.path:
        sys.path.insert(0, str(_ORCH_SRC))
    try:
        from orchestrator.run_control.gate_manifest import GATE_MODULES
        from orchestrator.run_control.registry import REGISTRY
    except ImportError as exc:
        raise HarnessError(
            "cannot import orchestrator.run_control (registry/gate_manifest): "
            f"{exc}. Run from the orchestrator venv (uv run) with the VT-374 "
            "run_control package present."
        ) from exc
    return REGISTRY, GATE_MODULES


# ---------------------------------------------------------------------------
# DISPATCH — (workflow_kind, step_name) -> implementing callable + live adapter.
#
# ``target`` is the dotted "module:callable" the registered step executes; the
# module part is what the gate-manifest refusal checks. ``build_call`` rebuilds
# (args, kwargs) from the recorded row + (pin-merged) input envelope; None means
# live replay is structurally unsupported — the stub path still works.
# Seam evidence: .viabe/queue/VT-374/step0-report.md §3.1.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchEntry:
    target: str
    build_call: (
        Callable[["StepRow", dict[str, Any]], tuple[list[Any], dict[str, Any]]] | None
    )
    note: str


@dataclass(frozen=True)
class StepRow:
    id: str
    run_id: str
    tenant_id: str
    step_seq: int
    step_kind: str | None
    step_name: str | None
    status: str
    input_envelope: dict[str, Any]
    output_envelope: dict[str, Any] | None


def _args_auto_discovery(row: StepRow, env: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    # auto_discovery_run(tenant_id, seed) — seed from the recorded envelope (or --pin seed=...).
    return [str(row.tenant_id), dict(env.get("seed") or {})], {}


def _args_plan_generate(row: StepRow, env: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    # generate_business_plan_workflow(tenant_id: str). plan_exists guard applies — an
    # existing plan makes the replay a recorded skip, which is correct harness behavior.
    return [str(row.tenant_id)], {}


def _args_plan_deliver(row: StepRow, env: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    # deliver_plan(tenant_id, version). Sends a REAL owner WhatsApp burst — dev DB only
    # (sentinel-gated); the delivered_parts bitmap makes the replay resume-safe.
    version = env.get("version")
    if version is None:
        raise HarnessError(
            "recorded envelope lacks 'version' — supply it: --pin version=<n>"
        )
    return [str(row.tenant_id), int(version)], {}


def _args_trial_evaluate(row: StepRow, env: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    # evaluate_trial(tenant_id, now) — pure verdict computation; the sweep body (NOT the
    # harness) is what applies transitions/notifies, so this live replay is side-effect-free.
    return [UUID(str(row.tenant_id)), datetime.now(timezone.utc)], {}


def _args_ingestion_pull(row: StepRow, env: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    # ingest_one_connector(tenant_id: UUID, connector_id: str).
    connector_id = env.get("connector_id")
    if not connector_id:
        raise HarnessError(
            "recorded envelope lacks 'connector_id' — supply it: --pin connector_id=<id>"
        )
    return [UUID(str(row.tenant_id)), str(connector_id)], {}


DISPATCH: dict[tuple[str, str], DispatchEntry] = {
    # runner.py:591-598 — pause-ONLY seam (N3); the brain consumes mid-workflow LangGraph
    # state that an envelope cannot reconstruct. Live replay of an inbound is forbidden
    # anyway (I8: webhook_pipeline_run is forbidden-on-rerun — MessageSid ledger semantics).
    ("webhook_inbound", "dispatch_brain"): DispatchEntry(
        target="orchestrator.agent.dispatch:dispatch_brain",
        build_call=None,
        note="pause-only seam; mid-workflow state — stub replay only",
    ),
    # journey.py:304 — STEP-0 demotion: observed tier (owner-inbound hot path, fail-open
    # except). Never a live-replay target.
    ("webhook_inbound", "question_brain_compose"): DispatchEntry(
        target="orchestrator.onboarding.journey:maybe_handle_journey_reply",
        build_call=None,
        note="observed tier (STEP-0 demotion) — stub replay only",
    ),
    # coordinator.py:470 — impl.execute_item(ctx) needs a WorkItemContext + claimed work
    # item; reconstruction from an envelope would skip the claim/CAS machinery. Re-runs
    # go through the /rerun ops API (app-level re-dispatch), not the harness.
    ("agent_dispatch", "execute_item"): DispatchEntry(
        target="orchestrator.agents.coordinator:agent_dispatch_workflow",
        build_call=None,
        note="needs claimed work-item ctx — use POST /rerun; stub replay only",
    ),
    # sales_recovery_executor.py:487 — detection needs an open tenant_connection kwarg
    # and feeds the in-flight executor phases; not reconstructable standalone.
    ("agent_dispatch", "candidate_build"): DispatchEntry(
        target="orchestrator.agents.sales_recovery_executor:detect_lapsed_customers",
        build_call=None,
        note="mid-phase (needs tenant conn + downstream phases) — stub replay only",
    ),
    # sales_recovery_executor.py:508 — the LLM draft phase is inline in execute_item
    # (no standalone callable); module:qualname kept for display/gate-check only.
    ("agent_dispatch", "compose_drafts"): DispatchEntry(
        target="orchestrator.agents.sales_recovery_executor:execute_item",
        build_call=None,
        note="inline LLM phase inside execute_item — stub replay only",
    ),
    # sales_recovery_executor.py:530 — persist needs the drafted batch in memory; a
    # live replay would mint an unarmed batch (violates the _cancel_batch invariant).
    ("agent_dispatch", "persist_batch"): DispatchEntry(
        target="orchestrator.agents.sales_recovery_executor:_persist_draft_batch",
        build_call=None,
        note="persist-before-arm invariant — stub replay only",
    ),
    # auto_discovery.py:68 — reuse-safe per I8 (ON CONFLICT draft merge upsert); replay
    # re-spends the Apify/Haiku ceiling (~$0.018) against the dev project.
    ("auto_discovery", "source_fetch"): DispatchEntry(
        target="orchestrator.onboarding.auto_discovery:auto_discovery_run",
        build_call=_args_auto_discovery,
        note="live re-runs all sources for the tenant (cost ceiling applies)",
    ),
    # generator.py:362 — reuse-safe per I8 (plan_exists guard; FOR UPDATE serializes).
    ("plan_generate", "generate_validate"): DispatchEntry(
        target="orchestrator.business_plan.generator:generate_business_plan_workflow",
        build_call=_args_plan_generate,
        note="live replay skips when a plan already exists (guard is the point)",
    ),
    # generator.py:375 / delivery.py:135-144 — delivered_parts bitmap keyed
    # (tenant, version, part) makes the replay resume-only.
    ("plan_deliver", "deliver_parts"): DispatchEntry(
        target="orchestrator.business_plan.delivery:deliver_plan",
        build_call=_args_plan_deliver,
        note="live replay sends REAL owner WhatsApp parts (bitmap-resumed; dev only)",
    ),
    # trial_sweep.py:127 — the evaluator (not the sweep body) is the per-tenant unit;
    # verdict-only, no transition applied.
    ("trial_sweep", "evaluate_tenant"): DispatchEntry(
        target="orchestrator.billing.trial_evaluator:evaluate_trial",
        build_call=_args_trial_evaluate,
        note="live replay computes the verdict only — never applies/notifies",
    ),
    # scheduler.py:143 — single-connector pull; the live recurring-connector path.
    ("ingestion", "connector_pull"): DispatchEntry(
        target="orchestrator.integrations.scheduler:ingest_one_connector",
        build_call=_args_ingestion_pull,
        note="live replay pulls one connector for the tenant",
    ),
    # supervisor.py:200-214 (VT-300 seam, migrating to workflow_controls in this row) —
    # fan-out consumes graph state and sends to customers; idempotency-keyed, but the
    # harness is not a send surface.
    ("campaign_send", "execute_fanout"): DispatchEntry(
        target="orchestrator.supervisor:_campaign_execute_node",
        build_call=None,
        note="graph-state arg + customer sends — stub replay only",
    ),
}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _resolve_dsn() -> str:
    """Mirror apply_migrations.resolve_dsn: DATABASE_URL preferred."""
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")
    if not dsn:
        raise HarnessError(
            "set DATABASE_URL or TEAM_SUPABASE_DB_URL (a direct Postgres DSN, "
            "not the Supabase REST URL)"
        )
    return dsn


def _assert_dev_sentinel(conn: psycopg.Connection) -> None:
    """VT-362 guard, read-only variant of apply_migrations.guard_environment.

    --live proceeds ONLY against a DB whose app_environment sentinel is exactly
    'dev'. Unlike the migration runner there is NO bootstrap arm: an absent
    sentinel refuses (the harness can never prove — let alone stamp — identity).
    """
    reg = conn.execute("SELECT to_regclass('public.app_environment')").fetchone()
    if reg is None or reg["to_regclass"] is None:
        raise HarnessRefusal(
            "--live: connected DB has NO app_environment sentinel (VT-362) — cannot "
            "prove this is dev; refusing. Bootstrap the sentinel via apply_migrations "
            "--expected-env dev; the harness never stamps."
        )
    rows = conn.execute("SELECT name FROM app_environment").fetchall()
    if len(rows) != 1:
        raise HarnessRefusal(
            f"--live: app_environment must hold exactly one row, found {len(rows)} — "
            "refusing (tampered sentinel)."
        )
    actual = rows[0]["name"]
    if actual != "dev":
        raise HarnessRefusal(
            f"--live: connected DB is stamped '{actual}', not 'dev' — refusing "
            "(VT-362; the harness NEVER replays against a non-dev database)."
        )
    print("  env-guard: sentinel 'dev' confirmed for --live ✓", file=sys.stderr)


def _fetch_step_row(
    conn: psycopg.Connection, run_id: str, step: str, seq: int | None
) -> StepRow:
    """Latest pipeline_steps row for run_id whose step_name OR step_kind == step."""
    sql = (
        "SELECT id, run_id, tenant_id, step_seq, step_kind, step_name, status, "
        "input_envelope, output_envelope FROM pipeline_steps "
        "WHERE run_id = %s AND (step_name = %s OR step_kind = %s)"
    )
    params: list[Any] = [run_id, step, step]
    if seq is not None:
        sql += " AND step_seq = %s"
        params.append(seq)
    sql += " ORDER BY step_seq DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise HarnessError(
            f"no pipeline_steps row for run_id={run_id} step={step!r}"
            + (f" seq={seq}" if seq is not None else "")
        )
    return StepRow(
        id=str(row["id"]),
        run_id=str(row["run_id"]),
        tenant_id=str(row["tenant_id"]),
        step_seq=int(row["step_seq"]),
        step_kind=row["step_kind"],
        step_name=row["step_name"],
        status=row["status"],
        input_envelope=dict(row["input_envelope"] or {}),
        output_envelope=(
            dict(row["output_envelope"]) if row["output_envelope"] is not None else None
        ),
    )


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def _warn_redacted(workflow_kind: str, step_name: str) -> None:
    bar = "!" * 78
    print(
        f"\n{bar}\n"
        f"!! WARNING: ({workflow_kind}, {step_name}) is registered "
        "inputs_redacted_at_write=True.\n"
        "!! The recorded input envelope was PII-REDACTED before storage. This replay\n"
        "!! is UNREPRESENTATIVE for any step that consumes the redacted fields.\n"
        f"{bar}\n",
        file=sys.stderr,
    )


def _refuse_live_gates(
    entry: Any, dispatch: DispatchEntry, gate_modules: frozenset[str]
) -> None:
    """The --live refusal gates, checked BEFORE any DB connection."""
    key = f"({entry.workflow_kind}, {entry.step_name})"
    if entry.pause_deny:
        raise HarnessRefusal(
            f"--live: {key} is pause_deny (I6 compliance path — opt-out/DSR class); "
            "live replay is forbidden."
        )
    module = dispatch.target.partition(":")[0]
    if module in gate_modules:
        raise HarnessRefusal(
            f"--live: {key} resolves to gate-manifest module '{module}' (F14 — "
            "send/consent/approval gates are never replayable)."
        )
    if dispatch.build_call is None:
        raise HarnessRefusal(f"--live: {key} has no live adapter — {dispatch.note}.")


def _invoke_live(dispatch: DispatchEntry, row: StepRow, env: dict[str, Any]) -> Any:
    assert dispatch.build_call is not None  # guarded by _refuse_live_gates
    args, kwargs = dispatch.build_call(row, env)
    module_name, _, qualname = dispatch.target.partition(":")
    module = importlib.import_module(module_name)
    fn: Any = module
    for part in qualname.split("."):
        fn = getattr(fn, part)
    print(
        f"  live: calling {dispatch.target} args={args!r} kwargs={kwargs!r}",
        file=sys.stderr,
    )
    return fn(*args, **kwargs)


def _parse_pins(pairs: list[str]) -> dict[str, Any]:
    """--pin key=value pairs; values JSON-parsed when possible, else raw strings.

    A dev convenience to fill/override envelope keys for the LOCAL replay only —
    the production override mechanism is step_overrides, not this flag.
    """
    pins: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise HarnessError(f"--pin expects key=value, got {pair!r}")
        try:
            pins[key] = json.loads(raw)
        except ValueError:
            pins[key] = raw
    return pins


def _print_drift(registry: dict[tuple[str, str], Any]) -> None:
    """Report REGISTRY <-> DISPATCH drift (parallel-build / future-row safety net)."""
    missing = sorted(set(registry) - set(DISPATCH))
    extra = sorted(set(DISPATCH) - set(registry))
    for key in missing:
        print(f"  drift: REGISTRY has {key} but DISPATCH does not — add it here", file=sys.stderr)
    for key in extra:
        print(f"  drift: DISPATCH has {key} but REGISTRY does not — stale entry?", file=sys.stderr)


def _list_steps(registry: dict[tuple[str, str], Any]) -> None:
    print(f"{'workflow_kind':<18} {'step_name':<24} {'tier':<13} {'live':<5} target")
    for key in sorted(DISPATCH):
        dispatch = DISPATCH[key]
        entry = registry.get(key)
        tier = getattr(entry, "tier", "?") if entry else "MISSING"
        live = "yes" if dispatch.build_call is not None else "no"
        print(f"{key[0]:<18} {key[1]:<24} {tier:<13} {live:<5} {dispatch.target}")
        print(f"{'':<62} {dispatch.note}")
    _print_drift(registry)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="step_harness",
        description=(
            "Replay one recorded pipeline step (VT-374 §9). Stub by default; "
            "--live actually re-invokes (dev DB only, gated)."
        ),
    )
    parser.add_argument("--run-id", help="pipeline_runs.id of the recorded run")
    parser.add_argument("--step", help="step_name (or step_kind) of the recorded step")
    parser.add_argument(
        "--workflow-kind",
        help="registry workflow_kind; inferred when --step is unique across kinds",
    )
    parser.add_argument("--seq", type=int, help="exact step_seq (default: latest match)")
    parser.add_argument(
        "--live",
        action="store_true",
        help="actually re-invoke the step function (refuses gate/pause_deny steps "
        "and any DB not sentinel-stamped 'dev')",
    )
    parser.add_argument(
        "--pin",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="fill/override an input-envelope key for THIS replay (JSON value if parseable)",
    )
    parser.add_argument(
        "--list", action="store_true", help="print the step table + drift report and exit"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        registry, gate_modules = _load_run_control()

        if args.list:
            _list_steps(registry)
            return EXIT_OK

        if not args.run_id or not args.step:
            print("step_harness: --run-id and --step are required (or --list)", file=sys.stderr)
            return EXIT_USAGE

        # Resolve the registry key. workflow_kind is inferable when the step name is
        # unambiguous across kinds; otherwise the caller must disambiguate.
        kind = args.workflow_kind
        if kind is None:
            candidates = sorted({k for (k, s) in registry if s == args.step})
            if len(candidates) != 1:
                print(
                    f"step_harness: --workflow-kind required; step {args.step!r} matches "
                    f"kinds {candidates or '(none)'}",
                    file=sys.stderr,
                )
                return EXIT_USAGE
            kind = candidates[0]

        entry = registry.get((kind, args.step))
        if entry is None:
            raise HarnessError(
                f"({kind}, {args.step}) is not in the run-control REGISTRY — "
                "run --list for the known steps"
            )
        dispatch = DISPATCH.get((kind, args.step))
        if dispatch is None:
            _print_drift(registry)
            raise HarnessError(
                f"({kind}, {args.step}) is registered but has no DISPATCH entry — "
                "add the module:callable mapping to scripts/step_harness.py"
            )

        if args.live:
            _refuse_live_gates(entry, dispatch, gate_modules)

        with psycopg.connect(_resolve_dsn(), autocommit=True, row_factory=dict_row) as conn:
            if args.live:
                _assert_dev_sentinel(conn)
            row = _fetch_step_row(conn, args.run_id, args.step, args.seq)

        if getattr(entry, "inputs_redacted_at_write", False):
            _warn_redacted(kind, args.step)

        pins = _parse_pins(args.pin)
        env = dict(row.input_envelope)
        env.update(pins)

        if args.live:
            replayed: Any = _invoke_live(dispatch, row, env)
            mode = "live"
        else:
            # Stub replay: a stand-in returning the recorded output envelope verbatim.
            # No application code runs — the value is seeing exactly what the step saw
            # and produced, through the same resolution path --live would take.
            replayed = row.output_envelope
            mode = "stub"

        doc = {
            "mode": mode,
            "workflow_kind": kind,
            "step_name": args.step,
            "tier": getattr(entry, "tier", None),
            "pause_deny": getattr(entry, "pause_deny", None),
            "inputs_redacted_at_write": getattr(entry, "inputs_redacted_at_write", None),
            "target": dispatch.target,
            "run_id": row.run_id,
            "tenant_id": row.tenant_id,
            "step_seq": row.step_seq,
            "step_kind": row.step_kind,
            "status": row.status,
            "pins_applied": sorted(pins),
            "recorded_input": row.input_envelope,
            "recorded_output": row.output_envelope,
            "replayed_output": replayed,
        }
        print(json.dumps(doc, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    except HarnessRefusal as exc:
        print(f"step_harness REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except HarnessError as exc:
        print(f"step_harness: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND


if __name__ == "__main__":
    raise SystemExit(main())
