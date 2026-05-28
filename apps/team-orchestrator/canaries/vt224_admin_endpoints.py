#!/usr/bin/env python3
"""VT-224 admin endpoints canary (Rule #15, DR-15).

Six deterministic assertions:

- A1: Missing X-Team-Admin-Token header → 403
- A2: Wrong X-Team-Admin-Token header → 403
- A3: Valid token + token_shape against a seeded tenant → 200 + scope
  shape returned + NO raw token values in body
- A4: Valid token + integration_agent health → 200 + active_oauth_tokens
  field present + structured last_ingestion array
- A5: 11 requests in 1s via asyncio.gather → ≥1 returns 429
- A6: Each successful call wrote one admin_audit_log row (delta
  before/after, scoped to this canary's token fingerprint)

Substrate: minimal FastAPI app built in-process with the admin router
mounted; init_substrate() opens the DB pool. NO DBOS launch — avoids
scheduler side effects in the canary process.

Subshell-source supabase-dev.env + the canary's TEAM_ADMIN_API_TOKEN:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      export TEAM_ADMIN_API_TOKEN="vt224-canary-$(date +%s)"
      set +a
      ./.venv/bin/python canaries/vt224_admin_endpoints.py
    )

Wall-clock budget ≤ 20s. Cost: 0 paise (no LLM).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANTS: list[str] = []
INSERTED_OAUTH_TENANTS: list[tuple[str, str]] = []


def assertion(
    num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None
) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {
        "name": name, "status": status, "observed": observed, "expected": expected,
    }
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _preflight() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    if not os.environ.get("TEAM_ADMIN_API_TOKEN"):
        print(
            "PREFLIGHT FAIL — TEAM_ADMIN_API_TOKEN missing",
            file=sys.stderr,
        )
        sys.exit(2)
    print("PREFLIGHT OK")


def _seed_tenant_with_token(pool: Any) -> tuple[str, str]:
    tid = str(uuid4())
    connector_id = "google_sheet"
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s) ON CONFLICT (id) DO NOTHING",
            (tid, f"vt224-canary-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
        conn.execute(
            """
            INSERT INTO tenant_oauth_tokens
                (tenant_id, connector_id, refresh_token_encrypted, scopes,
                 push_secret, last_refreshed_at)
            VALUES (%s, %s, 'placeholder_encrypted_value', ARRAY['https://example/scope'],
                    'placeholder_push_secret', now())
            ON CONFLICT (tenant_id, connector_id) DO UPDATE SET updated_at = now()
            """,
            (tid, connector_id),
        )
    INSERTED_TENANTS.append(tid)
    INSERTED_OAUTH_TENANTS.append((tid, connector_id))
    return tid, connector_id


def _cleanup(pool: Any) -> None:
    if not INSERTED_TENANTS:
        return
    with pool.connection() as conn:
        for tid, cid in INSERTED_OAUTH_TENANTS:
            conn.execute(
                "DELETE FROM tenant_oauth_tokens "
                "WHERE tenant_id = %s AND connector_id = %s",
                (tid, cid),
            )
        for tid in INSERTED_TENANTS:
            conn.execute("DELETE FROM tenants WHERE id = %s", (tid,))


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def run_canary() -> int:
    _preflight()

    # Bootstrap substrate WITHOUT DBOS launch (canary doesn't need schedulers).
    from orchestrator import graph as graph_mod
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

    # Build minimal FastAPI app with just the admin router.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api.admin import router as admin_router
    from orchestrator.api.admin._rate_limit import _reset_for_tests

    app = FastAPI()
    app.include_router(admin_router)

    valid_token = os.environ["TEAM_ADMIN_API_TOKEN"]
    fp = _token_fingerprint(valid_token)

    # Seed test data.
    tid, cid = _seed_tenant_with_token(pool)

    # Audit-log baseline: count rows with our fingerprint before.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM admin_audit_log WHERE token_fingerprint = %s",
            (fp,),
        )
        baseline_row = cur.fetchone()
    baseline = int(
        baseline_row["n"] if isinstance(baseline_row, dict) else baseline_row[0]
    )

    client = TestClient(app)

    # A1 — missing token
    r1 = client.get(
        "/api/orchestrator/admin/connector/token_shape",
        params={"tenant_id": tid, "connector_id": cid},
    )
    assertion(
        1,
        "missing X-Team-Admin-Token → 403",
        r1.status_code == 403,
        observed={"status": r1.status_code, "body": r1.text[:200]},
        expected={"status": 403},
    )

    # A2 — wrong token
    r2 = client.get(
        "/api/orchestrator/admin/connector/token_shape",
        params={"tenant_id": tid, "connector_id": cid},
        headers={"X-Team-Admin-Token": "wrong-token-aaaa"},
    )
    assertion(
        2,
        "wrong X-Team-Admin-Token → 403",
        r2.status_code == 403,
        observed={"status": r2.status_code, "body": r2.text[:200]},
        expected={"status": 403},
    )

    # A3 — valid token + token_shape → shape only
    _reset_for_tests()  # ensure rate-limit doesn't fire from A1/A2
    r3 = client.get(
        "/api/orchestrator/admin/connector/token_shape",
        params={"tenant_id": tid, "connector_id": cid},
        headers={"X-Team-Admin-Token": valid_token},
    )
    body3: dict[str, Any] = r3.json() if r3.status_code == 200 else {}
    pass_3 = (
        r3.status_code == 200
        and body3.get("refresh_present") is True
        and body3.get("push_secret_present") is True
        and isinstance(body3.get("scopes"), list)
        and "placeholder_encrypted_value" not in r3.text
        and "placeholder_push_secret" not in r3.text
    )
    assertion(
        3,
        "token_shape returns shape only + zero raw token leakage",
        pass_3,
        observed={
            "status": r3.status_code,
            "refresh_present": body3.get("refresh_present"),
            "push_secret_present": body3.get("push_secret_present"),
            "scopes": body3.get("scopes"),
            "raw_refresh_in_body": "placeholder_encrypted_value" in r3.text,
            "raw_push_secret_in_body": "placeholder_push_secret" in r3.text,
        },
        expected={"status": 200, "refresh_present": True, "raw_leakage": False},
    )

    # A4 — health
    r4 = client.get(
        "/api/orchestrator/admin/health/integration_agent",
        headers={"X-Team-Admin-Token": valid_token},
    )
    body4: dict[str, Any] = r4.json() if r4.status_code == 200 else {}
    pass_4 = (
        r4.status_code == 200
        and "active_oauth_tokens" in body4
        and isinstance(body4["active_oauth_tokens"], int)
        and "last_ingestion" in body4
        and isinstance(body4["last_ingestion"], list)
    )
    assertion(
        4,
        "integration_agent health returns structured response",
        pass_4,
        observed={"status": r4.status_code, "body_keys": list(body4.keys())},
        expected={"status": 200, "keys": ["active_oauth_tokens", "last_ingestion"]},
    )

    # A5 — rate limit burst
    _reset_for_tests()

    async def _burst() -> list[int]:
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            async def one() -> int:
                rr = await ac.get(
                    "/api/orchestrator/admin/health/integration_agent",
                    headers={"X-Team-Admin-Token": valid_token},
                )
                return rr.status_code
            return await asyncio.gather(*[one() for _ in range(11)])

    statuses = asyncio.run(_burst())
    pass_5 = sum(1 for s in statuses if s == 429) >= 1
    assertion(
        5,
        "11 requests in burst → ≥1 returns 429",
        pass_5,
        observed={"statuses": statuses, "count_429": sum(1 for s in statuses if s == 429)},
        expected={"count_429_gte": 1},
    )

    # A6 — audit log delta
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM admin_audit_log WHERE token_fingerprint = %s",
            (fp,),
        )
        after_row = cur.fetchone()
    after = int(after_row["n"] if isinstance(after_row, dict) else after_row[0])
    delta = after - baseline
    pass_6 = delta >= 5  # at minimum: A3, A4, several burst rows that completed before rate-limit kicked in
    assertion(
        6,
        f"admin_audit_log delta ≥ 5 for this fingerprint (observed {delta})",
        pass_6,
        observed={"baseline": baseline, "after": after, "delta": delta, "fingerprint": fp},
        expected={"delta_gte": 5},
    )

    _cleanup(pool)
    return _finalise(pool)


def _finalise(_pool: Any) -> int:
    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s) failed", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(run_canary())
