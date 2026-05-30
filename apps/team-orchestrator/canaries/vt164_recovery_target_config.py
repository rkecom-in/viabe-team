#!/usr/bin/env python3
"""VT-164 per-tenant recovery-target config canary (Rule #15, DR-15).

Subshell-source ONLY `.viabe/secrets/supabase-dev.env`:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt164_recovery_target_config.py 2>&1 | tee /tmp/vt164-canary-evidence.log | tail -60
    )

**NO anthropic.env sourced.** Defense-in-depth: ANTHROPIC_API_KEY must be
ABSENT (structural Pillar 1 enforcement). Zero LLM calls in this canary.

CL-422: SYNTHETIC tenant only. Dev DB is Seoul (ap-northeast-2), accepted
for synthetic data; no real customer data. Cleanup deletes the synthetic row.
CL-390: log tenant_id (UUID) + numeric config only, no PII.

Wall-clock budget <= 60s. Cost budget: 0 paise.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANT_IDS: list[str] = []


def assertion(num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _supabase_host() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if "@" not in url:
        return "<no-host>"
    return url.split("@", 1)[1].split("/", 1)[0]


def _preflight() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL not set", file=sys.stderr)
        sys.exit(2)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "PREFLIGHT FAIL — ANTHROPIC_API_KEY present; this canary must NOT "
            "source anthropic.env (Pillar 1 structural enforcement)",
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        f"PREFLIGHT OK — supabase: {_supabase_host()}; "
        f"ANTHROPIC_API_KEY: <absent — defense-in-depth>; "
        f"region target: aws-1-ap-northeast-2 (CL-422)"
    )


def _seed_tenant(pool, tenant_id: UUID) -> None:
    """Insert a synthetic tenant (CL-422: SYNTHETIC only, no real data)."""
    INSERTED_TENANT_IDS.append(str(tenant_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'founding', 'trial') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"VT164 Synthetic Canary {tenant_id}"),
        )


def run_canary() -> int:
    _preflight()
    start = time.monotonic()

    from orchestrator import graph as graph_mod
    from orchestrator.context_builder import (
        _DEFAULT_RECOVERY_TARGET_MULTIPLIER,
        _DEFAULT_TARGET_RECOVERED_PAISE,
        _build_recovery_target_config,
        serialize_bundle_for_prompt,
        AttributionSnapshot,
        SalesRecoveryContext,
    )
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    pool = get_pool()

    tenant_id = uuid4()

    # --- Assertion 1: schema — columns exist on tenants table -----------------
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'tenants' "
            "AND column_name IN ('recovery_target_multiplier', 'recovery_target_floor_paise')"
        )
        found_cols = {r["column_name"] for r in cur.fetchall()}
    expected_cols = {"recovery_target_multiplier", "recovery_target_floor_paise"}
    pass_1 = found_cols == expected_cols
    assertion(
        1,
        "Migration 051 applied: recovery_target_multiplier + recovery_target_floor_paise on tenants",
        pass_1,
        observed=sorted(found_cols),
        expected=sorted(expected_cols),
    )
    if not pass_1:
        print("FATAL: schema missing — cannot continue", file=sys.stderr)
        return 1

    # --- Assertion 2: insert synthetic tenant; DEFAULT read = (1.1, 50_000) ---
    _seed_tenant(pool, tenant_id)
    # CL-390: log tenant UUID only, no PII
    print(f"    synthetic tenant_id: {tenant_id}")

    multiplier, floor_paise = _build_recovery_target_config(tenant_id)
    pass_2 = (multiplier == _DEFAULT_RECOVERY_TARGET_MULTIPLIER and floor_paise == _DEFAULT_TARGET_RECOVERED_PAISE)
    assertion(
        2,
        "Default read: _build_recovery_target_config returns (1.1, 50_000) for new tenant",
        pass_2,
        observed={"multiplier": multiplier, "floor_paise": floor_paise},
        expected={"multiplier": 1.1, "floor_paise": 50000},
    )

    # --- Assertion 3: UPDATE to (1.5, 100_000); re-read asserts override ------
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE tenants SET recovery_target_multiplier = 1.5, "
            "recovery_target_floor_paise = 100000 WHERE id = %s",
            (str(tenant_id),),
        )
    multiplier2, floor2 = _build_recovery_target_config(tenant_id)
    pass_3 = (multiplier2 == 1.5 and floor2 == 100_000)
    assertion(
        3,
        "Override read: after UPDATE (1.5, 100_000) re-read asserts override",
        pass_3,
        observed={"multiplier": multiplier2, "floor_paise": floor2},
        expected={"multiplier": 1.5, "floor_paise": 100000},
    )

    # --- Assertion 4: target math with override in serialize_bundle_for_prompt -
    # last_7d=80_000, multiplier=1.5 → round(120_000) > floor=100_000 → 120_000
    from uuid import uuid4 as _uuid4
    ctx = SalesRecoveryContext(
        tenant_id=tenant_id,
        run_id=_uuid4(),
        user_request="VT-164 canary target math check",
        attribution_snapshot=AttributionSnapshot(last_7d_recovered_paise=80_000),
        recovery_target_multiplier=1.5,
        recovery_target_floor_paise=100_000,
    )
    rendered = serialize_bundle_for_prompt(ctx)
    expected_target = max(round(80_000 * 1.5), 100_000)  # = 120_000
    pass_4 = f"target_recovered_paise: {expected_target}" in rendered
    assertion(
        4,
        f"Target math: last_7d=80_000 × 1.5 = {expected_target} appears in rendered block",
        pass_4,
        observed=f"target_recovered_paise: {expected_target}" if pass_4 else "not found in rendered",
        expected=f"target_recovered_paise: {expected_target}",
    )

    # --- Assertion 5: CHECK enforcement — multiplier=0 must raise --------------
    check_raised = False
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE tenants SET recovery_target_multiplier = 0 WHERE id = %s",
                (str(tenant_id),),
            )
    except Exception as exc:
        check_raised = True
        print(f"    CHECK raised (expected): {type(exc).__name__}: {exc!s:.80}")
    pass_5 = check_raised
    assertion(
        5,
        "CHECK enforcement: UPDATE multiplier=0 raises DB CHECK violation (fail-not-skip)",
        pass_5,
        observed="exception raised" if check_raised else "NO exception — CHECK not enforced!",
        expected="psycopg.errors.CheckViolation",
    )

    # --- Cleanup ---------------------------------------------------------------
    elapsed = time.monotonic() - start
    print(f"\n    Wall-clock: {elapsed:.2f}s (budget: 60s)")

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM tenants WHERE id = ANY(%s)",
                (INSERTED_TENANT_IDS,),
            )
            print(f"    Cleanup: deleted synthetic tenant(s) {INSERTED_TENANT_IDS}")
    except BaseException as exc:  # noqa: BLE001
        print(f"    Cleanup partial: {exc!r}", file=sys.stderr)

    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print("\n=== Anthropic cost: 0 paise (zero LLM in deterministic path) ===")

    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print("\nALL 5 ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
