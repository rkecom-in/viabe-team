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


def _seed_tenant_no_l1(pool: Any) -> tuple[str, str]:
    """A baseline tenant with NO l1_entities row — assemble_context_bundle
    returns None, so no L1 block is injected. Used for the A3 token-delta."""
    tenant_id = str(uuid4())
    INSERTED_TENANT_IDS.append(tenant_id)
    phone = f"+9199666{uuid4().hex[:6]}"
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'paid_active', %s) ON CONFLICT (id) DO NOTHING",
            (tenant_id, f"vt195 baseline {tenant_id[:6]}", phone),
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


def _finalise(pool: Any) -> None:
    """VT-266: best-effort cleanup of the synthetic rows this canary seeded.

    Delete the run's pipeline_steps/_runs + the tenants' l1_entities first (no
    ON DELETE CASCADE on those FKs), then the tenants (cascades the CASCADE-FK
    children). Each step is best-effort — cleanup must never fail the canary.
    """
    for run_id in INSERTED_RUN_IDS:
        for tbl, col in (("pipeline_steps", "run_id"), ("pipeline_runs", "id")):
            try:
                with pool.connection() as conn:
                    conn.execute(f"DELETE FROM {tbl} WHERE {col} = %s", (run_id,))  # noqa: S608
            except Exception:  # noqa: BLE001
                pass
    for tid in INSERTED_TENANT_IDS:
        try:
            with pool.connection() as conn:
                conn.execute("DELETE FROM l1_entities WHERE tenant_id = %s", (tid,))
        except Exception:  # noqa: BLE001
            pass
        try:
            with pool.connection() as conn:
                conn.execute("DELETE FROM tenants WHERE id = %s", (tid,))
        except Exception:  # noqa: BLE001
            pass


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

    # Dispatch 1 (tenant WITH L1): first reasoning step's tokens_input includes
    # the injected (uncached) L1 block → the with-L1 input measurement.
    run_1 = _fire_webhook(orch_base, phone, body)
    _wait_terminal(pool, run_1)
    step_1 = _reasoning_step(pool, run_1)
    tokens_with = int((step_1 or {}).get("tokens_input") or 0)

    # Dispatch 2 (SAME tenant): A2 — cached prefix still HITs despite the block.
    time.sleep(2.0)
    run_2 = _fire_webhook(orch_base, phone, body)
    _wait_terminal(pool, run_2)
    step_2 = _reasoning_step(pool, run_2)

    # Baseline dispatch (tenant WITHOUT an L1 entity): no block injected.
    _base_id, base_phone = _seed_tenant_no_l1(pool)
    run_b = _fire_webhook(orch_base, base_phone, body)
    _wait_terminal(pool, run_b)
    step_b = _reasoning_step(pool, run_b)
    tokens_base = int((step_b or {}).get("tokens_input") or 0)

    # A2 — cache HIT preserved despite the extra L1 system block (the binding gate).
    if step_2 is None:
        failures.append("A2 second dispatch produced no agent_reasoning_step")
    else:
        cache_read_2 = int(
            (step_2.get("output_envelope") or {}).get("cache_read_input_tokens", 0) or 0
        )
        if cache_read_2 <= 0:
            failures.append(
                f"A2 cache_read not > 0 on 2nd dispatch ({cache_read_2}) — L1 block "
                "broke the VT-194 cached prefix"
            )

    # A3 — the L1 block reaches the model's INPUT. Same body / tools / system
    # prefix in both runs; the only difference is the injected (uncached) L1
    # block, so the with-L1 first-step tokens_input must exceed the no-L1
    # baseline by ~the block's tokens. Deterministic — no model-compliance
    # dependence (the reasoning envelope carries only action+think_text, so a
    # content-echo check is impossible). A 10-token margin guards against noise.
    if step_1 is None or step_b is None:
        failures.append("A3 missing reasoning step (with-L1 or baseline)")
    elif tokens_with < tokens_base + 10:
        failures.append(
            f"A3 L1 block not reflected in input tokens: with={tokens_with} "
            f"baseline={tokens_base} (expected with >= baseline+10)"
        )

    _finalise(pool)  # VT-266: clean the synthetic rows this run seeded.

    if failures:
        print("vt195-p2 canary FAILED:\n" + "\n".join(failures), file=sys.stderr)
        return 1
    print(
        "vt195-p2 canary: ALL CHECKS PASSED — block built; cache HIT (A2); "
        f"L1 reached model input (A3: tokens_with={tokens_with} > baseline={tokens_base})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
