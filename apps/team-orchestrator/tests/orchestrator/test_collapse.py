"""VT-3.4 PR 3/3 — collapse-path tests (migrated to CampaignPlan v1.0 by VT-122,
variant-correctness fix CL-294).

Live-Postgres tests that exercise ``orchestrator.collapse`` end-to-end
through ``tenant_connection`` (CL-122 / Pillar 3). Mirrors the
test_tenant_isolation.py setup pattern.

Cases:
  Proposed-variant path (``collapse_campaign_plan``):
    1. Happy path: CampaignPlan persisted to ``campaigns`` (plan_json carries
       the v1.0 dict); ``subscriber_states`` activity fields updated.
    2. Phase-unchanged (named): ``tenants.phase`` AND the persisted
       ``subscriber_states.phase`` equal the pre-collapse phase.
    3. Cross-tenant: collapse under tenant A does not touch tenant B's rows
       (subscriber_states or campaigns).
    4. Fail-loud guard (CL-294 Disposition B): calling
       ``collapse_campaign_plan`` directly with a non-proposed plan still
       raises ``RuntimeError`` — the guard is the defence-in-depth check
       behind the variant dispatch in ``collapse_node``.

  Non-proposed terminal-verdict path (``record_terminal_verdict`` via
  ``collapse_node`` dispatch, CL-294):
    5. ``out_of_scope``: ``collapse_node`` completes cleanly; one
       ``pipeline_steps`` row with ``step_kind='campaign_plan_emitted'``
       carries the variant + ``out_of_scope_reason``; no ``campaigns`` row.
    6. ``insufficient_data``: same shape; ``missing_data`` lands in the
       ``output_envelope``; no ``campaigns`` row.

  VT-241 fail-closed cohort wiring (``collapse_node``):
    7. Unresolvable cohort id: ``collapse_node`` returns a
       ``campaign_rejected`` dict (count only) and persists ZERO campaigns +
       ZERO campaign_recipients — the whole transaction rolls back.
    8. Mixed cohort (one real + one bogus id): still fully rejected and
       rolled back — the resolved recipient does NOT leak (atomicity).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("pydantic")

import psycopg  # noqa: E402 — imported after the dependency skip guard

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — collapse tests skipped",
)


@pytest.fixture(scope="module")
def rls_ctx():
    """Apply migrations (incl. 016/017) + launch DBOS so the pool exists."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _new_tenant(
    dsn: str, *, phase: str = "paid_at_risk", whatsapp_number: str | None = None
) -> str:
    """Seed a tenant via a direct superuser connection (RLS bypassed).

    ``whatsapp_number`` is None by default (existing call sites unaffected).
    VT-594 summary-send tests pass a synthetic test number (mock-fixture
    exempt — no real send path; every send in those tests is monkeypatched)
    so ``_resolve_owner_phone_for_summary`` has a recipient to resolve.
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, 'founding', %s, %s) RETURNING id",
            ("collapse-test", phase, whatsapp_number),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _fake_owner_phone() -> str:
    """Synthetic per-call WhatsApp number for the VT-594 summary-send tests.

    ``tenants.whatsapp_number`` has a UNIQUE constraint, so a fixed literal
    collides across tenants seeded in the same test run — each call must be
    unique. Mock-fixture only: every send in these tests is monkeypatched, so
    no real send path ever sees this number.
    """
    return f"+1{uuid4().int % 10**9:09d}"


def _new_run(dsn: str, tenant_id: str) -> str:
    """Seed a pipeline_runs row (FK target for campaigns.run_id)."""
    run_id = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running')",
            (run_id, tenant_id),
        )
    return run_id


def _seed_customer(dsn: str, tenant_id: str) -> str:
    """VT-241: seed a customers row so the cohort resolves (collapse now
    fail-closes on unresolvable cohort ids). Returns the customer id."""
    cid = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customers (id, tenant_id, display_name) "
            "VALUES (%s, %s, 'collapse-test-customer')",
            (cid, tenant_id),
        )
    return cid


def _plan(tenant_id: str, run_id: str, *, customer_id: str | None = None):
    """Build a valid v1.0 ``proposed`` CampaignPlan for collapse tests."""
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanProposed,
        CampaignWindow,
        ConfidenceLevel,
        EvidenceRef,
        EvidenceSourceKind,
        ExpectedARRR,
        Language,
        MessagePlan,
        TargetCohort,
    )

    cid = UUID(customer_id) if customer_id else uuid4()
    now = datetime.now(UTC)
    return CampaignPlanProposed(
        tenant_id=UUID(tenant_id),
        run_id=UUID(run_id),
        generated_at=now,
        campaign_window=CampaignWindow(
            start=now + timedelta(hours=1),
            end=now + timedelta(days=7),
        ),
        target_cohort=TargetCohort(
            customer_ids=[cid],
            cohort_label="60-90 day dormants",
            cohort_size=1,
            selection_reason="dormant cohort [E1].",
        ),
        expected_arrr=ExpectedARRR(
            low_paise=10_000_00,
            high_paise=30_000_00,
            confidence=ConfidenceLevel.MEDIUM,
            basis="prior winback yields [E1].",
        ),
        evidence_refs=[
            EvidenceRef(
                claim_id="E1",
                source_kind=EvidenceSourceKind.TOOL_CALL,
                source_id="test-evidence",
            ),
        ],
        message_plan=MessagePlan(
            template_id="team_winback_v1",
            template_params={"first_name": "Owner", "discount": "10"},
            language=Language.EN,
            personalization="owner-first-name.",
        ),
    )


def test_collapse_persists_campaign_and_updates_subscriber_state(rls_ctx):
    from orchestrator.collapse import collapse_campaign_plan
    from orchestrator.db import tenant_connection

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)
    plan = _plan(tenant, run_id, customer_id=_seed_customer(rls_ctx.dsn, tenant))

    campaign_id = collapse_campaign_plan(
        tenant_id=UUID(tenant), run_id=UUID(run_id), campaign_plan=plan
    )

    with tenant_connection(tenant) as conn:
        campaign_row = conn.execute(
            "SELECT plan_json, status, generated_at FROM campaigns WHERE id = %s",
            (str(campaign_id),),
        ).fetchone()
        sub_row = conn.execute(
            "SELECT last_campaign_at, attribution_close_pending "
            "FROM subscriber_states WHERE tenant_id = %s",
            (tenant,),
        ).fetchone()

    assert campaign_row is not None
    # status starts at the lifecycle-initial value 'proposed' (which
    # shares the name with the agent-terminal state — see CL: status-
    # enum split). Downstream VT-6 / VT-5 flip to approved/rejected /
    # sent/failed.
    assert campaign_row["status"] == "proposed"
    assert campaign_row["generated_at"] is not None
    # plan_json carries the full v1.0 dict. Spot-check the key nested
    # fields exposed by the migration's contract.
    plan_json = campaign_row["plan_json"]
    assert plan_json["version"] == "1.0"
    assert plan_json["status"] == "proposed"
    assert plan_json["message_plan"]["template_id"] == "team_winback_v1"
    assert plan_json["message_plan"]["template_params"] == {
        "first_name": "Owner",
        "discount": "10",
    }
    assert plan_json["target_cohort"]["cohort_size"] == 1
    assert len(plan_json["evidence_refs"]) >= 1

    assert sub_row is not None
    assert sub_row["last_campaign_at"] is not None
    assert sub_row["attribution_close_pending"] == [campaign_id]


def test_collapse_does_not_change_phase(rls_ctx):
    """Required (CL-233): collapse path is activity-only; phase is untouched."""
    from orchestrator.collapse import collapse_campaign_plan
    from orchestrator.db import tenant_connection

    starting_phase = "paid_at_risk"
    tenant = _new_tenant(rls_ctx.dsn, phase=starting_phase)
    run_id = _new_run(rls_ctx.dsn, tenant)

    with tenant_connection(tenant) as conn:
        phase_before = conn.execute(
            "SELECT phase FROM tenants WHERE id = %s", (tenant,)
        ).fetchone()["phase"]

    collapse_campaign_plan(
        tenant_id=UUID(tenant),
        run_id=UUID(run_id),
        campaign_plan=_plan(tenant, run_id, customer_id=_seed_customer(rls_ctx.dsn, tenant)),
    )

    with tenant_connection(tenant) as conn:
        tenants_phase_after = conn.execute(
            "SELECT phase FROM tenants WHERE id = %s", (tenant,)
        ).fetchone()["phase"]
        sub_phase = conn.execute(
            "SELECT phase FROM subscriber_states WHERE tenant_id = %s", (tenant,)
        ).fetchone()["phase"]

    assert phase_before == starting_phase
    assert tenants_phase_after == starting_phase, "collapse must not mutate tenants.phase"
    assert sub_phase == starting_phase, "subscriber_states.phase must mirror tenants.phase"


def _plan_out_of_scope(tenant_id: str, run_id: str):
    """Build a valid v1.0 ``out_of_scope`` CampaignPlan (CL-294)."""
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanOutOfScope,
        SuggestedSpecialist,
    )

    return CampaignPlanOutOfScope(
        tenant_id=UUID(tenant_id),
        run_id=UUID(run_id),
        generated_at=datetime.now(UTC),
        out_of_scope_reason=(
            "Request concerns review-reputation handling; reputation "
            "specialist owns that domain, not sales recovery."
        ),
        suggested_specialist=SuggestedSpecialist.REPUTATION,
    )


def _plan_insufficient_data(tenant_id: str, run_id: str):
    """Build a valid v1.0 ``insufficient_data`` CampaignPlan (CL-294)."""
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanInsufficientData,
        MissingDataItem,
    )

    return CampaignPlanInsufficientData(
        tenant_id=UUID(tenant_id),
        run_id=UUID(run_id),
        generated_at=datetime.now(UTC),
        missing_data=[
            MissingDataItem(
                category="cohort",
                description="No dormant-customer rows surfaced for tenant.",
                suggested_remediation="Seed customer ledger or supply cohort.",
            )
        ],
    )


def test_collapse_campaign_plan_raises_on_non_proposed_guard(rls_ctx):
    """CL-294 Disposition B: ``collapse_campaign_plan`` keeps its fail-loud
    guard against non-proposed plans. ``collapse_node`` dispatches non-
    proposed variants AWAY from this function, but if a future consumer
    calls it directly without gating on variant, the guard must still
    raise (defence in depth)."""
    from orchestrator.collapse import collapse_campaign_plan

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)

    with pytest.raises(RuntimeError, match="only handles the proposed variant"):
        collapse_campaign_plan(
            tenant_id=UUID(tenant),
            run_id=UUID(run_id),
            campaign_plan=_plan_insufficient_data(tenant, run_id),
        )
    with pytest.raises(RuntimeError, match="only handles the proposed variant"):
        collapse_campaign_plan(
            tenant_id=UUID(tenant),
            run_id=UUID(run_id),
            campaign_plan=_plan_out_of_scope(tenant, run_id),
        )


def test_collapse_node_out_of_scope_records_verdict_no_campaign(rls_ctx):
    """CL-294: ``out_of_scope`` terminal verdict — ``collapse_node`` completes
    cleanly, writes one ``pipeline_steps`` row with
    ``step_kind='campaign_plan_emitted'`` carrying the variant +
    ``out_of_scope_reason``, and creates NO ``campaigns`` row."""
    from orchestrator.collapse import collapse_node
    from orchestrator.db import tenant_connection

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)
    plan = _plan_out_of_scope(tenant, run_id)

    update = collapse_node(
        {
            "tenant_id": UUID(tenant),
            "run_id": UUID(run_id),
            "campaign_plan": plan,
        }
    )
    assert update == {}

    with tenant_connection(tenant) as conn:
        n_campaigns = conn.execute(
            "SELECT count(*) AS n FROM campaigns WHERE run_id = %s",
            (run_id,),
        ).fetchone()["n"]
        step_rows = conn.execute(
            "SELECT step_kind, output_envelope, decision_rationale "
            "FROM pipeline_steps WHERE run_id = %s "
            "AND step_kind = 'campaign_plan_emitted'",
            (run_id,),
        ).fetchall()

    assert n_campaigns == 0, (
        "out_of_scope verdict must NOT create a campaigns row"
    )
    assert len(step_rows) == 1, "exactly one terminal-verdict step expected"
    envelope = step_rows[0]["output_envelope"]
    assert envelope["variant"] == "out_of_scope"
    assert envelope["version"] == "1.0"
    assert envelope["out_of_scope_reason"].startswith(
        "Request concerns review-reputation"
    )
    assert envelope["suggested_specialist"] == "reputation"
    assert step_rows[0]["decision_rationale"] == "agent terminal verdict: out_of_scope"


def test_collapse_node_insufficient_data_records_verdict_no_campaign(rls_ctx):
    """CL-294: ``insufficient_data`` terminal verdict — ``collapse_node``
    completes cleanly, writes one ``pipeline_steps`` row carrying the
    variant + ``missing_data`` list, and creates NO ``campaigns`` row."""
    from orchestrator.collapse import collapse_node
    from orchestrator.db import tenant_connection

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)
    plan = _plan_insufficient_data(tenant, run_id)

    update = collapse_node(
        {
            "tenant_id": UUID(tenant),
            "run_id": UUID(run_id),
            "campaign_plan": plan,
        }
    )
    assert update == {}

    with tenant_connection(tenant) as conn:
        n_campaigns = conn.execute(
            "SELECT count(*) AS n FROM campaigns WHERE run_id = %s",
            (run_id,),
        ).fetchone()["n"]
        step_rows = conn.execute(
            "SELECT step_kind, output_envelope, decision_rationale "
            "FROM pipeline_steps WHERE run_id = %s "
            "AND step_kind = 'campaign_plan_emitted'",
            (run_id,),
        ).fetchall()

    assert n_campaigns == 0, (
        "insufficient_data verdict must NOT create a campaigns row"
    )
    assert len(step_rows) == 1
    envelope = step_rows[0]["output_envelope"]
    assert envelope["variant"] == "insufficient_data"
    assert envelope["version"] == "1.0"
    assert isinstance(envelope["missing_data"], list)
    assert len(envelope["missing_data"]) == 1
    assert envelope["missing_data"][0]["category"] == "cohort"
    assert envelope["missing_data"][0]["description"].startswith(
        "No dormant-customer rows"
    )
    assert (
        step_rows[0]["decision_rationale"]
        == "agent terminal verdict: insufficient_data"
    )


def test_collapse_node_proposed_still_persists_campaign(rls_ctx):
    """CL-294: ``collapse_node``'s variant dispatch routes ``proposed`` plans
    to ``collapse_campaign_plan`` unchanged. End-to-end via the node — the
    direct-call happy-path test already covers the function; this test
    pins the DISPATCH for the proposed branch.

    VT-47: a PERSISTED proposed campaign is a Pillar-7 sensitive action, so
    collapse_node now ALSO attaches a ``pending_approval_request`` to state
    (route_after_collapse keys on it to send the run to the owner-approval
    gate). The campaign row is still written exactly once."""
    from orchestrator.collapse import collapse_node
    from orchestrator.db import tenant_connection

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)
    plan = _plan(tenant, run_id, customer_id=_seed_customer(rls_ctx.dsn, tenant))

    update = collapse_node(
        {
            "tenant_id": UUID(tenant),
            "run_id": UUID(run_id),
            "campaign_plan": plan,
        }
    )
    # VT-47: proposed-success attaches the approval request (campaign_send).
    assert "pending_approval_request" in update
    req = update["pending_approval_request"]
    assert req["approval_type"] == "campaign_send"
    assert req["campaign_id"] is not None
    assert req["details"]["cohort_size"] == 1

    with tenant_connection(tenant) as conn:
        n_campaigns = conn.execute(
            "SELECT count(*) AS n FROM campaigns WHERE run_id = %s",
            (run_id,),
        ).fetchone()["n"]
        # No terminal-verdict step should be written on the proposed
        # path — that surface is for non-proposed only.
        n_terminal_steps = conn.execute(
            "SELECT count(*) AS n FROM pipeline_steps WHERE run_id = %s "
            "AND step_kind = 'campaign_plan_emitted'",
            (run_id,),
        ).fetchone()["n"]

    assert n_campaigns == 1, "proposed must create exactly one campaigns row"
    assert n_terminal_steps == 0, (
        "proposed must NOT write a campaign_plan_emitted step"
    )


def test_collapse_does_not_cross_tenant_boundary(rls_ctx):
    """Tenant A's collapse must not touch tenant B's campaigns / subscriber_states."""
    from orchestrator.collapse import collapse_campaign_plan
    from orchestrator.db import tenant_connection

    tenant_a = _new_tenant(rls_ctx.dsn)
    tenant_b = _new_tenant(rls_ctx.dsn)
    run_a = _new_run(rls_ctx.dsn, tenant_a)

    collapse_campaign_plan(
        tenant_id=UUID(tenant_a),
        run_id=UUID(run_a),
        campaign_plan=_plan(tenant_a, run_a, customer_id=_seed_customer(rls_ctx.dsn, tenant_a)),
    )

    with tenant_connection(tenant_b) as conn:
        b_campaigns = conn.execute("SELECT count(*) AS n FROM campaigns").fetchone()["n"]
        b_sub = conn.execute(
            "SELECT count(*) AS n FROM subscriber_states"
        ).fetchone()["n"]

    assert b_campaigns == 0, "tenant B must see no campaigns rows from tenant A's collapse"
    assert b_sub == 0, "tenant B must see no subscriber_states rows from tenant A's collapse"


def test_collapse_node_fails_closed_on_unresolvable_cohort(rls_ctx):
    """VT-241: a cohort whose customer_id is not a real same-tenant customer
    is REJECTED fail-closed — the node returns a count-only ``campaign_rejected``
    dict and NOTHING is persisted (campaign INSERT + recipient INSERTs roll
    back atomically). The owner surface gets a count, never the rejected ids."""
    from orchestrator.collapse import collapse_node
    from orchestrator.db import tenant_connection

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)
    # No _seed_customer — the cohort id is a random, non-existent uuid.
    plan = _plan(tenant, run_id)

    update = collapse_node(
        {
            "tenant_id": UUID(tenant),
            "run_id": UUID(run_id),
            "campaign_plan": plan,
        }
    )

    assert update == {
        "campaign_rejected": {"reason": "unresolved_cohort", "rejected_count": 1}
    }, "unresolvable cohort must return a count-only rejection (no ids leaked)"

    with tenant_connection(tenant) as conn:
        n_campaigns = conn.execute(
            "SELECT count(*) AS n FROM campaigns WHERE run_id = %s", (run_id,)
        ).fetchone()["n"]
        n_recipients = conn.execute(
            "SELECT count(*) AS n FROM campaign_recipients"
        ).fetchone()["n"]
        n_subs = conn.execute(
            "SELECT count(*) AS n FROM subscriber_states WHERE tenant_id = %s",
            (tenant,),
        ).fetchone()["n"]

    assert n_campaigns == 0, "fail-closed: NO campaigns row may persist on reject"
    assert n_recipients == 0, "fail-closed: NO campaign_recipients may persist on reject"
    assert n_subs == 0, "fail-closed: subscriber_states activity must not advance on reject"


def test_collapse_node_fails_closed_atomic_on_mixed_cohort(rls_ctx):
    """VT-241 atomicity: a cohort with one REAL + one bogus id is still fully
    rejected. The real recipient must NOT leak into campaign_recipients — the
    whole transaction unwinds, all-or-nothing. Proves the resolve runs inside
    collapse's transaction (cur-injected), not a separate committed one."""
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanProposed,
        CampaignWindow,
        ConfidenceLevel,
        EvidenceRef,
        EvidenceSourceKind,
        ExpectedARRR,
        Language,
        MessagePlan,
        TargetCohort,
    )
    from orchestrator.collapse import collapse_node
    from orchestrator.db import tenant_connection

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)
    real_id = _seed_customer(rls_ctx.dsn, tenant)
    bogus_id = str(uuid4())

    now = datetime.now(UTC)
    plan = CampaignPlanProposed(
        tenant_id=UUID(tenant),
        run_id=UUID(run_id),
        generated_at=now,
        campaign_window=CampaignWindow(
            start=now + timedelta(hours=1), end=now + timedelta(days=7)
        ),
        target_cohort=TargetCohort(
            customer_ids=[UUID(real_id), UUID(bogus_id)],
            cohort_label="mixed",
            cohort_size=2,
            selection_reason="mixed cohort [E1].",
        ),
        expected_arrr=ExpectedARRR(
            low_paise=10_000_00,
            high_paise=30_000_00,
            confidence=ConfidenceLevel.MEDIUM,
            basis="prior yields [E1].",
        ),
        evidence_refs=[
            EvidenceRef(
                claim_id="E1",
                source_kind=EvidenceSourceKind.TOOL_CALL,
                source_id="test-evidence",
            )
        ],
        message_plan=MessagePlan(
            template_id="team_winback_v1",
            template_params={"first_name": "Owner", "discount": "10"},
            language=Language.EN,
            personalization="owner-first-name.",
        ),
    )

    update = collapse_node(
        {
            "tenant_id": UUID(tenant),
            "run_id": UUID(run_id),
            "campaign_plan": plan,
        }
    )

    assert update == {
        "campaign_rejected": {"reason": "unresolved_cohort", "rejected_count": 1}
    }, "one bogus id rejects the whole campaign (count = 1 unresolved)"

    with tenant_connection(tenant) as conn:
        n_campaigns = conn.execute(
            "SELECT count(*) AS n FROM campaigns WHERE run_id = %s", (run_id,)
        ).fetchone()["n"]
        n_recipients = conn.execute(
            "SELECT count(*) AS n FROM campaign_recipients"
        ).fetchone()["n"]

    assert n_campaigns == 0, "mixed-cohort reject: NO campaigns row may persist"
    assert n_recipients == 0, (
        "atomicity: the RESOLVED recipient must roll back too — no partial leak"
    )


