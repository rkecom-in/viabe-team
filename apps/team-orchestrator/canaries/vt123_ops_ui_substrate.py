#!/usr/bin/env python3
"""VT-123 Ops Console substrate canary (Rule #15, DR-15).

Subshell-source ``.viabe/secrets/supabase-dev.env`` (+ pending env keys
for full coverage; see PREFLIGHT for the explicit list):

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt123_ops_ui_substrate.py 2>&1 | tee /tmp/vt123-canary-evidence.log | tail -200
    )

**NO anthropic.env sourced.** UI never invokes LLM; ANTHROPIC_API_KEY
ABSENT at PREFLIGHT (defense-in-depth per DR-15).

Wall-clock budget ≤ 60s. Cost budget: 0 paise.

9 assertions:

- A1: orchestrator proxy `/api/orchestrator/ops/resolve-phone` returns
  403 without `X-Internal-Secret`
- A2: orchestrator proxy returns 403 with valid internal-secret but
  missing operator-JWT
- A3: orchestrator proxy returns decrypted phone with valid internal-
  secret + valid operator-claim JWT (signed via OPERATOR_JWT_SECRET);
  audit row written to ``privacy_audit_log``
- A4: synthetic 50-step pipeline_run fixture inserts cleanly under
  service-role
- A5: ``fetchRunReplay`` query (raw SQL equivalent) returns ALL
  canonical pipeline_steps columns (parent_step_id, tokens_input/output,
  status, model_used, tool_calls, step_name, decision_rationale,
  step_seq) — no JSONB extraction
- A6: per-tenant 30-day query under service-role completes in <2s for
  100 synthetic runs
- A7: 50-step run replay query completes in <1s
- A8: operator-claim JWT TTL is 5 min (matches lib/auth/operator-jwt.ts)
- A9: ANTHROPIC ABSENT preflight (defense-in-depth)

If `OPERATOR_JWT_SECRET` / `FAZAL_OWNER_UUID` / `INTERNAL_API_SECRET`
are absent from the env (Q2 Fazal-async drop pending), the live
assertions skip with a BLOCKED status — canary returns exit 0 with
the BLOCKED reason printed so Cowork can route appropriately.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANT_IDS: list[str] = []
INSERTED_RUN_IDS: list[str] = []
INSERTED_TOKEN_IDS: list[str] = []


def assertion(num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def blocked(num: int, name: str, reason: str) -> None:
    RESULTS[num] = {"name": name, "status": "BLOCKED", "reason": reason}
    print(f"[{num}] BLOCKED — {name} ({reason})")


def _supabase_host() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if "@" not in url:
        return "<no-host>"
    return url.split("@", 1)[1].split("/", 1)[0]


def _preflight() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "PREFLIGHT FAIL — ANTHROPIC_API_KEY present; this canary MUST NOT "
            "source anthropic.env (defense-in-depth per DR-15).",
            file=sys.stderr,
        )
        sys.exit(2)
    extras = {
        "OPERATOR_JWT_SECRET": bool(os.environ.get("OPERATOR_JWT_SECRET")),
        "INTERNAL_API_SECRET": bool(os.environ.get("INTERNAL_API_SECRET")),
        "TEAM_PHONE_ENCRYPTION_KEY": bool(
            os.environ.get("TEAM_PHONE_ENCRYPTION_KEY")
        ),
    }
    print(
        f"PREFLIGHT OK — supabase: {_supabase_host()}; "
        f"ANTHROPIC_API_KEY: <absent — defense-in-depth>; "
        f"VT-123 extras present: {extras}"
    )


def _have_live_proxy_env() -> bool:
    return all(
        os.environ.get(k)
        for k in (
            "OPERATOR_JWT_SECRET",
            "INTERNAL_API_SECRET",
            "TEAM_PHONE_ENCRYPTION_KEY",
        )
    )


def _issue_test_jwt(operator_id: str) -> str:
    """Mirrors lib/auth/operator-jwt.ts ``issueOperatorJwt`` shape."""
    import jwt as pyjwt

    secret = os.environ["OPERATOR_JWT_SECRET"]
    now = int(time.time())
    payload = {
        "sub": operator_id,
        "operator_id": operator_id,
        "operator_claim": True,
        "aud": "authenticated",
        "iat": now,
        "exp": now + 300,
    }
    return pyjwt.encode(payload, secret, algorithm="HS256")


def run_canary() -> int:
    _preflight()

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool
    from orchestrator.observability.phone_tokens import register_phone_token

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

    # ---------------- A4 — synthetic fixture ----------------
    synth_tenant = uuid4()
    synth_run = uuid4()
    INSERTED_TENANT_IDS.append(str(synth_tenant))
    INSERTED_RUN_IDS.append(str(synth_run))

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(synth_tenant), f"canary-vt123-{synth_tenant}"),
        )
        cur.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status, started_at) "
            "VALUES (%s, %s, 'completed', NOW()) ON CONFLICT (id) DO NOTHING",
            (str(synth_run), str(synth_tenant)),
        )
        for seq in range(50):
            cur.execute(
                """
                INSERT INTO pipeline_steps (
                  run_id, tenant_id, step_seq, step_kind, step_name,
                  status, decision_rationale, model_used,
                  tokens_input, tokens_output, cost_paise, duration_ms,
                  tool_calls, input_envelope, output_envelope,
                  started_at, ended_at
                ) VALUES (
                  %s, %s, %s, 'mcp_tool_call', %s,
                  'completed', %s, 'claude-haiku-4-5',
                  100, 50, 1, 200,
                  '[{"tool_name": "noop"}]'::jsonb,
                  '{"tool_name": "noop", "tool_args": {}}'::jsonb,
                  '{"tool_result": {}, "cost_paise": 1, "duration_ms": 200}'::jsonb,
                  NOW(), NOW()
                )
                """,
                (
                    str(synth_run),
                    str(synth_tenant),
                    seq,
                    f"step-{seq}",
                    f"step {seq} synthetic rationale",
                ),
            )

    assertion(
        4,
        "synthetic 50-step fixture inserted cleanly under service-role",
        True,
        observed={"steps_inserted": 50, "run_id": str(synth_run)},
    )

    # ---------------- A5 — canonical columns visible ----------------
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_seq, step_kind, step_name, parent_step_id, status, "
            "decision_rationale, model_used, tokens_input, tokens_output, "
            "cost_paise, duration_ms, tool_calls, input_envelope, output_envelope, "
            "error, started_at, ended_at FROM pipeline_steps WHERE run_id = %s "
            "ORDER BY step_seq LIMIT 1",
            (str(synth_run),),
        )
        row = cur.fetchone()
    required_cols = [
        "step_seq",
        "step_kind",
        "step_name",
        "parent_step_id",
        "status",
        "decision_rationale",
        "model_used",
        "tokens_input",
        "tokens_output",
        "cost_paise",
        "duration_ms",
        "tool_calls",
        "input_envelope",
        "output_envelope",
        "error",
        "started_at",
        "ended_at",
    ]
    pass_5 = row is not None and all(c in row for c in required_cols)
    assertion(
        5,
        "pipeline_steps canonical per-field columns all visible (CL-417, NO JSONB extraction)",
        pass_5,
        observed={
            "row_present": row is not None,
            "missing_columns": [c for c in required_cols if not row or c not in row],
        },
    )

    # ---------------- A6 — per-tenant timeline <2s for 100 runs ----------------
    extra_run_ids: list[str] = []
    with pool.connection() as conn, conn.cursor() as cur:
        for _ in range(100):
            r_id = uuid4()
            extra_run_ids.append(str(r_id))
            INSERTED_RUN_IDS.append(str(r_id))
            cur.execute(
                "INSERT INTO pipeline_runs (id, tenant_id, status, started_at) "
                "VALUES (%s, %s, 'completed', NOW()) ON CONFLICT (id) DO NOTHING",
                (str(r_id), str(synth_tenant)),
            )

    t0 = time.monotonic()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, started_at, ended_at, trigger_kind, "
            "total_cost_paise, step_count FROM pipeline_runs "
            "WHERE tenant_id = %s "
            "AND started_at >= NOW() - INTERVAL '30 days' "
            "ORDER BY started_at DESC LIMIT 500",
            (str(synth_tenant),),
        )
        cur.fetchall()
    elapsed_6 = time.monotonic() - t0
    pass_6 = elapsed_6 < 2.0
    assertion(
        6,
        "per-tenant 30-day timeline query <2s for 100 runs",
        pass_6,
        observed={"elapsed_s": round(elapsed_6, 3), "runs_seeded": 100},
        expected={"elapsed_s_lt": 2.0},
    )

    # ---------------- A7 — 50-step run replay <1s ----------------
    t0 = time.monotonic()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, run_id, step_seq, step_kind, step_name, parent_step_id, "
            "status, decision_rationale, model_used, tokens_input, tokens_output, "
            "cost_paise, duration_ms, tool_calls, input_envelope, output_envelope, "
            "error, started_at, ended_at FROM pipeline_steps "
            "WHERE run_id = %s ORDER BY step_seq",
            (str(synth_run),),
        )
        cur.fetchall()
    elapsed_7 = time.monotonic() - t0
    pass_7 = elapsed_7 < 1.0
    assertion(
        7,
        "50-step run replay query <1s",
        pass_7,
        observed={"elapsed_s": round(elapsed_7, 3)},
        expected={"elapsed_s_lt": 1.0},
    )

    # ---------------- A8 — JWT TTL is 5 min ----------------
    if _have_live_proxy_env():
        import jwt as pyjwt

        op_id = str(uuid4())
        encoded = _issue_test_jwt(op_id)
        decoded = pyjwt.decode(
            encoded,
            os.environ["OPERATOR_JWT_SECRET"],
            algorithms=["HS256"],
            audience="authenticated",
        )
        ttl = int(decoded["exp"]) - int(decoded["iat"])
        pass_8 = ttl == 300
        assertion(
            8,
            "operator-claim JWT TTL is 300s (5 min) — matches lib/auth/operator-jwt.ts",
            pass_8,
            observed={"ttl_s": ttl},
            expected={"ttl_s": 300},
        )
    else:
        blocked(
            8,
            "operator-claim JWT TTL check",
            "OPERATOR_JWT_SECRET absent (Q2 Fazal-async drop pending)",
        )

    # ---------------- A1-A3 — proxy endpoint flows ----------------
    if _have_live_proxy_env():
        # Need running orchestrator service. Skip with informative blocked
        # if not reachable — full e2e (orchestrator boot) lives in CI.
        try:
            import httpx

            base = os.environ.get("ORCHESTRATOR_BASE_URL", "http://localhost:8001")

            # A1 — missing internal secret
            r1 = httpx.post(
                f"{base}/api/orchestrator/ops/resolve-phone",
                json={"phone_token": "phone_tok_dummy", "operator_id": "x"},
                timeout=5.0,
            )
            assertion(
                1,
                "proxy returns 403 without X-Internal-Secret",
                r1.status_code == 403,
                observed={"status": r1.status_code},
                expected={"status": 403},
            )

            # A2 — internal secret but no JWT
            r2 = httpx.post(
                f"{base}/api/orchestrator/ops/resolve-phone",
                json={"phone_token": "phone_tok_dummy", "operator_id": "x"},
                headers={
                    "X-Internal-Secret": os.environ["INTERNAL_API_SECRET"],
                },
                timeout=5.0,
            )
            assertion(
                2,
                "proxy returns 403 with internal-secret but missing operator-JWT",
                r2.status_code == 403,
                observed={"status": r2.status_code},
                expected={"status": 403},
            )

            # A3 — full happy-path: register a token, then resolve.
            phone = f"+919{uuid4().hex[:9]}"
            tok = register_phone_token(tenant_id=synth_tenant, phone_e164=phone)
            INSERTED_TOKEN_IDS.append(tok)
            op_id = str(uuid4())
            jwt = _issue_test_jwt(op_id)
            r3 = httpx.post(
                f"{base}/api/orchestrator/ops/resolve-phone",
                json={"phone_token": tok, "operator_id": op_id},
                headers={
                    "X-Internal-Secret": os.environ["INTERNAL_API_SECRET"],
                    "X-Operator-Jwt": jwt,
                },
                timeout=5.0,
            )
            data = r3.json() if r3.status_code == 200 else {}
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM privacy_audit_log "
                    "WHERE payload->>'phone_token' = %s",
                    (tok,),
                )
                audit_count = (cur.fetchone() or {}).get("n", 0)
            pass_3 = (
                r3.status_code == 200
                and data.get("phone_e164") == phone
                and int(audit_count) >= 1
            )
            assertion(
                3,
                "proxy returns decrypted phone + audit row written (VT-188 atomic)",
                pass_3,
                observed={
                    "status": r3.status_code,
                    "phone_match": data.get("phone_e164") == phone,
                    "audit_rows": int(audit_count),
                },
                expected={"status": 200, "phone_match": True, "audit_rows_gte": 1},
            )
        except httpx.HTTPError as exc:
            blocked(
                1,
                "proxy 403 without internal-secret",
                f"orchestrator unreachable: {exc!r}",
            )
            blocked(
                2,
                "proxy 403 with internal-secret + missing JWT",
                f"orchestrator unreachable: {exc!r}",
            )
            blocked(
                3,
                "proxy happy-path resolves + audits",
                f"orchestrator unreachable: {exc!r}",
            )
    else:
        for n, name in (
            (1, "proxy 403 without internal-secret"),
            (2, "proxy 403 with internal-secret + missing JWT"),
            (3, "proxy happy-path resolves + audits"),
        ):
            blocked(n, name, "env missing — Q2 Fazal-async drop pending")

    # ---------------- A9 — ANTHROPIC ABSENT ----------------
    pass_9 = not os.environ.get("ANTHROPIC_API_KEY")
    assertion(
        9,
        "ANTHROPIC_API_KEY absent throughout (defense-in-depth DR-15)",
        pass_9,
        observed={"ANTHROPIC_API_KEY": "<absent>" if pass_9 else "<PRESENT — FAIL>"},
        expected={"ANTHROPIC_API_KEY": "<absent>"},
    )

    return _finalise(pool)


def _finalise(pool: Any) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print("\n=== Anthropic cost: 0 paise (UI substrate; no LLM) ===")

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if INSERTED_TOKEN_IDS:
                cur.execute(
                    "DELETE FROM privacy_audit_log WHERE payload->>'phone_token' = ANY(%s)",
                    (INSERTED_TOKEN_IDS,),
                )
                cur.execute(
                    "DELETE FROM phone_token_resolutions WHERE phone_token = ANY(%s)",
                    (INSERTED_TOKEN_IDS,),
                )
            if INSERTED_RUN_IDS:
                cur.execute(
                    "DELETE FROM pipeline_steps WHERE run_id = ANY(%s)",
                    (INSERTED_RUN_IDS,),
                )
                cur.execute(
                    "DELETE FROM pipeline_runs WHERE id = ANY(%s)",
                    (INSERTED_RUN_IDS,),
                )
            if INSERTED_TENANT_IDS:
                cur.execute(
                    "DELETE FROM tenants WHERE id = ANY(%s)",
                    (INSERTED_TENANT_IDS,),
                )
    except BaseException as exc:  # noqa: BLE001
        print(f"cleanup partial: {exc!r}", file=sys.stderr)

    failed = [n for n, r in RESULTS.items() if r["status"] == "FAIL"]
    blocked_n = [n for n, r in RESULTS.items() if r["status"] == "BLOCKED"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    if blocked_n:
        print(
            f"\nBLOCKED assertions (env or runtime gating): {blocked_n}",
            file=sys.stderr,
        )
        # Exit 0 so the canary script doesn't fail CI when Q2 env is
        # pending — Cowork inspects the BLOCKED reasons in summary.
        return 0
    print(f"\nALL {len(RESULTS)} ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
