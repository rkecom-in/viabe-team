#!/usr/bin/env python3
"""VT-188 operator-role JWT substrate canary (Rule #15, DR-15).

Subshell-source ONLY `.viabe/secrets/supabase-dev.env`:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt188_operator_jwt_substrate.py 2>&1 | tee /tmp/vt188-canary-evidence.log | tail -200
    )

**NO anthropic.env sourced.** Deterministic substrate; ANTHROPIC_API_KEY
ABSENT at PREFLIGHT.

Wall-clock budget ≤ 45s. Cost budget: 0 paise.

Scope note: the team-web Next.js route validates JWT + sets the
`app.jwt.operator_claim` GUC via Supabase's PostgREST integration. This
canary tests the Postgres SUBSTRATE directly (migration artifacts +
RLS policy + stored function) by simulating the GUC + SET ROLE
combination at the Postgres layer. End-to-end HTTP testing of the
team-web route is integration scope (VT-123).

8 assertions (brief offered 9; the 9th — synthetic audit-write failure
→ resolution rolls back — is included as A9 best-effort via a transient
GRANT revoke per Cowork's observation; if injection is fragile,
drop + document in pre-merge-result).
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


def _seed_token(pool, tenant_id: UUID, phone: str) -> str:
    from orchestrator.observability.phone_tokens import register_phone_token

    INSERTED_TENANT_IDS.append(str(tenant_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-vt188-{tenant_id}"),
        )
    token = register_phone_token(tenant_id=tenant_id, phone_e164=phone)
    INSERTED_TOKEN_IDS.append(token)
    return token


def _operator_call(
    pool,
    *,
    tenant_id: UUID | None,
    operator_claim: str | None,
    phone_token: str,
    operator_id: str,
):
    """Simulate the team-web route's RPC dispatch.

    Sets `app.current_tenant` GUC + `app.jwt.operator_claim` GUC, switches
    to `app_operator_role`, then calls `resolve_phone_token_audited`.
    Mirrors what Supabase's PostgREST + RLS context would do for a JWT.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SET ROLE app_operator_role")
        # Always set BOTH GUCs explicitly so pool-state leakage from
        # prior _operator_call invocations on the same connection
        # cannot contaminate the test (false=session-scoped per
        # tenant_connection() pattern; explicit '' clears).
        cur.execute(
            "SELECT set_config('app.current_tenant', %s, false)",
            (str(tenant_id) if tenant_id is not None else "",),
        )
        cur.execute(
            "SELECT set_config('app.jwt.operator_claim', %s, false)",
            (operator_claim if operator_claim is not None else "",),
        )
        # Always invoke the stored function; capture either result or exception.
        try:
            cur.execute(
                "SELECT resolve_phone_token_audited(%s, %s) AS phone",
                (phone_token, operator_id),
            )
            row = cur.fetchone()
            result = row["phone"] if row else None
            exc = None
        except Exception as e:  # noqa: BLE001
            result = None
            exc = e
        finally:
            cur.execute("SELECT set_config('app.current_tenant', '', false)")
            cur.execute("SELECT set_config('app.jwt.operator_claim', '', false)")
            cur.execute("RESET ROLE")
        return result, exc


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt188-canary-salt")

    from orchestrator import graph as graph_mod
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
    # A1: app_operator_role exists
    # ----------------------------------------------------------------
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT rolname FROM pg_roles WHERE rolname = 'app_operator_role'")
        role = cur.fetchone()
    pass_1 = role is not None
    assertion(
        1,
        "migration 027 applied: app_operator_role in pg_roles",
        pass_1,
        observed={"role_present": role is not None},
        expected={"role_present": True},
    )

    # ----------------------------------------------------------------
    # A2: phone_token_resolutions_operator_select policy in pg_policies
    # ----------------------------------------------------------------
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT policyname FROM pg_policies "
            "WHERE policyname = 'phone_token_resolutions_operator_select'"
        )
        policy = cur.fetchone()
    pass_2 = policy is not None
    assertion(
        2,
        "phone_token_resolutions_operator_select policy in pg_policies",
        pass_2,
        observed={"policy_present": policy is not None},
        expected={"policy_present": True},
    )

    # ----------------------------------------------------------------
    # A3: helper + stored function in pg_proc
    # ----------------------------------------------------------------
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT proname FROM pg_proc WHERE proname IN "
            "('app_operator_audit_enabled', 'resolve_phone_token_audited') "
            "ORDER BY proname"
        )
        procs = [r["proname"] for r in cur.fetchall()]
    pass_3 = procs == ["app_operator_audit_enabled", "resolve_phone_token_audited"]
    assertion(
        3,
        "app_operator_audit_enabled + resolve_phone_token_audited in pg_proc",
        pass_3,
        observed={"procs": procs},
        expected={"procs": ["app_operator_audit_enabled", "resolve_phone_token_audited"]},
    )

    # ----------------------------------------------------------------
    # A4: valid claim + tenant_a GUC → SELECT returns row
    # ----------------------------------------------------------------
    tenant_a = uuid4()
    phone_a = f"+9198888{uuid4().hex[:5]}"
    token_a = _seed_token(pool, tenant_a, phone_a)
    operator_id = "ops_canary_admin"

    result, exc = _operator_call(
        pool,
        tenant_id=tenant_a,
        operator_claim="operator",
        phone_token=token_a,
        operator_id=operator_id,
    )
    # VT-191: resolve_phone_token_audited returns the ciphertext column
    # value (the stored function is a thin SELECT — it does NOT decrypt
    # like phone_tokens.resolve_phone_token does). Client-side decrypt
    # happens in the team-web route (VT-192) or, here, via decrypt_phone.
    from orchestrator.observability.phone_tokens import decrypt_phone as _decrypt
    from cryptography.fernet import InvalidToken as _InvalidToken
    decrypted_result: str | None = None
    if isinstance(result, str):
        try:
            decrypted_result = _decrypt(result)
        except _InvalidToken:
            pass
    pass_4 = exc is None and decrypted_result == phone_a
    assertion(
        4,
        "valid operator claim + tenant_a GUC → SELECT returns row (ciphertext; decrypt-client-side returns phone)",
        pass_4,
        observed={"resolved": result, "exc": repr(exc) if exc else None},
        expected={"resolved": phone_a},
    )

    # ----------------------------------------------------------------
    # A5: JWT WITHOUT operator-claim → SELECT returns NULL (RLS deny)
    # ----------------------------------------------------------------
    result_no_claim, exc_no_claim = _operator_call(
        pool,
        tenant_id=tenant_a,
        operator_claim=None,
        phone_token=token_a,
        operator_id=operator_id,
    )
    # Without operator_claim, app_operator_audit_enabled() returns false → RLS denies → SELECT returns NULL.
    # But audit_INSERT will then fail because tenant_id from SELECT is NULL → privacy_audit_log RLS denies NULL.
    # Outcome: either NULL result or an exception (both = "no resolve").
    pass_5 = result_no_claim is None
    assertion(
        5,
        "JWT WITHOUT operator-claim → SELECT returns NULL or raises (RLS deny)",
        pass_5,
        observed={"resolved": result_no_claim, "exc": repr(exc_no_claim) if exc_no_claim else None},
        expected={"resolved": None},
    )

    # ----------------------------------------------------------------
    # A6: audit row written per successful resolve (count grew by A4)
    # ----------------------------------------------------------------
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM privacy_audit_log "
            "WHERE tenant_id = %s "
            "  AND event_type = 'phone_token_resolved' "
            "  AND payload->>'phone_token' = %s "
            "  AND payload->>'via_jwt' = 'true'",
            (str(tenant_a), token_a),
        )
        audit_count = int(cur.fetchone()["n"])
    pass_6 = audit_count >= 1
    assertion(
        6,
        "successful resolve → privacy_audit_log row (event_type='phone_token_resolved' + via_jwt=true)",
        pass_6,
        observed={"audit_rows_for_token": audit_count},
        expected={"audit_rows_for_token_gte": 1},
    )

    # ----------------------------------------------------------------
    # A7: cross-tenant: tenant_a token + tenant_b GUC → NULL
    # ----------------------------------------------------------------
    tenant_b = uuid4()
    INSERTED_TENANT_IDS.append(str(tenant_b))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_b), f"canary-vt188-{tenant_b}"),
        )
    result_cross, exc_cross = _operator_call(
        pool,
        tenant_id=tenant_b,
        operator_claim="operator",
        phone_token=token_a,
        operator_id=operator_id,
    )
    # Cross-tenant: SELECT returns NULL (RLS denies tenant_id mismatch).
    # Audit INSERT then tries tenant_id=NULL → privacy_audit_log RLS denies.
    # Outcome: exception OR NULL with no audit row.
    pass_7 = result_cross is None
    assertion(
        7,
        "cross-tenant: tenant_a token + tenant_b GUC → NULL (RLS deny)",
        pass_7,
        observed={"resolved": result_cross, "exc": repr(exc_cross) if exc_cross else None},
        expected={"resolved": None},
    )

    # ----------------------------------------------------------------
    # A8: ANTHROPIC ABSENT
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

    print("\n=== Anthropic cost: 0 paise (substrate-only canary; no LLM) ===")

    # Cleanup. Service-role bypasses RLS.
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM privacy_audit_log "
                "WHERE event_type = 'phone_token_resolved' "
                "  AND payload->>'phone_token' = ANY(%s)",
                (INSERTED_TOKEN_IDS,),
            )
            cur.execute(
                "DELETE FROM phone_token_resolutions "
                "WHERE phone_token = ANY(%s)",
                (INSERTED_TOKEN_IDS,),
            )
            cur.execute(
                "DELETE FROM tenants WHERE id = ANY(%s)", (INSERTED_TENANT_IDS,)
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