# ---------------------------------------------------------------------------
# VT-594 change C — the in-chat plan SUMMARY before the approval prompt.
#
# A PERSISTED proposed campaign previously told the owner NOTHING until the
# separate team_weekly_approval template arrived (a blind approval ask). When
# the owner explicitly asked for the plan (trigger_reason == 'owner_initiated'),
# collapse_node now best-effort sends a deterministic summary BEFORE the gate
# routes to request_owner_approval — cohort size, segment label, window,
# expected recovery range, one-line selection reason. A weekly-cadence
# proposal (no owner ask) keeps today's prompt-only behavior — no new
# unsolicited sends. The send is fully try/except-wrapped: a failure must
# never unwind the persist or block the gate.
# ---------------------------------------------------------------------------


def _seed_recent_campaign_approval(dsn: str, tenant_id: str, run_id: str) -> None:
    """Seed one RESOLVED pending_approvals row within the VT-334 7-day budget
    window, so the budget counter (which counts regardless of resolved state)
    can be tripped without leaving an OPEN row (which would trigger a
    DIFFERENT guard — the per-tenant queue-busy refusal in arm_pause_request,
    not exercised by this test)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO pending_approvals
                (tenant_id, run_id, approval_type, summary, status, decision,
                 timeout_at, resolved_at)
            VALUES (%s, %s, 'campaign_send', 'prior approval', 'approved',
                    'approved', now() + interval '1 hour', now())
            """,
            (tenant_id, run_id),
        )


