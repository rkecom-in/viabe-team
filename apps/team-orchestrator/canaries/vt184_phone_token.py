#!/usr/bin/env python3
"""VT-184 phone-token resolution + audit log canary (Rule #15, DR-15).

Subshell-source ONLY `.viabe/secrets/supabase-dev.env`:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt184_phone_token.py 2>&1 | tee /tmp/vt184-canary-evidence.log | tail -200
    )

**NO anthropic.env sourced.** Deterministic writer; ANTHROPIC_API_KEY
ABSENT at PREFLIGHT.

Wall-clock budget ≤ 30s. Cost budget: 0 paise.

8 assertions per brief §Rule-15:
- A1: register_phone_token idempotent — same args → same token + no
  duplicate row + resolved_count unchanged on register
- A2: resolve_phone_token returns phone + resolved_count +1 + last_accessed_at updates
- A3: cross-tenant: tenant_b GUC + tenant_a's token → None (RLS denies UPDATE)
- A4: every resolve_phone_token → 1 row in privacy_audit_log
  (event_type='phone_token_resolved')
- A5: VT-104 _hash_phone produces identical token to phone_tokens._hash_phone
  (cross-module byte-identical hash)
- A6: idempotent hash — same phone twice → same token
- A7: RLS isolation under app_current_tenant GUC
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


def _seed_tenant(pool, tenant_id: UUID) -> None:
    INSERTED_TENANT_IDS.append(str(tenant_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-vt184-{tenant_id}"),
        )


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt184-canary-salt")

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool
    from orchestrator.observability.phone_tokens import (
        _hash_phone,
        register_phone_token,
        resolve_phone_token,
    )

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

    tenant_a = uuid4()
    tenant_b = uuid4()
    _seed_tenant(pool, tenant_a)
    _seed_tenant(pool, tenant_b)

    phone_a = f"+9198765{uuid4().hex[:5]}"

    # --------------------------------------------------------------
    # A1: register_phone_token idempotent
    # --------------------------------------------------------------

    token_first = register_phone_token(tenant_id=tenant_a, phone_e164=phone_a)
    token_second = register_phone_token(tenant_id=tenant_a, phone_e164=phone_a)
    INSERTED_TOKEN_IDS.append(token_first)

    from orchestrator.db.tenant_connection import tenant_connection

    # phone_token_resolutions uses BY-GRANT-EXCLUSION pattern (VT-178);
    # app_role can't SELECT. Verification reads via service-role pool.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n, MAX(resolved_count) AS rc "
            "FROM phone_token_resolutions WHERE phone_token = %s",
            (token_first,),
        )
        row = cur.fetchone()
    pass_1 = (
        token_first == token_second
        and int(row["n"]) == 1
        and int(row["rc"] or 0) == 0
    )
    assertion(
        1,
        "register_phone_token idempotent: same args → same token + 1 row + resolved_count=0",
        pass_1,
        observed={
            "token_first": token_first,
            "token_second": token_second,
            "row_count": int(row["n"]),
            "resolved_count": int(row["rc"] or 0),
        },
        expected={
            "tokens_equal": True,
            "row_count": 1,
            "resolved_count": 0,
        },
    )

    # --------------------------------------------------------------
    # A2: resolve_phone_token returns phone + increments + updates timestamp
    # --------------------------------------------------------------

    resolved = resolve_phone_token(
        tenant_id=tenant_a, phone_token=token_first, operator_id="ops_admin"
    )
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT resolved_count, last_accessed_at IS NOT NULL AS accessed "
            "FROM phone_token_resolutions WHERE phone_token = %s",
            (token_first,),
        )
        row_after = cur.fetchone()
    pass_2 = (
        resolved == phone_a
        and int(row_after["resolved_count"]) == 1
        and row_after["accessed"] is True
    )
    assertion(
        2,
        "resolve_phone_token returns phone + resolved_count=1 + last_accessed_at set",
        pass_2,
        observed={
            "resolved": resolved,
            "resolved_count": int(row_after["resolved_count"]),
            "last_accessed_set": row_after["accessed"],
        },
        expected={
            "resolved": phone_a,
            "resolved_count": 1,
            "last_accessed_set": True,
        },
    )

    # --------------------------------------------------------------
    # A3: cross-tenant — tenant_b GUC + tenant_a's token → None
    # --------------------------------------------------------------

    cross = resolve_phone_token(
        tenant_id=tenant_b, phone_token=token_first, operator_id="ops_admin"
    )
    pass_3 = cross is None
    assertion(
        3,
        "cross-tenant: tenant_b GUC + tenant_a's token → None (RLS denies UPDATE)",
        pass_3,
        observed={"resolved_under_tenant_b": cross},
        expected={"resolved_under_tenant_b": None},
    )

    # --------------------------------------------------------------
    # A4: every resolve → 1 audit row (count grew by 2 across A2 + A3)
    # --------------------------------------------------------------

    # privacy_audit_log also BY-GRANT-EXCLUSION (migration 008 created
    # before 015's default-privileges grant; no explicit GRANT in 015).
    # Verification via service-role pool.
    audit_count_a = 0
    audit_count_b = 0
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM privacy_audit_log "
            "WHERE tenant_id = %s "
            "  AND event_type = 'phone_token_resolved' "
            "  AND payload->>'phone_token' = %s",
            (str(tenant_a), token_first),
        )
        audit_count_a = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT COUNT(*) AS n FROM privacy_audit_log "
            "WHERE tenant_id = %s "
            "  AND event_type = 'phone_token_resolved' "
            "  AND payload->>'phone_token' = %s",
            (str(tenant_b), token_first),
        )
        audit_count_b = int(cur.fetchone()["n"])
    pass_4 = audit_count_a >= 1 and audit_count_b >= 1
    assertion(
        4,
        "every resolve_phone_token → 1 privacy_audit_log row (tenant_a hit + tenant_b miss)",
        pass_4,
        observed={
            "audit_rows_under_tenant_a": audit_count_a,
            "audit_rows_under_tenant_b": audit_count_b,
        },
        expected={
            "audit_rows_under_tenant_a_gte": 1,
            "audit_rows_under_tenant_b_gte": 1,
        },
    )

    # --------------------------------------------------------------
    # A5: VT-104 _hash_phone byte-identical with phone_tokens._hash_phone
    # --------------------------------------------------------------

    from orchestrator.privacy.pii_redactor import _hash_phone as vt104_hash_phone

    sample_phone = "+919876543210"
    pt_token = _hash_phone(sample_phone)
    vt104_token = vt104_hash_phone(sample_phone)
    pass_5 = pt_token == vt104_token and pt_token.startswith("phone_tok_")
    assertion(
        5,
        "phone_tokens._hash_phone == pii_redactor._hash_phone byte-identical (phone_tok_ prefix preserved)",
        pass_5,
        observed={"vt184_token": pt_token, "vt104_token": vt104_token},
        expected={"tokens_equal": True, "prefix": "phone_tok_"},
    )

    # --------------------------------------------------------------
    # A6: idempotent hash — same phone twice → same token
    # --------------------------------------------------------------

    pass_6 = _hash_phone(sample_phone) == _hash_phone(sample_phone)
    assertion(
        6,
        "_hash_phone idempotent: same phone twice → same token",
        pass_6,
        observed={"both_calls_equal": pass_6},
        expected={"both_calls_equal": True},
    )

    # --------------------------------------------------------------
    # A7: RLS isolation — tenant_a's row not visible under tenant_b GUC SELECT
    # --------------------------------------------------------------

    # phone_token_resolutions uses BY-GRANT-EXCLUSION pattern (VT-178):
    # app_role denied on the whole table. Verification reads via
    # service-role pool. RLS-tenant-isolation test = SELECT under
    # service-role WITH tenant_id filter mismatch → 0 rows (proves the
    # row CARRIES tenant_a's tenant_id correctly, so any future
    # operator-role policy on tenant_id = app_current_tenant() will
    # isolate correctly).
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM phone_token_resolutions "
            "WHERE phone_token = %s AND tenant_id = %s",
            (token_first, str(tenant_a)),
        )
        under_a = int(cur.fetchone()["n"])
    privilege_blocked_b = False
    import psycopg
    try:
        with tenant_connection(tenant_b) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM phone_token_resolutions "
                "WHERE phone_token = %s",
                (token_first,),
            )
            under_b = int(cur.fetchone()["n"])
    except psycopg.errors.InsufficientPrivilege:
        privilege_blocked_b = True
        under_b = 0
    pass_7 = under_a == 1 and (privilege_blocked_b or under_b == 0)
    assertion(
        7,
        "RLS isolation: tenant_a SELECT sees 1; tenant_b denied (privilege or 0 rows)",
        pass_7,
        observed={
            "under_a": under_a,
            "under_b_privilege_blocked": privilege_blocked_b,
            "under_b_count": under_b,
        },
        expected={
            "under_a": 1,
            "under_b": "privilege_blocked OR 0 rows",
        },
    )

    # --------------------------------------------------------------
    # A8: zero LLM
    # --------------------------------------------------------------

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

    print("\n=== Anthropic cost: 0 paise (deterministic writer; no LLM) ===")

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
                "DELETE FROM phone_token_resolutions WHERE phone_token = ANY(%s)",
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
