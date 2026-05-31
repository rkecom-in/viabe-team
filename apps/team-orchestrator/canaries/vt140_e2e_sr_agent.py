#!/usr/bin/env python3
"""VT-140 — Sprint 1+2 SR-agent E2E canary (the capstone, LIVE mode).

Rule #15 / DR-15 canary: drives the FULL owner-facing recovery loop on a
SYNTHETIC tenant (CL-422) through the REAL production seam — ``dispatch_brain``
(real Opus) → supervisor → SR agent → collapse → request_owner_approval (PAUSE)
→ resume_run(approved) → campaign_execute → VT-45 per-recipient send (Twilio
DRY-RUN, always mock — never a real WhatsApp message) → match_transactions →
get_attribution_data.

Two-mode contract (CL-274): the CI test (tests/orchestrator/test_e2e_sr_agent.py)
mocks Anthropic with a deterministic proposed plan; THIS canary is the live
fidelity proof. Twilio is mock in BOTH modes (dry-run is the contract — DEC-3).

Gating (fail-not-skip):
  - VT140_LIVE=1                — opt into the live canary (else exits 0, noop).
  - ANTHROPIC_API_KEY (sk-ant-) — real Opus dispatch.
  - DATABASE_URL                — real Postgres (synthetic seed + RLS reads).
Any missing gate when VT140_LIVE=1 → exit 2 (preflight fail, NOT silent skip).

CL-390: logs ids / counts / SIDs only — never phone / body / names.

Run:
    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      source ../../.viabe/secrets/anthropic.env
      set +a
      VT140_LIVE=1 TEAM_TWILIO_MOCK_MODE=1 \
        ./.venv/bin/python canaries/vt140_e2e_sr_agent.py
    )

Live-mode budgets (generous per plan §3): wall-clock <= 90s, cost <= 2000 paise.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

SRC = Path(__file__).resolve().parent.parent / "src"
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
TESTS = Path(__file__).resolve().parent.parent / "tests" / "orchestrator"
for p in (SRC, SCRIPTS, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


_LIVE_BUDGET_WALLCLOCK_S = 90.0
_LIVE_BUDGET_COST_PAISE = 2000


def _preflight() -> str:
    """Verify the live gates. Returns the DSN. Exits non-zero on any miss."""
    if os.environ.get("VT140_LIVE") != "1":
        print(
            "VT140_LIVE != 1 — this canary runs only in live mode. "
            "The CI test (tests/orchestrator/test_e2e_sr_agent.py) covers the "
            "mock path. Exiting 0 (noop)."
        )
        sys.exit(0)

    # Twilio dry-run is the contract — never a real customer send (DEC-3).
    os.environ.setdefault("TEAM_TWILIO_MOCK_MODE", "1")

    missing: list[str] = []
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key.startswith("sk-ant-"):
        missing.append("ANTHROPIC_API_KEY (sk-ant- prefix required)")
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")
    if not dsn:
        missing.append("DATABASE_URL")
    if missing:
        print(f"PREFLIGHT FAIL — missing live gates: {missing}", file=sys.stderr)
        sys.exit(2)

    # set_config-style RLS needs the GUC plumbing; the substrate + tools handle
    # it. Twilio creds are not needed when TEAM_TWILIO_MOCK_MODE=1, but the
    # send helper still reads TEAM_TWILIO_FROM_NUMBER — provide a synthetic one.
    os.environ.setdefault("TEAM_TWILIO_FROM_NUMBER", "+910000000000")
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    print(
        f"PREFLIGHT OK — live mode; DATABASE_URL set; ANTHROPIC_API_KEY present; "
        f"TEAM_TWILIO_MOCK_MODE={os.environ['TEAM_TWILIO_MOCK_MODE']} (dry-run)."
    )
    return dsn


def run_canary() -> int:
    t_start = time.monotonic()
    dsn = _preflight()

    import apply_migrations
    from langgraph.types import Command

    from orchestrator import graph as graphmod
    from orchestrator.agent.approval_resume import (
        find_open_approval_for_tenant,
        mark_approval_resolved,
    )
    from orchestrator.db import tenant_connection

    from _e2e_seed import seed, teardown

    apply_migrations.apply(dsn=dsn)
    graphmod.reset_substrate()
    graphmod.init_substrate(dsn)

    result = seed(dsn)
    t1, t2 = result.t1, result.t2
    failures: list[str] = []

    def check(name: str, ok: bool, observed: Any = None) -> None:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name} :: observed={observed}")
        if not ok:
            failures.append(name)

    try:
        import psycopg

        # --- Drive the REAL production seam: dispatch_brain (real Opus). ---
        # The run_id MUST be the one we seeded (thread_id == run_id; the
        # checkpoint + pending_approvals + RLS all key on it).
        from orchestrator.agent.dispatch import dispatch_brain
        from orchestrator.state import SubscriberState
        from orchestrator.types import WebhookEvent

        event = WebhookEvent(
            body="Recover my dormant customers from the last 60 days",
            sender_phone=t1.whatsapp_number,
            twilio_message_sid=f"SM{t1.run_id.hex}",
        )
        state: SubscriberState = {  # type: ignore[assignment]
            "tenant_id": t1.tenant_id,
            "phase": "paid_active",
        }

        dispatch_result = dispatch_brain(
            event=event,
            state=state,
            run_id=t1.run_id,
            tenant_id=t1.tenant_id,
        )
        print(f"  dispatch_result.final_status={dispatch_result.final_status}")

        # A real Opus run that proposes a campaign PAUSES on the approval gate.
        # (If Opus declined / deferred — out_of_scope / insufficient_data — the
        # run completes without a pause; that is a valid agent verdict but means
        # the synthetic seed didn't elicit a proposal. We assert the pause path
        # since the seed is built to elicit a recovery campaign.)
        check(
            "A0 dispatch PAUSED on approval gate (real Opus proposed a campaign)",
            dispatch_result.final_status == "paused",
            observed=dispatch_result.final_status,
        )

        with psycopg.connect(dsn, autocommit=True) as conn:
            n_appr = conn.execute(
                "SELECT count(*) FROM pending_approvals WHERE run_id = %s",
                (str(t1.run_id),),
            ).fetchone()[0]
        check("A1 exactly 1 pending_approvals row at pause", n_appr == 1, n_appr)

        # --- Simulate owner 'YES': resolve the durable row + resume. ---
        with tenant_connection(t1.tenant_id) as conn:
            approval = find_open_approval_for_tenant(conn, t1.tenant_id)
            if approval is not None:
                mark_approval_resolved(conn, approval["id"], "approved",
                                       owner_message_sid="SM_owner_yes")

        # Resume on the SAME checkpointer + thread_id (production resume_run
        # rebuilds the graph; we drive it directly to keep the model cheap on
        # the resume leg — no LLM call happens on resume).
        from orchestrator.supervisor import build_supervisor_graph
        from orchestrator.agent.dispatch import _resolve_model

        graph = build_supervisor_graph(
            model=_resolve_model(), checkpointer=graphmod.get_checkpointer()
        )
        resumed = graph.invoke(
            Command(resume={"decision": "approved"}),
            config={"configurable": {"thread_id": str(t1.run_id)}},
        )
        check(
            "A2 resume yields owner_decision='approved'",
            resumed.get("owner_decision") == "approved",
            observed=resumed.get("owner_decision"),
        )
        exec_error = resumed.get("campaign_execution_error")
        exec_summary = resumed.get("campaign_execution_summary")
        check("A2b campaign_execute ran without error", exec_error is None, exec_error)

        with psycopg.connect(dsn, autocommit=True) as conn:
            crow = conn.execute(
                "SELECT id, status FROM campaigns WHERE tenant_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (str(t1.tenant_id),),
            ).fetchone()
            campaign_id = UUID(str(crow[0])) if crow else None
            campaign_status = crow[1] if crow else None
            check("A3 campaign persisted", campaign_id is not None, str(campaign_id))

            sent_rows = conn.execute(
                "SELECT customer_id, message_sid FROM campaign_messages "
                "WHERE tenant_id = %s",
                (str(t1.tenant_id),),
            ).fetchall()
            sent_ids = {str(r[0]) for r in sent_rows}
            check(
                "A4 dry-run sends recorded in campaign_messages",
                len(sent_rows) >= 1 and all(r[1] for r in sent_rows),
                observed={"count": len(sent_rows)},
            )
            opt_ok = all(str(o) not in sent_ids for o in t1.opted_out_ids)
            check("A4b opted_out recipients skipped (CL-421)", opt_ok, observed=None)
            check(
                "A5 campaigns.status == 'sent'",
                campaign_status == "sent",
                observed=campaign_status,
            )
            print(f"  exec_summary={exec_summary}")

        # --- A6 match_transactions (synthetic matching payment) ---
        from datetime import UTC, datetime
        from orchestrator.agent.tools.match_transactions import (
            MatchTransactionsInput,
            TransactionInput,
            match_transactions,
        )

        ts = datetime.now(UTC)
        match_out = match_transactions(
            MatchTransactionsInput(
                tenant_id=str(t1.tenant_id),
                transactions=[
                    TransactionInput(
                        txn_id="vt140-live-txn",
                        amount_paise=500_000,
                        timestamp=ts,
                        vpa="synthetic@upi",
                    )
                ],
            ),
            candidate_ledger=[
                {"id": "vt140-live-ledger", "amount_paise": 500_000,
                 "entry_ts": ts, "ref_vpa": "synthetic@upi"}
            ],
        )
        check(
            "A6 match_transactions returns a match",
            len(match_out.matches) >= 1,
            observed={"matches": len(match_out.matches)},
        )

        # Seed an attributions row (VT-176 close path is async; do it here so
        # A7 reads a real snapshot) — only if the loop produced a campaign.
        if campaign_id is not None and match_out.matches:
            m = match_out.matches[0]
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(
                    "INSERT INTO attributions (tenant_id, campaign_id, customer_id, "
                    "attributed_paise, attribution_method, attribution_confidence) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (str(t1.tenant_id), str(campaign_id),
                     str(t1.subscribed_ids[0]), 500_000,
                     m.attribution_method, m.confidence),
                )

        # --- A7 get_attribution_data ---
        from orchestrator.agent.tools.get_attribution_data import (
            GetAttributionDataInput,
            get_attribution_data,
        )

        if campaign_id is not None:
            attr = get_attribution_data(
                GetAttributionDataInput(
                    tenant_id=str(t1.tenant_id), campaign_id=str(campaign_id)
                )
            )
            check(
                "A7 attribution reachable/computed",
                attr.campaign is not None and attr.campaign.transacting_count >= 1,
                observed={"transacting": attr.campaign.transacting_count if attr.campaign else None},
            )

        # --- A8 ZERO cross-tenant leakage ---
        with tenant_connection(t1.tenant_id) as conn:
            t2_cids = [str(c) for c in t2.customers.keys()]
            row = conn.execute(
                "SELECT count(*) FROM customers WHERE id = ANY(%s)", (t2_cids,)
            ).fetchone()
            n = row["count"] if isinstance(row, dict) else row[0]
            row2 = conn.execute(
                "SELECT count(*) FROM campaigns WHERE id = %s",
                (str(result.t2_campaign_id),),
            ).fetchone()
            n2 = row2["count"] if isinstance(row2, dict) else row2[0]
        check("A8 zero cross-tenant leakage (T2 invisible under T1)",
              n == 0 and n2 == 0, observed={"t2_customers": n, "t2_campaign": n2})

        # --- Live budgets ---
        elapsed = time.monotonic() - t_start
        check(
            f"A9 wall-clock <= {_LIVE_BUDGET_WALLCLOCK_S}s",
            elapsed <= _LIVE_BUDGET_WALLCLOCK_S,
            observed={"elapsed_s": round(elapsed, 1)},
        )
        with psycopg.connect(dsn, autocommit=True) as conn:
            cost_row = conn.execute(
                "SELECT COALESCE(SUM(cost_paise), 0) FROM pipeline_steps "
                "WHERE run_id = %s",
                (str(t1.run_id),),
            ).fetchone()
            total_cost = int(cost_row[0] or 0)
        check(
            f"A10 cost <= {_LIVE_BUDGET_COST_PAISE} paise (proof-of-call: > 0)",
            0 < total_cost <= _LIVE_BUDGET_COST_PAISE,
            observed={"cost_paise": total_cost},
        )
    finally:
        teardown(dsn, result)
        graphmod.reset_substrate()

    print(f"\n=== VT-140 LIVE CANARY: {len(failures)} failure(s) "
          f"in {time.monotonic() - t_start:.1f}s ===")
    if failures:
        print(f"FAILED: {failures}", file=sys.stderr)
        return 1
    print("ALL ASSERTIONS PASSED — Sprint 1+2 loop proven on the live seam.")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
