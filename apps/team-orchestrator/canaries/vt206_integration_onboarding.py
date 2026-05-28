#!/usr/bin/env python3
"""VT-206 Integration Agent onboarding canary (Rule #15, DR-15).

Substrate-only verification (no orchestrator boot; no Anthropic). The
6 brief assertions split into:

- A1: pre_filter regex detects "I want to use Shopify" → integration_intent
  (the deterministic intent classifier from VT-206 Q4)
- A2: tenant_integration_state migration applied; 5 valid phases enforced
  by CHECK constraint (insert invalid phase → fails)
- A3: PendingOwnerInput Pydantic model validates the JSONB write shape;
  extra fields rejected (Q2 flag)
- A4: integration_escalate_to_fazal tool returns the expected ack shape
- A5: dedupe on UPSERT — second insert with same tenant_id updates not duplicates
- A6: render_connector_listing_markdown produces the listing the agent's
  system prompt embeds (deterministic + non-empty)

Orchestrator boot + real Anthropic invocation deferred to a follow-up
integration canary (VT-N) when the orchestrator E2E rerun cycle picks
this up.

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      ./.venv/bin/python canaries/vt206_integration_onboarding.py
    )

Wall-clock < 10s. Cost: 0 paise. NO Anthropic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

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


def _preflight() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "PREFLIGHT FAIL — ANTHROPIC_API_KEY present; substrate canary "
            "must NOT source anthropic.env (defense-in-depth DR-15)",
            file=sys.stderr,
        )
        sys.exit(2)
    print("PREFLIGHT OK — db only; no Anthropic")


def run_canary() -> int:
    _preflight()

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

    # ---------------- A1 — pre_filter intent regex ----------------
    from orchestrator.pre_filter_gate import _INTEGRATION_INTENT_RE

    matches = [
        "I want to use Shopify",
        "Add integration",
        "connect my data",
        "setup shopify",
        "integrate woocommerce",
        "set me up",
    ]
    misses = [
        "I want to use my brain",
        "I want to use this for marketing",
        "how do I respond",
        "stop",
    ]
    match_results = [(s, bool(_INTEGRATION_INTENT_RE.search(s))) for s in matches]
    miss_results = [(s, bool(_INTEGRATION_INTENT_RE.search(s))) for s in misses]
    pass_1 = (
        all(m[1] for m in match_results) and not any(m[1] for m in miss_results)
    )
    assertion(
        1,
        "pre_filter regex: matches integration phrases; rejects ambiguous 'use' phrases",
        pass_1,
        observed={
            "matches": [s for s, hit in match_results if hit],
            "missed_phrases": [s for s, hit in match_results if not hit],
            "false_positives": [s for s, hit in miss_results if hit],
        },
        expected={"all_matches_hit": True, "no_false_positives": True},
    )

    # ---------------- A2 — migration applied + 5-phase CHECK ----------------
    tenant_a = uuid4()
    INSERTED_TENANT_IDS.append(str(tenant_a))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'paid_active') ON CONFLICT (id) DO NOTHING",
            (str(tenant_a), f"vt206-canary-{tenant_a.hex[:6]}"),
        )
    invalid_rejected = False
    valid_inserted = False
    with pool.connection() as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO tenant_integration_state (tenant_id, phase) "
                "VALUES (%s, 'phase_99_invalid')",
                (str(tenant_a),),
            )
        except Exception:  # noqa: BLE001 — CHECK violation
            invalid_rejected = True
        cur.execute(
            "INSERT INTO tenant_integration_state (tenant_id, phase) "
            "VALUES (%s, 'phase_1_discovery') ON CONFLICT (tenant_id) DO NOTHING",
            (str(tenant_a),),
        )
        cur.execute(
            "SELECT phase FROM tenant_integration_state WHERE tenant_id = %s",
            (str(tenant_a),),
        )
        row = cur.fetchone()
        valid_inserted = row is not None and row["phase"] == "phase_1_discovery"
    pass_2 = invalid_rejected and valid_inserted
    assertion(
        2,
        "tenant_integration_state CHECK rejects invalid phase; accepts valid",
        pass_2,
        observed={"invalid_rejected": invalid_rejected, "valid_inserted": valid_inserted},
        expected={"invalid_rejected": True, "valid_inserted": True},
    )

    # ---------------- A3 — PendingOwnerInput Pydantic ----------------
    from orchestrator.agent.integration_agent import PendingOwnerInput

    good = PendingOwnerInput(
        awaiting="connector_choice",
        prompt_text="Which tool do you use for customer data?",
        valid_responses=["google_sheet", "shopify"],
    )
    extra_field_rejected = False
    try:
        PendingOwnerInput.model_validate({
            "awaiting": "connector_choice",
            "prompt_text": "x",
            "rogue_field": "x",  # extra="forbid"
        })
    except Exception:  # noqa: BLE001
        extra_field_rejected = True
    invalid_kind_rejected = False
    try:
        PendingOwnerInput.model_validate({
            "awaiting": "not_a_kind",
            "prompt_text": "x",
        })
    except Exception:  # noqa: BLE001
        invalid_kind_rejected = True
    pass_3 = good.awaiting == "connector_choice" and extra_field_rejected and invalid_kind_rejected
    assertion(
        3,
        "PendingOwnerInput: good payload validates; extra fields + invalid kind rejected",
        pass_3,
        observed={
            "good_awaiting": good.awaiting,
            "extra_field_rejected": extra_field_rejected,
            "invalid_kind_rejected": invalid_kind_rejected,
        },
        expected={"extra_field_rejected": True, "invalid_kind_rejected": True},
    )

    # ---------------- A4 — escalate tool returns ack ----------------
    from orchestrator.agent.integration_agent import integration_escalate_to_fazal

    ack = integration_escalate_to_fazal.invoke({
        "run_id": "test-run-1",
        "reason": "owner stuck at OAuth",
        "owner_stuck_at": "phase_2_auth",
    })
    pass_4 = "[escalated]" in ack and "owner stuck" in ack
    assertion(
        4,
        "integration_escalate_to_fazal returns escalation ack",
        pass_4,
        observed={"ack": ack},
        expected={"ack_contains": "[escalated]"},
    )

    # ---------------- A5 — UPSERT idempotency ----------------
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_integration_state (tenant_id, phase) "
            "VALUES (%s, 'phase_2_auth') "
            "ON CONFLICT (tenant_id) DO UPDATE SET phase = EXCLUDED.phase, "
            "updated_at = now()",
            (str(tenant_a),),
        )
        cur.execute(
            "SELECT COUNT(*) AS n, MAX(phase) AS phase "
            "FROM tenant_integration_state WHERE tenant_id = %s",
            (str(tenant_a),),
        )
        row = cur.fetchone()
    pass_5 = (
        row is not None
        and int(row["n"]) == 1
        and row["phase"] == "phase_2_auth"
    )
    assertion(
        5,
        "UPSERT on tenant_id PK: phase advances, no duplicate row",
        pass_5,
        observed={"row_count": int(row["n"]) if row else None, "phase": row["phase"] if row else None},
        expected={"row_count": 1, "phase": "phase_2_auth"},
    )

    # ---------------- A6 — render_connector_listing_markdown ----------------
    from orchestrator.integrations import render_connector_listing_markdown

    rendered = render_connector_listing_markdown()
    pass_6 = (
        "google_sheet" in rendered
        and "shopify" in rendered
        and "paper_book" in rendered
        and len(rendered) > 500
    )
    assertion(
        6,
        "Integration Agent prompt embeds VT-205 registry listing",
        pass_6,
        observed={"rendered_bytes": len(rendered), "has_google_sheet": "google_sheet" in rendered},
        expected={"has_google_sheet": True, "has_shopify": True, "has_paper_book": True},
    )

    return _finalise(pool)


def _finalise(pool: Any) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")
    print("\n=== Anthropic cost: 0 paise (substrate canary; no LLM) ===")
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if INSERTED_TENANT_IDS:
                cur.execute(
                    "DELETE FROM tenant_integration_state WHERE tenant_id = ANY(%s)",
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
