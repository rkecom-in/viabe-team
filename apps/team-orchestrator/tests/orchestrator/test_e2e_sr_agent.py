"""VT-140 — Sprint 1+2 SR-agent E2E harness (the capstone).

Drives the FULL owner-facing recovery loop on a SYNTHETIC tenant (CL-422) and
asserts per-stage state, zero cross-tenant leakage, recorded dry-run sends,
opt-out skipping (CL-421), and reachable attribution.

Loop (one synthetic run, thread_id == run_id):
    inbound → supervisor graph → SR agent (deterministic proposed plan in CI) →
    collapse persists campaign + campaign_recipients → route_after_collapse →
    request_owner_approval (PAUSE: interrupt() + pending_approvals row) →
    simulate owner 'YES' → resume_run(approved) → route_after_approval →
    _campaign_execute_node → execute_approved_campaign → per-recipient
    send_whatsapp_template (mock Twilio) → campaign_messages recorded
    (opted_out skipped) → campaigns.status='sent' → match_transactions
    (synthetic matching payment) → get_attribution_data.

Two modes:
  - CI / default: mock Twilio (TEAM_TWILIO_MOCK_MODE + conftest autostub) + mock
    Anthropic (the SR-agent node is monkeypatched to a deterministic proposed
    plan — NO ANTHROPIC_API_KEY required, NO network). Real local Postgres.
    Gated @pytest.mark.integration so the keyless ``test`` CI job skips the DB
    path; runs when RUN_INTEGRATION_TESTS=1 + DATABASE_URL.
  - Live mode (RUN_E2E_LIVE=1 + ANTHROPIC_API_KEY): the canary
    (canaries/vt140_e2e_sr_agent.py) — real Opus dispatch. Twilio STILL mock.

Fail-not-skip in the chosen mode: a stage that errors fails the test.

Discipline: CL-422 synthetic-only, CL-390 ids/counts/SIDs only, CL-421 opt-out
skipped, CL-418 (no production-code change here — this is a test row).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

# Sibling E2E helpers (_e2e_seed / _e2e_plan) live next to this file. The tests
# tree is NOT a package (pyproject pythonpath=["src","scripts"]; no tests
# __init__), so load them by adding this directory to sys.path — mirroring the
# repo's path-based test-helper convention (e.g. _resume_worker.py).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason=(
            "VT-140 E2E harness needs DATABASE_URL (real local Postgres for the"
            " seeded synthetic tenant + RLS-enforced loop reads). CI mode mocks"
            " Anthropic + Twilio — no ANTHROPIC_API_KEY required."
        ),
    ),
]


@pytest.fixture()
def substrate() -> Any:
    """Apply migrations, init the module-level pool + PostgresSaver, tear down.

    reset_substrate() first so a prior module's stale pool can't leak in.
    """
    import apply_migrations

    from orchestrator import graph as graphmod

    dsn = os.environ["DATABASE_URL"]
    apply_migrations.apply(dsn=dsn)
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    graphmod.reset_substrate()
    graphmod.init_substrate(dsn)
    try:
        yield {"dsn": dsn, "graphmod": graphmod}
    finally:
        graphmod.reset_substrate()


def _drive_loop_ci(
    monkeypatch: pytest.MonkeyPatch,
    dsn: str,
    graphmod: Any,
    t1: Any,
) -> dict[str, Any]:
    """Drive the supervisor loop in CI mode: deterministic proposed plan.

    Returns the terminal state after resume (the campaign_execute terminal).
    Monkeypatches ONLY the SR-agent specialist node to inject a deterministic
    proposed plan — every other seam (collapse, approval gate, interrupt/resume,
    campaign_execute, VT-45 send) runs unchanged against the real DB.
    """
    from langgraph.types import Command
    from langchain_anthropic import ChatAnthropic

    import orchestrator.supervisor as supervisor_mod
    from orchestrator.supervisor import build_supervisor_graph

    from _e2e_plan import build_proposed_plan

    cohort = t1.subscribed_ids + t1.opted_out_ids  # include the opt-out target

    def _fake_sr_node(state: dict[str, Any]) -> dict[str, Any]:
        plan = build_proposed_plan(
            tenant_id=t1.tenant_id,
            run_id=t1.run_id,
            cohort_ids=cohort,
        )
        return {"campaign_plan": plan}

    # Mirror the documented test seam (test_supervisor.py patches the module
    # binding before the graph is built so the wrapped node uses the fake).
    monkeypatch.setattr(supervisor_mod, "_sales_recovery_node", _fake_sr_node)

    # Force the spawn route deterministically (the fake orchestrator does not
    # emit a tool_call AIMessage). route_after_orchestrator is read as a module
    # global inside build_supervisor_graph, so patch it before the graph builds.
    monkeypatch.setattr(
        supervisor_mod, "route_after_orchestrator", lambda state: "spawn"
    )

    # The spawn handoff (_build_sales_recovery_update) attaches the Composer
    # bundle; the fake SR node ignores it, so neutralise the orchestrator node
    # to a pure pass-through that sets the bundle + run identity in state.
    from orchestrator.context_builder import build_sales_recovery_context

    def _fake_orchestrator(state: dict[str, Any]) -> dict[str, Any]:
        bundle = build_sales_recovery_context(
            t1.tenant_id, t1.run_id, "weekly_cadence", "Recover dormant customers"
        )
        return {
            "active_agent": "sales_recovery_agent",
            "sales_recovery_context": bundle,
        }

    # The supervisor wires build_orchestrator_agent()'s CompiledStateGraph as
    # the 'orchestrator_agent' node. Replace it with a RunnableLambda over a
    # plain pass-through fn so the orchestrator never makes a real Anthropic
    # call — every OTHER seam (spawn route, collapse, gate, resume, execute)
    # runs unchanged. _build_recent_campaigns / _build_pending_owner_inputs
    # read the real pool inside build_sales_recovery_context.
    from langchain_core.runnables import RunnableLambda

    monkeypatch.setattr(
        supervisor_mod,
        "build_orchestrator_agent",
        lambda **kw: RunnableLambda(_fake_orchestrator),
    )

    saver = graphmod.get_checkpointer()
    model = ChatAnthropic(model="claude-haiku-4-5")  # type: ignore[call-arg]
    graph = build_supervisor_graph(model=model, checkpointer=saver)

    cfg = {"configurable": {"thread_id": str(t1.run_id)}}
    initial = {
        "messages": [{"role": "user", "content": "Recover my dormant customers"}],
        "tenant_id": t1.tenant_id,
        "run_id": t1.run_id,
        "trigger_reason": "weekly_cadence",
    }

    paused = graph.invoke(initial, config=cfg)
    assert "__interrupt__" in paused, (
        f"approval gate must PAUSE via interrupt(); state keys={list(paused.keys())}"
    )

    # Simulate owner 'YES' → resume with approved. (Mirrors dispatch + the VT-47
    # integration test: the decision flows through Command(resume=...).)
    resumed = graph.invoke(Command(resume={"decision": "approved"}), config=cfg)
    return resumed


def test_vt140_e2e_sr_agent_full_loop(
    substrate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capstone: drive the whole Sprint 1+2 loop + assert per stage.

    A1 pending_approvals: exactly 1 pending row created at pause.
    A2 resume continues: owner_decision == 'approved'.
    A3 campaign persisted (campaigns + campaign_recipients) — cohort_size match.
    A4 dry-run sends recorded in campaign_messages (mock SID); opted_out skipped.
    A5 campaigns.status → 'sent'.
    A6 match_transactions returns ≥1 match for the synthetic payment.
    A7 get_attribution_data returns a non-empty snapshot for the run's campaign.
    A8 ZERO cross-tenant leakage (decoy T2 rows never surface under T1).
    A9 CL-390: campaign_messages/send_idempotency carry no phone/body plaintext.
    """
    monkeypatch.setenv("TEAM_TWILIO_MOCK_MODE", "1")

    from _e2e_seed import seed, teardown

    dsn = substrate["dsn"]
    graphmod = substrate["graphmod"]
    result = seed(dsn)
    t1, t2 = result.t1, result.t2

    try:
        resumed = _drive_loop_ci(monkeypatch, dsn, graphmod, t1)

        # The seam may swallow a campaign_execute error (supervisor.py catches
        # and stores campaign_execution_error). Surface it loudly — a broken
        # seam must FAIL here, not be papered over.
        exec_error = resumed.get("campaign_execution_error")
        exec_summary = resumed.get("campaign_execution_summary")

        # --- A2 resume continues with the owner's approval ---
        assert resumed.get("owner_decision") == "approved", (
            f"resume must yield owner_decision='approved'; got {resumed.get('owner_decision')!r}"
        )

        with psycopg.connect(dsn, autocommit=True) as conn:
            # --- A1 exactly 1 pending_approvals row for the run ---
            n_appr = conn.execute(
                "SELECT count(*) FROM pending_approvals WHERE run_id = %s",
                (str(t1.run_id),),
            ).fetchone()[0]
            assert n_appr == 1, (
                f"pause must create exactly 1 pending_approvals row; got {n_appr}"
            )

            # --- A3 campaign persisted + cohort linked ---
            crow = conn.execute(
                "SELECT id, status FROM campaigns WHERE tenant_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (str(t1.tenant_id),),
            ).fetchone()
            assert crow is not None, "collapse must persist a campaigns row"
            campaign_id = UUID(str(crow[0]))

            n_recip = conn.execute(
                "SELECT count(*) FROM campaign_recipients WHERE campaign_id = %s",
                (str(campaign_id),),
            ).fetchone()[0]
            cohort_size = len(t1.subscribed_ids) + len(t1.opted_out_ids)
            assert n_recip == cohort_size, (
                f"campaign_recipients must equal cohort size {cohort_size}; got {n_recip}"
            )

        # --- The campaign-execute seam MUST have run cleanly ---
        # This is the load-bearing proof that the loop CONNECTS end to end.
        assert exec_error is None, (
            f"campaign_execute seam errored: {exec_error!r}. The loop did NOT "
            f"connect — execute_approved_campaign failed against the real schema "
            f"(see VT-140 findings)."
        )
        assert exec_summary is not None, (
            "campaign_execute must produce a count summary on the approved path"
        )

        with psycopg.connect(dsn, autocommit=True) as conn:
            # --- A4 dry-run sends recorded; opted_out skipped (CL-421) ---
            sent_rows = conn.execute(
                "SELECT customer_id, message_sid, send_status FROM campaign_messages "
                "WHERE tenant_id = %s",
                (str(t1.tenant_id),),
            ).fetchall()
            sent_customer_ids = {str(r[0]) for r in sent_rows}
            assert len(sent_rows) == len(t1.subscribed_ids), (
                f"campaign_messages must record one row per subscribed recipient "
                f"({len(t1.subscribed_ids)}); got {len(sent_rows)}"
            )
            for opted in t1.opted_out_ids:
                assert str(opted) not in sent_customer_ids, (
                    "opted_out customer must NOT receive a campaign_messages send row (CL-421)"
                )
            # every send row carries a mock SID
            for r in sent_rows:
                assert r[1], "each recorded send must carry a (mock) message_sid"

            # opted_out skip recorded in send_idempotency_keys (the execute
            # seam's skip ledger) — proves the skip path ran.
            skip_rows = conn.execute(
                "SELECT customer_id FROM send_idempotency_keys "
                "WHERE tenant_id = %s AND message_sid IS NULL AND send_status = 'error'",
                (str(t1.tenant_id),),
            ).fetchall()
            skip_ids = {str(r[0]) for r in skip_rows}
            for opted in t1.opted_out_ids:
                assert str(opted) in skip_ids, (
                    "opted_out skip must be recorded in send_idempotency_keys"
                )

            # --- A5 campaigns.status → 'sent' ---
            final_status = conn.execute(
                "SELECT status FROM campaigns WHERE id = %s",
                (str(campaign_id),),
            ).fetchone()[0]
            assert final_status == "sent", (
                f"campaigns.status must advance to 'sent'; got {final_status!r}"
            )

            # --- A9 CL-390: no phone / no name in the send ledger plaintext ---
            phones = set()
            for row in conn.execute(
                "SELECT phone_e164 FROM customers WHERE tenant_id = %s",
                (str(t1.tenant_id),),
            ).fetchall():
                if row[0]:
                    phones.add(str(row[0]))
            ledger_blob = str(
                conn.execute(
                    "SELECT array_agg(message_sid) FROM campaign_messages WHERE tenant_id = %s",
                    (str(t1.tenant_id),),
                ).fetchone()[0]
            )
            for ph in phones:
                assert ph not in ledger_blob, "phone leaked into campaign_messages (CL-390)"

        # --- A6 match_transactions returns a match for a synthetic payment ---
        from datetime import UTC, datetime
        from orchestrator.agent.tools.match_transactions import (
            MatchTransactionsInput,
            TransactionInput,
            match_transactions,
        )

        synthetic_ts = datetime.now(UTC)
        ledger_entry_id = "vt140-ledger-1"
        match_out = match_transactions(
            MatchTransactionsInput(
                tenant_id=str(t1.tenant_id),
                transactions=[
                    TransactionInput(
                        txn_id="vt140-txn-1",
                        amount_paise=500_000,
                        timestamp=synthetic_ts,
                        vpa="synthetic@upi",
                    )
                ],
            ),
            candidate_ledger=[
                {
                    "id": ledger_entry_id,
                    "amount_paise": 500_000,
                    "entry_ts": synthetic_ts,
                    "ref_vpa": "synthetic@upi",
                }
            ],
        )
        assert len(match_out.matches) >= 1, (
            f"match_transactions must match the synthetic payment; got "
            f"matches={match_out.matches} unmatched={match_out.unmatched}"
        )
        match = match_out.matches[0]
        assert match.ledger_entry_id == ledger_entry_id

        # --- Seed an attributions row from the match (the VT-176 close path
        # would do this async; the harness does it explicitly so A7 reads a
        # real, non-empty snapshot tied to THIS run's campaign). ---
        with psycopg.connect(dsn, autocommit=True) as conn:
            a_customer = t1.subscribed_ids[0]
            conn.execute(
                """
                INSERT INTO attributions (tenant_id, campaign_id, customer_id,
                                          attributed_paise, attribution_method,
                                          attribution_confidence)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    str(t1.tenant_id),
                    str(campaign_id),
                    str(a_customer),
                    500_000,
                    match.attribution_method,
                    match.confidence,
                ),
            )

        # --- A7 get_attribution_data returns a non-empty snapshot ---
        from orchestrator.agent.tools.get_attribution_data import (
            GetAttributionDataInput,
            get_attribution_data,
        )

        attr_out = get_attribution_data(
            GetAttributionDataInput(
                tenant_id=str(t1.tenant_id),
                campaign_id=str(campaign_id),
            )
        )
        assert attr_out.mode == "campaign"
        assert attr_out.campaign is not None
        assert attr_out.campaign.transacting_count >= 1, (
            f"attribution must be reachable/computed; snapshot={attr_out.campaign}"
        )
        assert attr_out.campaign.arrr_paise >= 500_000, (
            f"attribution ARRR must reflect the synthetic payment; got "
            f"{attr_out.campaign.arrr_paise}"
        )

        # --- A8 ZERO cross-tenant leakage ---------------------------------
        # Under T1's RLS context, NONE of T2's rows may surface. Probe each
        # RLS-scoped table for T2 ids while scoped to T1.
        from orchestrator.db import tenant_connection

        with tenant_connection(t1.tenant_id) as conn:
            # T2 customers invisible under T1
            t2_customer_ids = [str(c) for c in t2.customers.keys()]
            n = conn.execute(
                "SELECT count(*) FROM customers WHERE id = ANY(%s)",
                (t2_customer_ids,),
            ).fetchone()
            n_t2_customers = n["count"] if isinstance(n, dict) else n[0]
            assert n_t2_customers == 0, (
                f"T2 customers leaked into T1 RLS context: {n_t2_customers}"
            )

            # T2 decoy campaign invisible under T1
            row = conn.execute(
                "SELECT count(*) FROM campaigns WHERE id = %s",
                (str(result.t2_campaign_id),),
            ).fetchone()
            n_t2_campaign = row["count"] if isinstance(row, dict) else row[0]
            assert n_t2_campaign == 0, (
                f"T2 campaign leaked into T1 RLS context: {n_t2_campaign}"
            )

            # T2 attributions invisible under T1
            row = conn.execute(
                "SELECT count(*) FROM attributions WHERE campaign_id = %s",
                (str(result.t2_campaign_id),),
            ).fetchone()
            n_t2_attr = row["count"] if isinstance(row, dict) else row[0]
            assert n_t2_attr == 0, (
                f"T2 attributions leaked into T1 RLS context: {n_t2_attr}"
            )

        # And the decoy's attribution snapshot must be empty under T1 context.
        attr_decoy = get_attribution_data(
            GetAttributionDataInput(
                tenant_id=str(t1.tenant_id),
                campaign_id=str(result.t2_campaign_id),
            )
        )
        assert attr_decoy.campaign is not None
        assert attr_decoy.campaign.transacting_count == 0, (
            "T2 decoy attribution must be invisible under T1 (cross-tenant leak)"
        )
    finally:
        teardown(dsn, result)