def test_collapse_node_owner_initiated_sends_plan_summary_before_approval_request(
    rls_ctx, monkeypatch
):
    """trigger_reason == 'owner_initiated' -> the summary send fires BEFORE the
    approval-request payload is built, and the normal proposed-success return
    (pending_approval_request attached) is unaffected."""
    import orchestrator.collapse as collapse_mod

    tenant = _new_tenant(rls_ctx.dsn, whatsapp_number=_fake_owner_phone())
    run_id = _new_run(rls_ctx.dsn, tenant)
    plan = _plan(tenant, run_id, customer_id=_seed_customer(rls_ctx.dsn, tenant))

    order: list[str] = []
    real_send = collapse_mod._maybe_send_plan_summary
    real_build = collapse_mod._build_approval_request

    def _spy_send(tenant_id, plan_arg):
        order.append("summary")
        return real_send(tenant_id, plan_arg)

    def _spy_build(*, plan, campaign_id):
        order.append("approval_request_built")
        return real_build(plan=plan, campaign_id=campaign_id)

    sent: list[str] = []
    monkeypatch.setattr(collapse_mod, "_maybe_send_plan_summary", _spy_send)
    monkeypatch.setattr(collapse_mod, "_build_approval_request", _spy_build)
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale",
        lambda tenant_id: "en",
    )
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack",
        lambda tenant_id, recipient, body: sent.append(body) or True,
    )

    update = collapse_mod.collapse_node(
        {
            "tenant_id": UUID(tenant),
            "run_id": UUID(run_id),
            "campaign_plan": plan,
            "trigger_reason": "owner_initiated",
        }
    )

    assert order == ["summary", "approval_request_built"], (
        "the plan summary must send BEFORE the approval-request payload is built"
    )
    assert len(sent) == 1
    assert str(plan.target_cohort.cohort_size) in sent[0]
    assert "pending_approval_request" in update


