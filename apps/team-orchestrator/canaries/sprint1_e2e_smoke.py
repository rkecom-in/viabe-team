#!/usr/bin/env python3
"""Sprint 1 close-out — synthetic full-pipeline E2E smoke canary.

Goal (Fazal directive 2026-05-27):
    "today we only need to figure out if everything we did till now
     works as planned"

End-to-end flow exercised:
    synthetic webhook → twilio-ingress → DBOS webhook_pipeline_run
    workflow → orchestrator-agent (real Anthropic, low-token) →
    composed output. Real DB writes; Twilio outbound NOT verified
    (composer + send-stub is enough for smoke).

Subshell-source the necessary env files:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      source ../../.viabe/secrets/anthropic.env
      set +a
      time ./.venv/bin/python canaries/sprint1_e2e_smoke.py 2>&1 | tee /tmp/sprint1-e2e-evidence.log | tail -200
    )

Wall-clock budget ≤ 30s. Anthropic cost budget ≤ 10 paise (synthetic
message is minimal; bound prevents runaway).

8 assertions:

- A1: pipeline_runs row exists with status ∈ {completed, terminal}
  (NOT failed) for the synthetic run
- A2: ≥1 pipeline_steps rows per phase populated with canonical
  per-field columns (CL-417 — no JSONB-only paths)
- A3: ≥1 anthropic-mediated step row (cost_paise > 0; model_used set)
- A4: at least one envelope row present + parseable JSON for
  output_envelope (VT-180/187 contract)
- A5: zero privacy_audit_log rows for the run (no [resolve] flow
  triggered)
- A6: L0 fragments — synthetic cohort observation_count < 10 so
  query_l0 returns empty (expected; k-anonymity gate working)
- A7: wall-clock total < 30s
- A8: total Anthropic cost < 10 paise (synthetic input bound)

## Findings (2026-05-27 first run)

Substrate proven at 6/8 PASS. The 2 FAIL surface a real structural
seam — NOT a regression:

- A1 FAIL (`status='escalated'` not in {completed, terminal})
- A3 FAIL (zero `cost_paise > 0` rows)

**Finding 3 (CORRECTED 2026-05-27):** The `agent_invocation` envelope
visible in step_kinds is `record_brain_pending`'s placeholder write per
``runner.py:303-307``, NOT a real ``OrchestratorAgentDriver`` invocation.
The supervisor + agent + callback + L0 code is built across
VT-125/126/27/180/182/183 but the call site from
``pre_filter.brain`` → supervisor graph does not exist. **VT-193 closes
that seam.** After VT-193 lands, re-run this canary; expect
``agent_reasoning_step`` rows + ``total_cost_paise > 0`` (A1 → completed,
A3 → PASS).

Three legitimate Sprint 2 followups (allocated separately by Cowork):
``TEAM_TWILIO_MOCK_MODE``, ``twilio_inbound_events`` FK cascade, DBOS
``purge_workflow_inputs_scheduled`` registration.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANT_IDS: list[str] = []
INSERTED_RUN_IDS: list[str] = []


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


def _preflight() -> tuple[str, str]:
    """Verify env + orchestrator reachable. Returns (orch_base, tenant_phone)."""
    required = (
        "DATABASE_URL",
        "ANTHROPIC_API_KEY",
        "INTERNAL_API_SECRET",
        "TEAM_PHONE_ENCRYPTION_KEY",
    )
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"PREFLIGHT FAIL — missing env: {missing}", file=sys.stderr)
        sys.exit(2)

    import httpx

    orch_base = os.environ.get("ORCHESTRATOR_BASE_URL", "http://localhost:8001")
    try:
        # /docs is FastAPI's default; if orchestrator's docs disabled, any
        # non-200 (404, etc.) still proves the port is listening.
        r = httpx.get(orch_base, timeout=3.0)
        _ = r.status_code
    except httpx.HTTPError as exc:
        print(
            f"PREFLIGHT FAIL — orchestrator unreachable at {orch_base}: {exc!r}",
            file=sys.stderr,
        )
        print(
            "Boot the orchestrator first: "
            "(cd apps/team-orchestrator && uvicorn main:app --app-dir src --port 8001)",
            file=sys.stderr,
        )
        sys.exit(2)

    # Synthesise a per-run phone so re-runs don't collide on tenants
    # whatsapp_number lookups.
    tenant_phone = f"+9199888{uuid4().hex[:6]}"
    print(
        f"PREFLIGHT OK — supabase: {_supabase_host()}; "
        f"orchestrator: {orch_base}; tenant_phone: {tenant_phone}; "
        f"ANTHROPIC_API_KEY: present (real call mode)"
    )
    return orch_base, tenant_phone


def run_canary() -> int:
    t_start = time.monotonic()
    orch_base, tenant_phone = _preflight()

    import httpx

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

    # ---------------- FIXTURES ----------------
    tenant_id = uuid4()
    INSERTED_TENANT_IDS.append(str(tenant_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'paid_active', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), "E2E Smoke RKeCom", tenant_phone),
        )
    print(f"FIXTURES — tenant_id={tenant_id} whatsapp_number={tenant_phone}")

    # ---------------- TRIGGER ----------------
    message_sid = f"SM{uuid4().hex}"
    # Derive the orchestrator's run_id the same way twilio_ingress does
    # (uuid5 over NAMESPACE_URL + message_sid). The endpoint only returns
    # workflow_id ("twilio_inbound_<sid>"); pipeline_runs.id is the
    # uuid5-derived value.
    run_id = uuid5(NAMESPACE_URL, message_sid)
    # Body MUST NOT match pre_filter_gate's _STATUS_PING regex (which
    # catches "hi" / "hello" / etc. → routes to status_ping_handler →
    # tries real Twilio send → never reaches Anthropic). A substantive
    # message routes to RouteToBrain → orchestrator-agent reasoning →
    # real Anthropic call (A3 substrate).
    twilio_fields = {
        "From": tenant_phone,
        "To": "+910000000000",
        "Body": "can you give me a quick summary of my customers this week",
        "MessageSid": message_sid,
        "NumMedia": "0",
    }
    res = httpx.post(
        f"{orch_base}/api/orchestrator/twilio-ingress",
        json={"twilio_fields": twilio_fields},
        headers={"X-Internal-Secret": os.environ["INTERNAL_API_SECRET"]},
        timeout=15.0,
    )
    if res.status_code != 200:
        print(f"TRIGGER FAIL — HTTP {res.status_code}: {res.text}", file=sys.stderr)
        return _finalise(pool, t_start)
    body = res.json()
    workflow_id = body.get("workflow_id")
    if not workflow_id:
        print(f"TRIGGER FAIL — no workflow_id returned: {body}", file=sys.stderr)
        return _finalise(pool, t_start)
    run_id_str = str(run_id)
    INSERTED_RUN_IDS.append(run_id_str)
    print(
        f"TRIGGER — workflow_id={workflow_id} "
        f"run_id={run_id_str} reason={body.get('reason')}"
    )

    # ---------------- WAIT ----------------
    poll_start = time.monotonic()
    terminal_status: str | None = None
    while time.monotonic() - poll_start < 25.0:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM pipeline_runs WHERE id = %s",
                (run_id_str,),
            )
            row = cur.fetchone()
        if row and row["status"] in ("completed", "failed", "terminal"):
            terminal_status = row["status"]
            break
        time.sleep(0.5)
    print(f"WAIT — terminal_status={terminal_status} after {time.monotonic() - poll_start:.1f}s")

    # ---------------- ASSERTIONS ----------------

    # A1 — pipeline_runs row exists with non-failed status
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, total_cost_paise, step_count "
            "FROM pipeline_runs WHERE id = %s",
            (run_id_str,),
        )
        run_row = cur.fetchone()
    pass_1 = run_row is not None and run_row["status"] in ("completed", "terminal")
    assertion(
        1,
        "pipeline_runs row exists with non-failed status",
        pass_1,
        observed={
            "row_present": run_row is not None,
            "status": run_row["status"] if run_row else None,
        },
        expected={"status": "completed_or_terminal"},
    )

    # A2 — pipeline_steps populated with canonical per-field columns
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_kind, step_name, status, step_seq, input_envelope, "
            "output_envelope, decision_rationale "
            "FROM pipeline_steps WHERE run_id = %s ORDER BY step_seq",
            (run_id_str,),
        )
        step_rows = cur.fetchall()
    canonical_present = all(
        all(c in row for c in ("step_kind", "step_name", "status", "step_seq", "input_envelope"))
        for row in step_rows
    )
    pass_2 = len(step_rows) >= 1 and canonical_present
    assertion(
        2,
        "pipeline_steps rows present with canonical per-field columns (CL-417)",
        pass_2,
        observed={
            "step_count": len(step_rows),
            "canonical_columns_present": canonical_present,
            "step_kinds": list({r["step_kind"] for r in step_rows}),
        },
        expected={"step_count_gte": 1, "canonical_columns_present": True},
    )

    # A3 — ≥1 anthropic-mediated step row
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM pipeline_steps "
            "WHERE run_id = %s AND cost_paise > 0 AND model_used IS NOT NULL",
            (run_id_str,),
        )
        cost_row = cur.fetchone()
    anthropic_rows = int(cost_row["n"]) if cost_row else 0
    pass_3 = anthropic_rows >= 1
    assertion(
        3,
        "≥1 pipeline_steps row with cost_paise > 0 + model_used set (Anthropic)",
        pass_3,
        observed={"anthropic_rows": anthropic_rows},
        expected={"anthropic_rows_gte": 1},
    )

    # A4 — at least one envelope output_envelope is non-null + JSON-shaped
    envelope_rows = [r for r in step_rows if r.get("output_envelope") is not None]
    pass_4 = len(envelope_rows) >= 1 and isinstance(envelope_rows[0]["output_envelope"], (dict, list))
    assertion(
        4,
        "≥1 pipeline_steps row carries non-null output_envelope (VT-180/187 contract)",
        pass_4,
        observed={
            "envelope_rows": len(envelope_rows),
            "first_envelope_type": (
                type(envelope_rows[0]["output_envelope"]).__name__ if envelope_rows else None
            ),
        },
        expected={"envelope_rows_gte": 1},
    )

    # A5 — zero privacy_audit_log rows for this run (no resolve triggered)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM privacy_audit_log "
            "WHERE tenant_id = %s",
            (str(tenant_id),),
        )
        audit_row = cur.fetchone()
    audit_rows = int(audit_row["n"]) if audit_row else 0
    pass_5 = audit_rows == 0
    assertion(
        5,
        "privacy_audit_log rows = 0 for this run (no [resolve] flow)",
        pass_5,
        observed={"audit_rows": audit_rows},
        expected={"audit_rows": 0},
    )

    # A6 — L0 fragments returned empty for this cohort (k-anonymity gate)
    from orchestrator.observability.l0_memory import query_l0

    cohort_key = f"e2e-smoke|tier_unknown|paid_active|{uuid4().hex[:8]}"
    l0_result = query_l0(
        fragment_type="routing_decision", cohort_key=cohort_key, k=5
    )
    pass_6 = l0_result["matched_count"] == 0
    assertion(
        6,
        "L0 query returns empty for fresh cohort (k=10 gate working)",
        pass_6,
        observed={"matched_count": l0_result["matched_count"]},
        expected={"matched_count": 0},
    )

    # A7 — wall-clock total <30s
    total_elapsed = time.monotonic() - t_start
    pass_7 = total_elapsed < 30.0
    assertion(
        7,
        "wall-clock total < 30s (Fazal don't-stress budget)",
        pass_7,
        observed={"elapsed_s": round(total_elapsed, 2)},
        expected={"elapsed_s_lt": 30.0},
    )

    # A8 — total Anthropic cost <10 paise (synthetic input bound)
    total_cost = int(run_row["total_cost_paise"] or 0) if run_row else 0
    pass_8 = total_cost < 10
    assertion(
        8,
        "total Anthropic cost < 10 paise (synthetic input bound)",
        pass_8,
        observed={"total_cost_paise": total_cost},
        expected={"total_cost_paise_lt": 10},
    )

    return _finalise(pool, t_start)


def _finalise(pool: Any, t_start: float) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    total = time.monotonic() - t_start
    print(f"\n=== Total wall-clock: {total:.1f}s ===")
    print("=== Anthropic budget bound: ≤ 10 paise (synthetic input) ===")

    # ---------------- CLEANUP ----------------
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if INSERTED_RUN_IDS:
                cur.execute(
                    "DELETE FROM pipeline_steps WHERE run_id = ANY(%s)",
                    (INSERTED_RUN_IDS,),
                )
                cur.execute(
                    "DELETE FROM pipeline_runs WHERE id = ANY(%s)",
                    (INSERTED_RUN_IDS,),
                )
                cur.execute(
                    "DELETE FROM pipeline_log WHERE run_id = ANY(%s)",
                    (INSERTED_RUN_IDS,),
                )
            if INSERTED_TENANT_IDS:
                cur.execute(
                    "DELETE FROM privacy_audit_log WHERE tenant_id = ANY(%s)",
                    (INSERTED_TENANT_IDS,),
                )
                cur.execute(
                    "DELETE FROM phone_token_resolutions WHERE tenant_id = ANY(%s)",
                    (INSERTED_TENANT_IDS,),
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
    print(f"\nALL {len(RESULTS)} ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
