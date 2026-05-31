"""VT-4 ship-thin — Sales Recovery Agent, real end-to-end first run.

This is the integration test that proves the agent can run **for real**
on a seeded tenant: bundle assembled by ``build_sales_recovery_context``
through ``tenant_connection`` (real DB substrate), rendered into the
prompt by ``serialize_bundle_for_prompt``, dispatched to the real
Anthropic Messages API, output round-tripped through the strict v1.0
``CampaignPlan`` union.

Env requirement (THREE-WAY, all independent gates):

  - ``RUN_INTEGRATION_TESTS=1`` (conftest hook strips
    ``@pytest.mark.integration`` skip; without it the marker collects
    a skip regardless of the keys).
  - ``ANTHROPIC_API_KEY`` (real Messages API call against Opus).
  - ``DATABASE_URL`` (real Postgres for tenant seed +
    ``tenant_connection`` reads).

``RUN_INTEGRATION_TESTS=1`` alone is insufficient. CI does NOT export
``ANTHROPIC_API_KEY``, so this test is skipped in CI. Fazal runs it
manually once before merge (cost: one Opus turn, <₹2 at Phase-1 rates).

Asserts valid-any-variant. DOES NOT assert ``status='proposed'`` —
the prompt's v1.0 contract is that empty / under-specified context
yields ``insufficient_data`` (the correct verdict). Plan-quality on a
fully-seeded fixture is a later VT row (post-ship-thin).

Proof-of-call discipline (CL-272): assertions on tokens, cost,
response_id prefix prove the network call really happened — a green
pass cannot be reached by a mock-leak path.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("dbos")
pytest.importorskip("pydantic")
pytest.importorskip("psycopg")

import psycopg  # noqa: E402

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY")
        or not os.environ.get("DATABASE_URL"),
        reason=(
            "VT-4 ship-thin end-to-end test needs ANTHROPIC_API_KEY"
            " (one real Opus messages.create) AND DATABASE_URL (tenant"
            " seed + tenant_connection bundle reads). RUN_INTEGRATION_TESTS=1"
            " alone is insufficient — all three env gates are required."
        ),
    ),
]


@pytest.fixture(scope="module")
def rls_ctx() -> Any:
    """Apply migrations + launch DBOS so the pool exists; tear down."""
    import apply_migrations  # noqa: E402

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos  # noqa: E402

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _seed_tenant(dsn: str) -> UUID:
    """Seed one tenant row via a privileged connection (RLS bypassed).

    The agent's bundle builder reads through ``tenant_connection`` (RLS
    on); the seed has to be a direct write so the test environment
    can stage the row without going through tenant context."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES (%s, 'founding', 'paid_at_risk') RETURNING id",
            ("vt4-ship-thin",),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_run(dsn: str, tenant_id: UUID) -> UUID:
    """Seed a pipeline_runs row (FK target for campaigns.run_id)."""
    run_id = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running')",
            (str(run_id), str(tenant_id)),
        )
    return run_id


def _seed_recent_campaign(dsn: str, tenant_id: UUID, run_id: UUID) -> None:
    """Seed one prior campaign so the recent_campaigns substrate
    returns a non-empty list (exercises the real VT-138 read path)."""
    plan_dict = {
        "version": "1.0",
        "status": "proposed",
        "tenant_id": str(tenant_id),
        "run_id": str(run_id),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO campaigns "
            "(tenant_id, run_id, status, plan_json, generated_at) "
            "VALUES (%s, %s, 'sent', %s, now() - interval '3 days')",
            (str(tenant_id), str(run_id), json.dumps(plan_dict)),
        )