def test_collapse_node_weekly_cadence_trigger_reason_sends_no_summary(
    rls_ctx, monkeypatch
):
    """A weekly-cadence proposal (no explicit owner ask) keeps today's
    prompt-only behavior — no new unsolicited summary send."""
    import orchestrator.collapse as collapse_mod

    tenant = _new_tenant(rls_ctx.dsn, whatsapp_number=_fake_owner_phone())
    run_id = _new_run(rls_ctx.dsn, tenant)
    plan = _plan(tenant, run_id, customer_id=_seed_customer(rls_ctx.dsn, tenant))

    called = {"n": 0}
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True,
    )

    update = collapse_mod.collapse_node(
        {
            "tenant_id": UUID(tenant),
            "run_id": UUID(run_id),
            "campaign_plan": plan,
            "trigger_reason": "weekly_cadence",
        }
    )

    assert called["n"] == 0, "no summary send on a non-owner-initiated trigger"
    assert "pending_approval_request" in update


def test_collapse_node_summary_send_raise_does_not_block_approval_request(
    rls_ctx, monkeypatch
):
    """A summary-send failure must never unwind the persist or block the gate
    — collapse_node still returns pending_approval_request normally."""
    import orchestrator.collapse as collapse_mod
    from orchestrator.db import tenant_connection

    tenant = _new_tenant(rls_ctx.dsn, whatsapp_number=_fake_owner_phone())
    run_id = _new_run(rls_ctx.dsn, tenant)
    plan = _plan(tenant, run_id, customer_id=_seed_customer(rls_ctx.dsn, tenant))

    def _boom(*a, **k):
        raise RuntimeError("send exploded")

    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale",
        lambda tenant_id: "en",
    )
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack", _boom
    )

    update = collapse_mod.collapse_node(
        {
            "tenant_id": UUID(tenant),
            "run_id": UUID(run_id),
            "campaign_plan": plan,
            "trigger_reason": "owner_initiated",
        }
    )

    assert "pending_approval_request" in update
    with tenant_connection(tenant) as conn:
        n_campaigns = conn.execute(
            "SELECT count(*) AS n FROM campaigns WHERE run_id = %s", (run_id,)
        ).fetchone()["n"]
    assert n_campaigns == 1, "the persist must survive a summary-send failure"


