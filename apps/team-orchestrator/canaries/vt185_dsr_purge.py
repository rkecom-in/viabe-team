#!/usr/bin/env python3
"""VT-185 DSR-purge coverage canary (Rule #15, DR-15).

Subshell-source ONLY `.viabe/secrets/supabase-dev.env`:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt185_dsr_purge.py 2>&1 | tee /tmp/vt185-canary-evidence.log | tail -200
    )

**NO anthropic.env sourced.** Deterministic purge substrate;
ANTHROPIC_API_KEY ABSENT at PREFLIGHT.

Wall-clock budget ≤ 45s. Cost budget: 0 paise.

8 assertions:
- A1: full purge → 3 pipeline observability tables emptied for subject
  + per-table audit rows in privacy_audit_log
- A2: dry-run returns correct counts + commits nothing
- A3: cross-tenant safety (tenant_a DSR doesn't touch tenant_b)
- A4: idempotency (re-run = already_completed=True, no extra rows)
- A5: FK-safe deletion order (pipeline_steps deletes before pipeline_runs)
- A6: tenant_id filter applied per delete (no cross-tenant leak)
- A7: audit log row_count column matches actual deletes per table
- A8: ANTHROPIC ABSENT preflight
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANT_IDS: list[str] = []
INSERTED_TICKET_IDS: list[str] = []
INSERTED_RUN_IDS: list[str] = []
INSERTED_TOKEN_IDS: list[str] = []


def assertion(num, name, passed, *, observed=None, expected=None):
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _supabase_host():
    url = os.environ.get("DATABASE_URL", "")
    if "@" not in url:
        return "<no-host>"
    return url.split("@", 1)[1].split("/", 1)[0]


def _preflight():
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL not set", file=sys.stderr)
        sys.exit(2)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "PREFLIGHT FAIL — ANTHROPIC_API_KEY present; this canary must NOT "
            "source anthropic.env (defense-in-depth per DR-15).",
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        f"PREFLIGHT OK — supabase: {_supabase_host()}; "
        "ANTHROPIC_API_KEY: <absent — defense-in-depth>"
    )


def _seed_subject(pool, tenant_id: UUID, *, label: str) -> dict[str, Any]:
    """Seed a tenant + pipeline_run + pipeline_steps + phone_token + dsr_ticket."""
    INSERTED_TENANT_IDS.append(str(tenant_id))
    run_id = uuid4()
    INSERTED_RUN_IDS.append(str(run_id))
    ticket_id = uuid4()
    INSERTED_TICKET_IDS.append(str(ticket_id))
    phone = f"+918888{label[:3]}{uuid4().hex[:5]}"

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-vt185-{label}"),
        )
        cur.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status) "
            "VALUES (%s, %s, 'completed')",
            (str(run_id), str(tenant_id)),
        )
        # Seed 3 pipeline_steps (multi-row delete coverage).
        for seq in (1, 2, 3):
            cur.execute(
                "INSERT INTO pipeline_steps "
                "(run_id, tenant_id, step_seq, step_kind, status) "
                "VALUES (%s, %s, %s, 'canary_step', 'completed')",
                (str(run_id), str(tenant_id), seq),
            )
        # DSR ticket. Schema (migration 010): id, tenant_id, requested_at,
        # request_type ('deletion'|'access'|'correction'), status
        # ('open'|'acknowledged'|'completed'), acknowledged_at, completed_at.
        cur.execute(
            "INSERT INTO dsr_tickets (id, tenant_id, request_type, status) "
            "VALUES (%s, %s, 'deletion', 'open')",
            (str(ticket_id), str(tenant_id)),
        )

    from orchestrator.observability.phone_tokens import register_phone_token
    token = register_phone_token(tenant_id=tenant_id, phone_e164=phone)
    INSERTED_TOKEN_IDS.append(token)

    return {
        "tenant_id": tenant_id,
        "run_id": run_id,
        "ticket_id": ticket_id,
        "token": token,
        "phone": phone,
    }


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt185-canary-salt")

    from orchestrator import graph as graph_mod
    from orchestrator.dsr_purge import (
        purge_tenant_data,
        purge_tenant_data_dry_run,
    )
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=8,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    pool = get_pool()

    # ----------------------------------------------------------------
    # Seed two tenants: A (target) + B (cross-tenant safety check)
    # ----------------------------------------------------------------
    tenant_a = uuid4()
    tenant_b = uuid4()
    seed_a = _seed_subject(pool, tenant_a, label="a")
    seed_b = _seed_subject(pool, tenant_b, label="b")

    def _counts_for_tenant(tid: UUID) -> dict[str, int]:
        counts: dict[str, int] = {}
        with pool.connection() as conn, conn.cursor() as cur:
            for table in ("pipeline_steps", "pipeline_runs", "phone_token_resolutions"):
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE tenant_id = %s",
                    (str(tid),),
                )
                counts[table] = int(cur.fetchone()["n"])
        return counts

    pre_a = _counts_for_tenant(tenant_a)
    pre_b = _counts_for_tenant(tenant_b)

    # ----------------------------------------------------------------
    # A2 — dry-run BEFORE purge (must report > 0 counts, not commit)
    # ----------------------------------------------------------------
    dry = purge_tenant_data_dry_run(seed_a["ticket_id"])
    pre_a_after_dry = _counts_for_tenant(tenant_a)
    pass_2 = (
        dry.deleted_counts.get("pipeline_steps", 0) == 3
        and dry.deleted_counts.get("pipeline_runs", 0) == 1
        and dry.deleted_counts.get("phone_token_resolutions", 0) == 1
        and pre_a_after_dry == pre_a  # no commit
    )
    assertion(
        2,
        "dry-run reports correct counts + commits nothing",
        pass_2,
        observed={
            "dry_counts": {k: dry.deleted_counts.get(k) for k in
                           ("pipeline_steps", "pipeline_runs", "phone_token_resolutions")},
            "counts_before": pre_a,
            "counts_after_dry": pre_a_after_dry,
        },
        expected={
            "pipeline_steps": 3,
            "pipeline_runs": 1,
            "phone_token_resolutions": 1,
            "post_dry_unchanged": True,
        },
    )

    # ----------------------------------------------------------------
    # A1 — real purge → tables empty for subject + per-table audit rows
    # ----------------------------------------------------------------
    result_a = purge_tenant_data(seed_a["ticket_id"])
    post_a = _counts_for_tenant(tenant_a)

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, payload FROM privacy_audit_log "
            "WHERE tenant_id = %s AND payload->>'ticket_id' = %s "
            "ORDER BY event_at",
            (str(tenant_a), str(seed_a["ticket_id"])),
        )
        audit_rows = cur.fetchall()
    per_table_audit = [r for r in audit_rows if r["event_type"] == "subject_data_purged_table"]
    intent_audit = [r for r in audit_rows if r["event_type"] == "subject_data_purged"]

    pass_1 = (
        post_a["pipeline_steps"] == 0
        and post_a["pipeline_runs"] == 0
        and post_a["phone_token_resolutions"] == 0
        and len(intent_audit) == 1
        and len(per_table_audit) >= 3  # at least the 3 pipeline tables
    )
    assertion(
        1,
        "real purge: 3 pipeline tables emptied + 1 intent audit + per-table audit rows",
        pass_1,
        observed={
            "counts_after": post_a,
            "intent_audit_count": len(intent_audit),
            "per_table_audit_count": len(per_table_audit),
            "result_deleted_counts": result_a.deleted_counts,
        },
        expected={
            "counts_after": {"pipeline_steps": 0, "pipeline_runs": 0, "phone_token_resolutions": 0},
            "intent_audit_count": 1,
            "per_table_audit_count_gte": 3,
        },
    )

    # ----------------------------------------------------------------
    # A3 — cross-tenant safety: tenant_b counts unchanged
    # ----------------------------------------------------------------
    post_b = _counts_for_tenant(tenant_b)
    pass_3 = post_b == pre_b
    assertion(
        3,
        "cross-tenant safety: tenant_b counts unchanged after tenant_a DSR purge",
        pass_3,
        observed={"counts_before": pre_b, "counts_after": post_b},
        expected={"counts_after": pre_b},
    )

    # ----------------------------------------------------------------
    # A4 — idempotency: re-run returns already_completed=True; no new rows
    # ----------------------------------------------------------------
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM privacy_audit_log "
            "WHERE tenant_id = %s AND payload->>'ticket_id' = %s",
            (str(tenant_a), str(seed_a["ticket_id"])),
        )
        audit_count_pre_rerun = int(cur.fetchone()["n"])

    result_rerun = purge_tenant_data(seed_a["ticket_id"])

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM privacy_audit_log "
            "WHERE tenant_id = %s AND payload->>'ticket_id' = %s",
            (str(tenant_a), str(seed_a["ticket_id"])),
        )
        audit_count_post_rerun = int(cur.fetchone()["n"])
    pass_4 = (
        result_rerun.already_completed is True
        and audit_count_pre_rerun == audit_count_post_rerun
    )
    assertion(
        4,
        "idempotency: re-run returns already_completed=True + no new audit rows",
        pass_4,
        observed={
            "already_completed": result_rerun.already_completed,
            "audit_pre_rerun": audit_count_pre_rerun,
            "audit_post_rerun": audit_count_post_rerun,
        },
        expected={
            "already_completed": True,
            "audit_count_unchanged": True,
        },
    )

    # ----------------------------------------------------------------
    # A5 — FK-safe deletion order: pipeline_steps audit row BEFORE pipeline_runs
    # ----------------------------------------------------------------
    steps_audit_idx = None
    runs_audit_idx = None
    for idx, r in enumerate(per_table_audit):
        if r["payload"].get("table") == "pipeline_steps" and steps_audit_idx is None:
            steps_audit_idx = idx
        if r["payload"].get("table") == "pipeline_runs" and runs_audit_idx is None:
            runs_audit_idx = idx
    pass_5 = (
        steps_audit_idx is not None
        and runs_audit_idx is not None
        and steps_audit_idx < runs_audit_idx
    )
    assertion(
        5,
        "FK-safe deletion order: pipeline_steps audit row written before pipeline_runs",
        pass_5,
        observed={
            "pipeline_steps_audit_index": steps_audit_idx,
            "pipeline_runs_audit_index": runs_audit_idx,
        },
        expected={"steps_index_lt_runs_index": True},
    )

    # ----------------------------------------------------------------
    # A6 — tenant_id filter: tenant_b rows still exist (already checked in A3
    # but assert specifically that the DSR purge's WHERE tenant_id = tenant_a
    # was the only mutation surface)
    # ----------------------------------------------------------------
    pass_6 = (
        post_b["pipeline_steps"] > 0
        and post_b["pipeline_runs"] > 0
        and post_b["phone_token_resolutions"] > 0
    )
    assertion(
        6,
        "tenant_id filter applied per delete: tenant_b rows survive tenant_a purge",
        pass_6,
        observed={"tenant_b_counts_post_purge": post_b},
        expected={"all_tenant_b_counts_gt_0": True},
    )

    # ----------------------------------------------------------------
    # A7 — audit row_count matches actual deletes
    # ----------------------------------------------------------------
    per_table_match = []
    for r in per_table_audit:
        table = r["payload"].get("table")
        rows_deleted_claim = int(r["payload"].get("rows_deleted", 0))
        actual_deleted = result_a.deleted_counts.get(table, -1)
        per_table_match.append({
            "table": table,
            "audit_rows_deleted": rows_deleted_claim,
            "result_deleted": actual_deleted,
            "match": rows_deleted_claim == actual_deleted,
        })
    pass_7 = all(m["match"] for m in per_table_match) and len(per_table_match) >= 3
    assertion(
        7,
        "per-table audit row_count matches actual deletes",
        pass_7,
        observed={"per_table_match": per_table_match},
        expected={"all_match": True, "per_table_count_gte": 3},
    )

    # ----------------------------------------------------------------
    # A8 — ANTHROPIC ABSENT
    # ----------------------------------------------------------------
    pass_8 = os.environ.get("ANTHROPIC_API_KEY") is None
    assertion(
        8,
        "Zero LLM invariant: ANTHROPIC_API_KEY absent throughout canary execution",
        pass_8,
        observed={"anthropic_api_key_present": os.environ.get("ANTHROPIC_API_KEY") is not None},
        expected={"anthropic_api_key_present": False},
    )

    return _finalise(pool)


def _finalise(pool) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print("\n=== Anthropic cost: 0 paise (deterministic purge substrate canary) ===")

    # Cleanup. Service-role bypasses RLS.
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM privacy_audit_log "
                "WHERE payload->>'ticket_id' = ANY(%s)",
                (INSERTED_TICKET_IDS,),
            )
            cur.execute(
                "DELETE FROM phone_token_resolutions "
                "WHERE phone_token = ANY(%s)",
                (INSERTED_TOKEN_IDS,),
            )
            cur.execute(
                "DELETE FROM pipeline_steps WHERE run_id = ANY(%s)",
                (INSERTED_RUN_IDS,),
            )
            cur.execute(
                "DELETE FROM pipeline_runs WHERE id = ANY(%s)",
                (INSERTED_RUN_IDS,),
            )
            cur.execute(
                "DELETE FROM dsr_tickets WHERE id = ANY(%s)",
                (INSERTED_TICKET_IDS,),
            )
            cur.execute(
                "DELETE FROM tenants WHERE id = ANY(%s)",
                (INSERTED_TENANT_IDS,),
            )
    except BaseException as exc:  # noqa: BLE001
        print(f"cleanup partial: {exc!r}", file=sys.stderr)

    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print("\nALL 8 ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
