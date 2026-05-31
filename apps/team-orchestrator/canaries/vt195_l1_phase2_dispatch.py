"""Canary: VT-195 Phase 2 — L1 block pre-injected at dispatch, cache-safe (Rule #15).

BINDING gate for Phase 2 (Cowork): a real dispatch must prove the per-tenant L1
block ships AFTER the VT-194 cached prefix WITHOUT breaking the cache, and that
the model actually receives it.

Assertions (real Anthropic calls; mirrors vt194_prompt_caching's webhook+usage
pattern):
  A1. Seed a tenant + a 'business_profile' L1 entity carrying a UNIQUE marker
      token in owner_curated_context. assemble_context_bundle(tenant) returns a
      block containing the marker (the injection source is non-empty).
  A2. Two dispatches for that tenant. The 2nd dispatch's agent_reasoning_step
      shows cache_read_input_tokens > 0 — the VT-194 cached prefix still HITs
      despite the extra L1 system block (D2 cache-safety).
  A3. The 2nd dispatch's reasoning envelope contains the UNIQUE marker token —
      proving the L1 block reached the model (not just the request).

Preflight (SKIP, not fail) when DATABASE_URL / ANTHROPIC_API_KEY /
INTERNAL_API_SECRET / a running orchestrator are absent — same gate as vt194.
This canary requires a live key + running orchestrator; it is NOT run in CI's
keyless jobs (run it on the canary machine / Fazal's env).

CL-422 synthetic data only. CL-390: the marker is a synthetic business note, not
customer PII.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

INSERTED_TENANT_IDS: list[str] = []
INSERTED_RUN_IDS: list[str] = []
_MARKER = f"vt195-marker-{uuid4().hex[:8]}"


def _preflight() -> str | None:
    required = ("DATABASE_URL", "ANTHROPIC_API_KEY", "INTERNAL_API_SECRET")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"vt195-p2 canary SKIP: missing env {missing} (preflight)", file=sys.stderr)
        return None
    orch_base = os.environ.get("ORCH_BASE_URL", "http://localhost:8080")
    return orch_base


def _seed_tenant_and_l1(pool: Any) -> tuple[str, str]:
    tenant_id = str(uuid4())
    INSERTED_TENANT_IDS.append(tenant_id)
    phone = f"+9199777{uuid4().hex[:6]}"
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'paid_active', %s) ON CONFLICT (id) DO NOTHING",
            (tenant_id, f"vt195 canary {tenant_id[:6]}", phone),
        )
        cur.execute(
            "INSERT INTO l1_entities (tenant_id, entity_type, attributes) "
            "VALUES (%s, 'business_profile', %s::jsonb)",
            (
                tenant_id,
                json.dumps(
                    {
                        "business_archetype": "electronics_retail",
                        "owner_curated_context": (
                            f"Internal note {_MARKER}: always mention the EMI offer."
                        ),
                    }
                ),
            ),
        )
    return tenant_id, phone


def _fire_webhook(orch_base: str, phone: str, body: str) -> str:
    import httpx

    message_sid = f"SM{uuid4().hex}"
    run_id = str(uuid5(NAMESPACE_URL, message_sid))
    res = httpx.post(
        f"{orch_base}/api/orchestrator/twilio-ingress",
        json={
            "twilio_fields": {
                "From": phone,
                "To": "+910000000000",
                "Body": body,
                "MessageSid": message_sid,
                "NumMedia": "0",
            }
        },
        headers={"X-Internal-Secret": os.environ["INTERNAL_API_SECRET"]},
        timeout=15.0,
    )
    if res.status_code != 200:
        raise RuntimeError(f"webhook POST failed: HTTP {res.status_code} {res.text}")
    INSERTED_RUN_IDS.append(run_id)
    return run_id


def _wait_terminal(pool: Any, run_id: str, max_wait_s: float = 45.0) -> str | None:
    start = time.monotonic()
    while time.monotonic() - start < max_wait_s:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM pipeline_runs WHERE id = %s", (run_id,))
            row = cur.fetchone()
        if row and row["status"] in (
            "completed", "failed", "terminal", "escalated", "aborted_hard_limit"
        ):
            return row["status"]
        time.sleep(0.5)
    return None


def _reasoning_step(pool: Any, run_id: str) -> dict[str, Any] | None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT output_envelope, tokens_input FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'agent_reasoning_step' "
            "ORDER BY step_seq LIMIT 1",
            (run_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def run_canary() -> int:
    orch_base = _preflight()
    if orch_base is None:
        return 0  # preflight skip — not a failure

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool
    from orchestrator.knowledge import assemble_context_bundle

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

    tenant_id, phone = _seed_tenant_and_l1(pool)
    body = "give me a quick summary of how my shop is doing this week"
    failures: list[str] = []

    # A1: the injection source is non-empty + carries the marker.
    block = assemble_context_bundle(tenant_id)
    if not block or _MARKER not in block:
        failures.append(f"A1 assemble_context_bundle missing marker: {block!r}")

    # Two dispatches for the same tenant.
    run_1 = _fire_webhook(orch_base, phone, body)
    _wait_terminal(pool, run_1)
    time.sleep(2.0)
    run_2 = _fire_webhook(orch_base, phone, body)
    _wait_terminal(pool, run_2)
    step_2 = _reasoning_step(pool, run_2)

    if step_2 is None:
        failures.append("A2/A3 second dispatch produced no agent_reasoning_step")
    else:
        env_2 = step_2.get("output_envelope") or {}
        cache_read_2 = int(env_2.get("cache_read_input_tokens", 0) or 0)
        if cache_read_2 <= 0:
            failures.append(
                f"A2 cache_read_input_tokens not > 0 on 2nd dispatch ({cache_read_2}) "
                "— L1 block may have broken the VT-194 cached prefix"
            )
        # A3: marker reached the model (appears in the reasoning envelope).
        if _MARKER not in json.dumps(env_2, default=str):
            failures.append("A3 marker token absent from 2nd reasoning envelope")

    if failures:
        print("vt195-p2 canary FAILED:\n" + "\n".join(failures), file=sys.stderr)
        return 1
    print("vt195-p2 canary: ALL CHECKS PASSED (L1 injected, cache HIT, marker reached model)")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
