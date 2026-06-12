#!/usr/bin/env python3
"""VT-379 — one-shot backfill: redact historical pipeline_steps free-text columns.

The ``pipeline_steps.error`` column was never redacted at write (the three
direct-INSERT writers — ``error_router._log_decision`` /
``sales_recovery._emit_self_evaluate_gate`` / ``collapse.record_terminal_verdict``
— bypassed the redacting writer), and was never swept by Detector-5. VT-379
closes the write path AND extends Detector-5; this script closes the third leg:
EXISTING rows written before the fix still carry raw exception strings /
verbatim model output that can contain phones, names, bodies.

What it redacts (pattern-redaction + a per-tenant customer-name registry):
  * ``error`` (jsonb) on EVERY row — the universal blind spot (any writer).
  * ``decision_rationale`` (text) on EVERY row — error_router writes the raw
    ``failure_type -> strategy`` rationale there; cheap to co-redact.
  * ``output_envelope`` (jsonb) ONLY for the three writers' cheaply-identifiable
    ``step_kind`` values — ``error`` (error_router: metadata.dropped_values +
    failure.message), ``self_evaluate_gate`` (feedback_messages free text),
    ``campaign_plan_emitted`` (out_of_scope_reason / missing_data free text).
    Other kinds' envelopes already flowed through the redacting writer (VT-104),
    so re-redacting them is unnecessary AND risks corrupting already-tokenised
    content — scoped by step_kind keeps the blast radius to the known offenders.

Idempotent: the canonical redactor is idempotent by construction
(``redact(redact(x)) == redact(x)``), and each row is UPDATEd ONLY when the
redacted JSON/text differs from what is stored. A second ``--execute`` run
therefore reports zero updates.

Env-guarded (VT-362 / CL-431): refuses unless the connected DB's
``app_environment`` sentinel matches ``--expected-env`` — reuses
``apply_migrations.guard_environment`` (one env-guard source). NEVER prints a
DSN/password or any redacted/raw content (CL-390): reports COUNTS ONLY.

Dry-run is the DEFAULT. ``--execute`` is required to write.

Usage::

    # count what would change, write nothing (default):
    python apps/team-orchestrator/scripts/backfill_redact_error_column.py \
        --expected-env dev

    # actually redact:
    python apps/team-orchestrator/scripts/backfill_redact_error_column.py \
        --expected-env dev --execute

Service-role / RLS-bypassed by design: it must read+rewrite raw rows across
every tenant, the same elevated-path justification as ``dsr_purge`` (this is a
controller/operator data-migration action, not a tenant client write). The
explicit ``WHERE id = %s`` per-row UPDATE keeps the scope tight.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

import psycopg

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# ``error`` is the universal blind spot — redact it on EVERY step_kind. The
# three writers' output_envelope free text is redacted ONLY on these kinds
# (cheaply identifiable; other kinds' envelopes were redacted at write).
_ENVELOPE_REDACT_KINDS: frozenset[str] = frozenset(
    {"error", "self_evaluate_gate", "campaign_plan_emitted"}
)

_BATCH_SIZE_DEFAULT = 500


def _build_registry(
    conn: psycopg.Connection, tenant_id: str
) -> Callable[[str], bool]:
    """Per-tenant customer-name registry predicate, read service-role.

    Mirrors ``customer_registry.make_name_registry`` (case-folded exact match)
    but reads on THIS service-role connection so the backfill needs no RLS GUC
    dance — it already runs RLS-bypassed to sweep every tenant. ``customers``
    table absent (forward-compat) → an always-False predicate, never raises.
    """
    names: set[str] = set()
    row = conn.execute("SELECT to_regclass('public.customers')").fetchone()
    if row and row[0] is not None:
        rows = conn.execute(
            "SELECT display_name FROM customers "
            "WHERE tenant_id = %s AND display_name IS NOT NULL",
            (tenant_id,),
        ).fetchall()
        names = {str(r[0]).casefold() for r in rows if r[0]}

    def _predicate(text: str) -> bool:
        return text.casefold() in names

    return _predicate


def _redact_jsonb(
    value: Any, name_registry: Callable[[str], bool]
) -> Any:
    """Redact a jsonb value (already a python dict/list from psycopg)."""
    from orchestrator.observability.pii import redact_for_log

    return redact_for_log(value, name_registry=name_registry)


def _redact_text(
    value: str, name_registry: Callable[[str], bool]
) -> str:
    """Redact a plain-text column through the same canonical redactor."""
    from orchestrator.observability.pii import redact_for_log

    return redact_for_log(value, name_registry=name_registry)


def run(
    *,
    dsn: str,
    expected_env: str,
    execute: bool,
    batch_size: int = _BATCH_SIZE_DEFAULT,
    expected_host_substr: str | None = None,
) -> dict[str, int]:
    """Scan + (optionally) redact historical pipeline_steps free-text columns.

    Returns COUNTS ONLY (CL-390): scanned / would-change-or-changed per column.
    Never returns or prints redacted content.
    """
    import apply_migrations  # reuse the ONE env-guard source (VT-362)
    from psycopg.types.json import Jsonb

    scanned = 0
    error_changed = 0
    rationale_changed = 0
    envelope_changed = 0
    rows_changed = 0

    with psycopg.connect(dsn, autocommit=False) as conn:
        # VT-362 env-guard FIRST — refuse on any sentinel mismatch before reading.
        apply_migrations.guard_environment(
            conn, dsn, expected_env, expected_host_substr
        )

        # Cache the per-tenant registry across the run (tenant count is bounded).
        registry_cache: dict[str, Callable[[str], bool]] = {}

        def _registry_for(tenant_id: str) -> Callable[[str], bool]:
            if tenant_id not in registry_cache:
                registry_cache[tenant_id] = _build_registry(conn, tenant_id)
            return registry_cache[tenant_id]

        # Keyset pagination on the PK so a large table streams in bounded memory
        # and an in-flight UPDATE never disturbs the scan cursor.
        last_id: str | None = None
        while True:
            if last_id is None:
                cur = conn.execute(
                    "SELECT id, tenant_id, step_kind, error, decision_rationale, "
                    "output_envelope "
                    "FROM pipeline_steps ORDER BY id LIMIT %s",
                    (batch_size,),
                )
            else:
                cur = conn.execute(
                    "SELECT id, tenant_id, step_kind, error, decision_rationale, "
                    "output_envelope "
                    "FROM pipeline_steps WHERE id > %s ORDER BY id LIMIT %s",
                    (last_id, batch_size),
                )
            rows = cur.fetchall()
            if not rows:
                break

            for r in rows:
                (
                    step_id,
                    tenant_id,
                    step_kind,
                    error_val,
                    rationale_val,
                    envelope_val,
                ) = r
                last_id = str(step_id)
                scanned += 1
                reg = _registry_for(str(tenant_id))

                set_fragments: list[str] = []
                params: list[Any] = []
                row_touched = False

                # error (jsonb) — universal blind spot, every kind.
                if error_val is not None:
                    new_error = _redact_jsonb(error_val, reg)
                    if json.dumps(new_error, sort_keys=True) != json.dumps(
                        error_val, sort_keys=True
                    ):
                        error_changed += 1
                        row_touched = True
                        set_fragments.append("error = %s")
                        params.append(Jsonb(new_error))

                # decision_rationale (text) — error_router raw rationale.
                if rationale_val is not None:
                    new_rationale = _redact_text(str(rationale_val), reg)
                    if new_rationale != rationale_val:
                        rationale_changed += 1
                        row_touched = True
                        set_fragments.append("decision_rationale = %s")
                        params.append(new_rationale)

                # output_envelope (jsonb) — ONLY the three identifiable kinds.
                if (
                    envelope_val is not None
                    and step_kind in _ENVELOPE_REDACT_KINDS
                ):
                    new_envelope = _redact_jsonb(envelope_val, reg)
                    if json.dumps(new_envelope, sort_keys=True) != json.dumps(
                        envelope_val, sort_keys=True
                    ):
                        envelope_changed += 1
                        row_touched = True
                        set_fragments.append("output_envelope = %s")
                        params.append(Jsonb(new_envelope))

                if row_touched:
                    rows_changed += 1
                    if execute:
                        params.append(str(step_id))
                        conn.execute(
                            f"UPDATE pipeline_steps SET {', '.join(set_fragments)} "
                            "WHERE id = %s",  # noqa: S608 — fragments are module-fixed
                            tuple(params),
                        )

        if execute:
            conn.commit()
        else:
            conn.rollback()

    return {
        "scanned": scanned,
        "rows_changed": rows_changed,
        "error_changed": error_changed,
        "decision_rationale_changed": rationale_changed,
        "output_envelope_changed": envelope_changed,
    }


def main() -> int:  # pragma: no cover — CLI wrapper
    parser = argparse.ArgumentParser(
        description=(
            "VT-379 backfill: redact historical pipeline_steps.error (+ "
            "decision_rationale + the three writers' output_envelope free text)."
        )
    )
    parser.add_argument(
        "--expected-env",
        default=os.environ.get("EXPECTED_ENV"),
        help="{dev|prod} — must match the app_environment sentinel (VT-362).",
    )
    parser.add_argument(
        "--expected-host-substr",
        default=os.environ.get("EXPECTED_HOST_SUBSTR"),
        help=(
            "Bootstrap-only: a non-secret host substring proving DB identity "
            "when no sentinel exists yet."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the redaction. WITHOUT this flag the script is a dry-run.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_BATCH_SIZE_DEFAULT,
        help=f"Keyset page size (default {_BATCH_SIZE_DEFAULT}).",
    )
    args = parser.parse_args()

    if not args.expected_env:
        print(
            "backfill_redact_error_column: --expected-env {dev|prod} is REQUIRED "
            "(or the EXPECTED_ENV env var). Refusing (VT-362 env guard).",
            file=sys.stderr,
        )
        return 2

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    counts = run(
        dsn=dsn,
        expected_env=args.expected_env,
        execute=args.execute,
        batch_size=args.batch_size,
        expected_host_substr=args.expected_host_substr,
    )

    # COUNTS ONLY — never the redacted content (CL-390).
    print(
        json.dumps(
            {
                "mode": "execute" if args.execute else "dry_run",
                "expected_env": args.expected_env,
                **counts,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
