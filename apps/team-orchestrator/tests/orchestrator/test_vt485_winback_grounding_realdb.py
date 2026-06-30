"""VT-485 — SR win-back grounding: purchase-ledger recency + self_evaluate context.

The defect (last-mile drive): a legitimately-lapsed customer produced
``insufficient_data`` → self_evaluate CORRECTLY rejected (missing target_cohort /
campaign_window / expected_arrr). Two root causes:

  (a) SR recency used ``customers.last_inbound_at`` ALONE (NULL for Shopify-
      sourced customers who never messaged) instead of the purchase-ledger
      ``entry_date``. A customer lapsed BY PURCHASE surfaced no dormant cohort.
  (b) ``SelfEvaluateAdapter.evaluate`` passed ``context_summary={}`` so the gate
      could not verify the plan's grounding.

This suite proves the fix on a REAL DB (DATABASE_URL; CL-422 synthetic; skip when
unset) WITHOUT a real LLM (the agent loop + the gate seam are both injected):

  1. A Shopify-style customer (NULL last_inbound_at) with a 90+-day-old PURCHASE
     surfaces a dormant cohort — ``_build_ledger_summary`` recency is populated
     from the purchase ledger, and the bundle-derived context_summary carries
     the cohort/recency/expected-ARRR grounding (root cause a + the substrate for
     b).
  2. Driving ``run_sales_recovery_agent`` with that grounded bundle: the agent
     (injected) emits a grounded PROPOSED CampaignPlan, the gate (a real
     ``SelfEvaluateAdapter`` over a mocked Opus that is fed the REAL
     context_summary) PASSES → a send-ready, self_evaluate=PASSED draft ships.
  3. The gate is NOT weakened: a fabricated/thin draft still drives REVISE →
     REJECTED (the gate continues to reject genuinely thin data).

Real-LLM end-to-end (the brain→SR→gate-passed armed draft) runs on deployed dev
— see VT-485. This suite locks the deterministic grounding chain.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("pydantic")
pytest.importorskip("anthropic")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-485 win-back grounding canary skipped",
)


# --- fixtures + seed helpers -------------------------------------------------


@pytest.fixture(scope="module")
def pool():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _seed_tenant(dsn: str, *, business_type: str = "apparel", ownership_verified: bool = True) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, business_type, plan_tier, phase, ownership_verified) "
            "VALUES ('vt485 winback', %s, 'founding', 'paid_at_risk', %s) RETURNING id",
            (business_type, ownership_verified),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_run(dsn: str, tenant_id: UUID) -> UUID:
    run_id = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running')",
            (str(run_id), str(tenant_id)),
        )
    return run_id


def _seed_shopify_customer_with_old_purchase(
    dsn: str, tenant_id: UUID, *, days_ago: int, amount_paise: int
) -> UUID:
    """A Shopify-sourced customer: NULL last_inbound_at (never messaged), with a
    purchase ``days_ago`` days back. THIS is the customer the old recency logic
    excluded (last_inbound_at IS NOT NULL filter) → no dormant cohort."""
    cid = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customers (id, tenant_id, display_name, last_inbound_at, "
            " acquired_via, opt_out_status) "
            "VALUES (%s, %s, 'Synthetic Buyer', NULL, ARRAY['apify_gbp'], 'subscribed')",
            (str(cid), str(tenant_id)),
        )
        conn.execute(
            "INSERT INTO customer_ledger_entries "
            "(tenant_id, customer_id, amount_paise, entry_type, entry_date, "
            " acquired_via, source_confidence, entry_key) "
            "VALUES (%s, %s, %s, 'sale', (now()::date - %s), 'apify_gbp', 1.0, %s)",
            (str(tenant_id), str(cid), amount_paise, days_ago, uuid4().hex),
        )
    return cid


# --- 1. the dormant cohort surfaces from purchase recency (root cause a) -----


def test_purchase_lapsed_customer_surfaces_grounded_context_summary(pool):
    """A Shopify customer (NULL inbound) with a 90-day-old purchase → the ledger
    summary recency is populated FROM THE PURCHASE, and the bundle-derived
    self_evaluate context_summary carries the cohort/recency/expected-ARRR
    grounding (the substrate the gate needs to verify a win-back)."""
    from orchestrator.context_builder import (
        build_self_evaluate_context_summary,
        build_sales_recovery_context,
    )

    dsn = os.environ["DATABASE_URL"]
    tenant_id = _seed_tenant(dsn)
    run_id = _seed_run(dsn, tenant_id)
    _seed_shopify_customer_with_old_purchase(
        dsn, tenant_id, days_ago=92, amount_paise=120_000
    )

    bundle = build_sales_recovery_context(
        tenant_id, run_id, "weekly_cadence",
        "Recover dormant customers who haven't bought recently",
    )

    ls = bundle.customer_ledger_summary
    # Root cause (a): recency surfaces FROM THE PURCHASE (NULL last_inbound_at no
    # longer excludes the customer). Pre-fix this map was empty → no cohort.
    assert ls.total_customers == 1
    assert ls.recency_days_pctl != {}, (
        "purchase-lapsed customer must surface a recency distribution (VT-485 a)"
    )
    assert ls.recency_days_pctl["p50"] == 92
    assert ls.spend_paise_pctl["p50"] == 120_000

    # Root cause (b) substrate: the gate's context_summary is non-empty and
    # carries the real grounding (not the old {}).
    summary = build_self_evaluate_context_summary(bundle)
    assert summary["customer_ledger_summary"]["total_customers"] == 1
    assert summary["customer_ledger_summary"]["recency_days_pctl"]["p50"] == 92
    assert "purchase" in summary["customer_ledger_summary"]["recency_basis"]
    assert summary["expected_arrr_target_paise"] > 0
    assert "attribution_snapshot" in summary


# --- 2 + 3. gate ships a PASSED draft on legit; still rejects thin -----------


def _grounded_proposed_plan_json(cid: UUID) -> dict[str, Any]:
    """A grounded PROPOSED CampaignPlan the injected agent emits — every required
    field (target_cohort / campaign_window / expected_arrr) present and grounded
    in the seeded ledger. (tenant_id / run_id / generated_at are overwritten by
    the agent's identity-injection coercion.)"""
    start = datetime.now(UTC) + timedelta(days=1)
    end = start + timedelta(days=14)
    return {
        "status": "proposed",
        "campaign_window": {"start": start.isoformat(), "end": end.isoformat()},
        "target_cohort": {
            "customer_ids": [str(cid)],
            "cohort_label": "90-day purchase-lapsed buyers",
            "cohort_size": 1,
            "selection_reason": (
                "one customer last purchased ~92 days ago [E1] — dormant by "
                "purchase recency for this tenant."
            ),
        },
        "expected_arrr": {
            "low_paise": 50_000,
            "high_paise": 120_000,
            "confidence": "medium",
            "basis": (
                "lifetime spend ₹1,200 for the lapsed buyer [E1]; a single "
                "re-engagement recovers a fraction of that band."
            ),
        },
        "evidence_refs": [
            {
                "claim_id": "E1",
                "source_kind": "tool_call",
                "source_id": "customer_ledger_summary",
            }
        ],
        "message_plan": {
            "template_id": "team_winback_simple",
            "template_params": {"customer_name": "there", "business_name": "Shop"},
            "language": "en",
            "personalization": "warm, no pressure",
        },
    }


def _make_agent_client(plan_json: dict[str, Any]):
    """A fake Anthropic client for the AGENT loop — emits ``plan_json`` once at a
    terminal (no tool_use) turn, then the loop coerces + gates it."""

    class _Client:
        def __init__(self) -> None:
            self.messages = self

        def create(self, **kwargs: Any) -> Any:
            block = SimpleNamespace(type="text", text=json.dumps(plan_json))
            return SimpleNamespace(
                id="msg_agent",
                usage=SimpleNamespace(input_tokens=300, output_tokens=120),
                content=[block],
                stop_reason="end_turn",
            )

    return _Client()


def _make_gate_client(verdict_payload: dict[str, Any], captured: dict[str, Any]):
    """A fake Anthropic client for the GATE (self_evaluate) — returns a scripted
    verdict and CAPTURES the context_summary the adapter forwarded (proves the
    real grounding reached the gate, not {})."""

    class _GateClient:
        def __init__(self) -> None:
            self.messages = self

        def create(self, **kwargs: Any) -> Any:
            user_payload = json.loads(kwargs["messages"][0]["content"])
            captured["context_summary"] = user_payload["context_summary"]
            block = SimpleNamespace(type="text", text=json.dumps(verdict_payload))
            return SimpleNamespace(
                id="msg_gate",
                usage=SimpleNamespace(input_tokens=200, output_tokens=40),
                content=[block],
                stop_reason="end_turn",
            )

    return _GateClient()


def _drive(bundle, *, agent_client, gate_client, monkeypatch):
    """Run the agent loop with injected agent + a real SelfEvaluateAdapter whose
    Opus client is the injected gate client, and the bundle-derived
    context_summary plumbed in (mirrors sales_recovery_node)."""
    from orchestrator.agent import sales_recovery as sr_mod
    from orchestrator.agent.tools.self_evaluate import (
        SelfEvaluateAdapter,
        SelfEvaluateTool,
    )
    from orchestrator.context_builder import build_self_evaluate_context_summary
    from team_shared.mcp import ToolContext
    from team_shared.mcp.test_harness import no_op_db_factory

    monkeypatch.setenv("VIABE_ENV", "test")
    monkeypatch.setattr(sr_mod, "Anthropic", lambda: agent_client)
    monkeypatch.setattr(
        SelfEvaluateTool, "_make_client", classmethod(lambda cls: gate_client)
    )

    ctx = ToolContext(
        tenant_id=bundle.tenant_id, run_id=bundle.run_id, agent_id="sales_recovery",
        parent_tool_call_id=None, cost_budget_remaining_paise=10_000,
        wallclock_remaining_ms=60_000, db_handle=no_op_db_factory,
    )
    summary = build_self_evaluate_context_summary(bundle)
    evaluator = SelfEvaluateAdapter(ctx=ctx, context_summary=summary)
    return sr_mod.run_sales_recovery_agent(bundle, evaluator=evaluator), summary


def test_legit_lapsed_customer_passes_gate_with_send_ready_draft(pool, monkeypatch):
    """End-to-end (deterministic): grounded bundle → agent emits a grounded
    PROPOSED plan → the gate (fed the REAL context_summary) PASSES → a send-ready,
    self_evaluate=PASSED CampaignPlan ships."""
    from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan
    from orchestrator.context_builder import build_sales_recovery_context

    dsn = os.environ["DATABASE_URL"]
    tenant_id = _seed_tenant(dsn)
    run_id = _seed_run(dsn, tenant_id)
    cid = _seed_shopify_customer_with_old_purchase(
        dsn, tenant_id, days_ago=92, amount_paise=120_000
    )
    bundle = build_sales_recovery_context(
        tenant_id, run_id, "weekly_cadence", "Recover lapsed buyers"
    )

    captured: dict[str, Any] = {}
    pass_verdict = {
        "outcome": "pass",
        "feedback": {"schema": None, "pillar": None, "consistency": None, "legal": None},
    }
    result, summary = _drive(
        bundle,
        agent_client=_make_agent_client(_grounded_proposed_plan_json(cid)),
        gate_client=_make_gate_client(pass_verdict, captured),
        monkeypatch=monkeypatch,
    )

    diag = {"status": result.status, "output_keys": sorted((result.output or {}).keys())}
    assert result.status == "completed", diag
    plan = parse_campaign_plan(result.output)
    assert plan.status.value == "proposed", diag
    # The send-ready grounded fields are all present.
    assert plan.target_cohort.cohort_size == 1
    assert plan.expected_arrr.high_paise >= plan.expected_arrr.low_paise
    assert plan.self_evaluate_status.value == "passed", diag
    # Proof the gate was fed the REAL grounding (root cause b), not {}.
    assert captured["context_summary"], "gate must receive a non-empty context_summary"
    assert captured["context_summary"]["customer_ledger_summary"]["total_customers"] == 1
    assert captured["context_summary"]["customer_ledger_summary"]["recency_days_pctl"]["p50"] == 92


def test_gate_still_rejects_a_thin_draft(pool, monkeypatch):
    """The gate is NOT weakened: when self_evaluate REVISEs (twice, per the
    two-revise-then-reject policy), the run is REJECTED and the draft is stamped
    FAILED_AFTER_REVISIONS — a genuinely thin/bogus plan never ships as passed."""
    from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan
    from orchestrator.context_builder import build_sales_recovery_context

    dsn = os.environ["DATABASE_URL"]
    tenant_id = _seed_tenant(dsn)
    run_id = _seed_run(dsn, tenant_id)
    cid = _seed_shopify_customer_with_old_purchase(
        dsn, tenant_id, days_ago=92, amount_paise=120_000
    )
    bundle = build_sales_recovery_context(
        tenant_id, run_id, "weekly_cadence", "Recover lapsed buyers"
    )

    captured: dict[str, Any] = {}
    revise_verdict = {
        "outcome": "revise",
        "feedback": {
            "consistency": [
                "target_cohort.cohort_size=1 but expected_arrr.high_paise band is "
                "implausible for a single lapsed buyer — ungrounded."
            ],
            "schema": None,
            "pillar": None,
            "legal": None,
        },
    }
    # The agent re-emits the same plan on the retry turn; the gate REVISEs both
    # times → after max_revisions the gate REJECTS (never ships known-bad).
    result, _ = _drive(
        bundle,
        agent_client=_make_agent_client(_grounded_proposed_plan_json(cid)),
        gate_client=_make_gate_client(revise_verdict, captured),
        monkeypatch=monkeypatch,
    )

    diag = {"status": result.status}
    assert result.status == "rejected", diag
    plan = parse_campaign_plan(result.output)
    assert plan.self_evaluate_status.value == "failed_after_revisions", diag
    # Even on rejection the gate still saw the real context_summary.
    assert captured["context_summary"], "gate must receive a non-empty context_summary"