def test_vt4_ship_thin_first_real_end_to_end_run(rls_ctx: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seeded tenant → real bundle → real Opus call → valid CampaignPlan.

    PROVES:
      (1) ``build_sales_recovery_context`` returns a bundle assembled
          from real DB substrate (recent_campaigns substrate-populated;
          other sections safe-empty per CL-190).
      (2) ``serialize_bundle_for_prompt`` renders bundle into the first
          user message (the model receives context, not a wedge).
      (3) The real Anthropic Messages API returns a response (proof:
          ``response_id`` starts with 'msg_', tokens + cost > 0).
      (4) Output round-trips through the v1.0 ``CampaignPlan`` strict
          union — agent produces a contract-valid verdict end-to-end.

    DOES NOT assert ``status='proposed'``. Under-specified context
    legitimately yields ``insufficient_data``; the brief calls for the
    minimum real path, not plan quality.
    """
    from anthropic import Anthropic as _RealAnthropic  # noqa: E402

    from orchestrator.agent.sales_recovery import (  # noqa: E402
        _resolve_model,
        run_sales_recovery_agent,
    )
    from orchestrator.agent.schemas.campaign_plan import (  # noqa: E402
        CampaignPlanInsufficientData,
        CampaignPlanOutOfScope,
        CampaignPlanProposed,
        parse_campaign_plan,
    )
    from orchestrator.context_builder import (  # noqa: E402
        build_sales_recovery_context,
    )

    # Sanity: confirm anthropic.Anthropic is the genuine SDK class.
    assert _RealAnthropic.__module__.startswith("anthropic"), (
        f"anthropic.Anthropic appears non-genuine: "
        f"module={_RealAnthropic.__module__!r}"
    )

    dsn = rls_ctx.dsn
    tenant_id = _seed_tenant(dsn)
    run_id = _seed_run(dsn, tenant_id)
    _seed_recent_campaign(dsn, tenant_id, run_id)

    # Real bundle build — reads campaigns through tenant_connection.
    bundle = build_sales_recovery_context(
        tenant_id,
        run_id,
        "weekly_cadence",
        "Recover dormant customers from the last 60 days",
    )

    # The substrate the seed populated should land as substrate-backed.
    assert bundle.data_completeness["recent_campaigns"] is False or len(
        bundle.recent_campaigns
    ) >= 1, asdict(bundle.data_completeness)
    # (note: recent_campaigns completeness flag is False even on real
    # rows — see _build_recent_campaigns docstring; the row count is
    # the substrate-presence signal here.)
    assert len(bundle.recent_campaigns) == 1

    # Real-API ledger for proof-of-call assertions. Class-level so the
    # assertions can reach it without a reference to the constructed
    # client.
    class _LedgerClient:
        calls_to_real_anthropic: list[dict[str, Any]] = []

        def __init__(self) -> None:
            self._real = _RealAnthropic()

        @property
        def messages(self) -> Any:
            return self

        def create(self, **kwargs: Any) -> Any:
            response = self._real.messages.create(**kwargs)
            msgs = list(kwargs.get("messages", []))
            _LedgerClient.calls_to_real_anthropic.append(
                {
                    "model": kwargs.get("model"),
                    "first_user_message_first_200": (
                        str(msgs[0].get("content"))[:200]
                        if msgs and isinstance(msgs[0], dict)
                        else None
                    ),
                    "response_id": getattr(response, "id", None),
                }
            )
            return response

    _LedgerClient.calls_to_real_anthropic = []

    monkeypatch.setenv("VIABE_ENV", "production")  # → Opus
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", _LedgerClient
    )

    result = run_sales_recovery_agent(bundle, evaluator=None)

    diag = {
        "status": result.status,
        "tokens_used": result.tokens_used,
        "cost_paise": result.cost_paise,
        "real_call_ledger": _LedgerClient.calls_to_real_anthropic,
        "output_keys": (
            sorted(result.output.keys()) if isinstance(result.output, dict) else None
        ),
    }

    # --- PROOF-OF-CALL (CL-272) ---------------------------------------
    assert len(_LedgerClient.calls_to_real_anthropic) >= 1, diag
    first_call = _LedgerClient.calls_to_real_anthropic[0]
    # VT-165: derive the expected model from the canonical resolver (the test
    # already pins VIABE_ENV=production → Opus) rather than hardcoding the id —
    # strictly stronger, and survives a model bump in config/models.yaml.
    assert first_call["model"] == _resolve_model("sales_recovery"), diag
    # Serialized bundle reached the SDK — the rendered block leads with
    # the markdown header.
    assert "Sales Recovery Context" in (
        first_call["first_user_message_first_200"] or ""
    ), diag
    assert isinstance(first_call["response_id"], str), diag
    assert first_call["response_id"].startswith("msg_"), diag
    assert result.tokens_used > 0, diag
    assert result.cost_paise > 0, diag

    # --- Variant + contract conformance -------------------------------
    assert result.status == "completed", diag
    assert result.output is not None, diag
    plan = parse_campaign_plan(result.output)
    assert isinstance(
        plan,
        (
            CampaignPlanProposed,
            CampaignPlanOutOfScope,
            CampaignPlanInsufficientData,
        ),
    ), diag
    # Identity-injection invariant — agent (not the model) sets these.
    assert plan.tenant_id == tenant_id, diag
    assert plan.run_id == run_id, diag