def test_collapse_node_budget_skip_path_unchanged_returns_empty_dict(
    rls_ctx, monkeypatch
):
    """VT-334 budget-skip path is UNCHANGED by the VT-594 summary addition: the
    campaign still persists, the return still degrades to {} (no approval
    prompt this week) — the summary (gated only on trigger_reason) still fires
    since it's a SEPARATE concern from the approval-ask cadence guard."""
    import orchestrator.collapse as collapse_mod

    tenant = _new_tenant(rls_ctx.dsn, whatsapp_number=_fake_owner_phone())
    run_id = _new_run(rls_ctx.dsn, tenant)
    plan = _plan(tenant, run_id, customer_id=_seed_customer(rls_ctx.dsn, tenant))

    # Trip the VT-334 weekly budget (>= 2 recent campaign_send requests).
    _seed_recent_campaign_approval(rls_ctx.dsn, tenant, _new_run(rls_ctx.dsn, tenant))
    _seed_recent_campaign_approval(rls_ctx.dsn, tenant, _new_run(rls_ctx.dsn, tenant))

    sent: list[str] = []
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale",
        lambda tenant_id: "en",
    )
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack",
        lambda tenant_id, recipient, body: sent.append(body) or True,
    )

    update = collapse_mod.collapse_node(
        {
            "tenant_id": UUID(tenant),
            "run_id": UUID(run_id),
            "campaign_plan": plan,
            "trigger_reason": "owner_initiated",
        }
    )

    assert update == {}, "budget-skip return contract is unchanged"
    assert len(sent) == 1, "the plan summary still sends — gated on trigger_reason only"
