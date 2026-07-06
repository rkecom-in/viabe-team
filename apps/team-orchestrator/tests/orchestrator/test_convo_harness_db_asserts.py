"""VT-611 Package H1 — real-DB behavior of convo_harness.py's DB-state asserts.

test_convo_harness_helpers.py covers the pure/mocked dispatch logic (_evaluate_db_asserts); THIS
file proves the actual SQL against a live Postgres: assert_route / assert_side_effects /
assert_grounded_count / assert_no_unapproved_effect, plus _observed_route / _campaign_id_for_run.

Live Postgres via DATABASE_URL (CI orchestrator job) — no DBOS needed (these functions do plain
reads over tenants/pipeline_runs/campaigns/pending_approvals/campaign_messages, not the graph).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

import psycopg  # noqa: E402 — after the dependency skip guard
from psycopg.types.json import Jsonb  # noqa: E402

# canaries/ is NOT on the pytest pythonpath (only src/scripts) — add it so we can import the harness.
_CANARIES = Path(__file__).resolve().parents[2] / "canaries"
sys.path.insert(0, str(_CANARIES))

import convo_harness as ch  # noqa: E402 — after the sys.path insert

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — convo_harness DB-assert tests skipped",
)


@pytest.fixture(scope="module")
def dsn():
    import apply_migrations

    url = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=url)
    assert not r["failed"], r["failed"]
    return url


def _new_tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at) "
            "VALUES ('convo-harness-h1-test', 'founding', 'trial', now()) RETURNING id"
        ).fetchone()
    assert row is not None
    return str(row[0])


def _new_run(dsn: str, tenant_id: str) -> str:
    """A pipeline_runs row with an EXPLICIT id (mirrors run_id_for_sid's deterministic derivation —
    the test picks its own run_id up front rather than deriving it from a fake sid)."""
    run_id = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status) VALUES (%s, %s, 'completed')",
            (run_id, tenant_id),
        )
    return run_id


def _new_campaign(
    dsn: str, tenant_id: str, run_id: str, *, cohort_size: int = 8, status: str = "proposed"
) -> str:
    plan_json = {
        "target_cohort": {"cohort_size": cohort_size, "customer_ids": [str(uuid4()) for _ in range(cohort_size)]},
    }
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO campaigns (tenant_id, run_id, plan_json, status, generated_at) "
            "VALUES (%s, %s, %s, %s, now()) RETURNING id",
            (tenant_id, run_id, Jsonb(plan_json), status),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _new_pending_approval(
    dsn: str, tenant_id: str, run_id: str, campaign_id: str, *, decision: str | None
) -> None:
    status = "pending" if decision is None else ("approved" if decision == "approved" else "rejected")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pending_approvals (tenant_id, run_id, campaign_id, approval_type, "
            "summary, status, decision, timeout_at) "
            "VALUES (%s, %s, %s, 'campaign_send', 'approve the campaign?', %s, %s, "
            "now() + interval '1 hour')",
            (tenant_id, run_id, campaign_id, status, decision),
        )


def _new_campaign_message(
    dsn: str, tenant_id: str, campaign_id: str, customer_id: str, *, send_status: str = "sent"
) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO campaign_messages (tenant_id, customer_id, idempotency_key, send_status) "
            "VALUES (%s, %s, %s, %s)",
            (tenant_id, customer_id, f"{campaign_id}:{customer_id}", send_status),
        )


# --- _campaign_id_for_run / _observed_route -----------------------------------------------------


def test_campaign_id_for_run_none_when_no_campaign(dsn):
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch._campaign_id_for_run(conn, tenant, run_id) is None
        assert ch._observed_route(conn, tenant, run_id) == "none"


def test_campaign_id_for_run_found_and_route_is_sales_recovery(dsn):
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    campaign_id = _new_campaign(dsn, tenant, run_id)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch._campaign_id_for_run(conn, tenant, run_id) == campaign_id
        assert ch._observed_route(conn, tenant, run_id) == "sales_recovery"


def test_campaign_id_for_run_scoped_to_the_specific_run_not_tenant_wide(dsn):
    """A DIFFERENT run_id for the SAME tenant must not see another run's campaign — the whole point
    of scoping by run_id (not just tenant_id) in a multi-step scenario."""
    tenant = _new_tenant(dsn)
    run_a = _new_run(dsn, tenant)
    run_b = _new_run(dsn, tenant)
    _new_campaign(dsn, tenant, run_a)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch._campaign_id_for_run(conn, tenant, run_b) is None
        assert ch._observed_route(conn, tenant, run_b) == "none"


# --- assert_route --------------------------------------------------------------------------------


def test_assert_route_passes_when_expectation_matches_no_delegation(dsn):
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch.assert_route(conn, tenant, run_id, expect_sr_delegation=False) == []


def test_assert_route_fails_when_expected_delegation_did_not_happen(dsn):
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    with psycopg.connect(dsn, autocommit=True) as conn:
        failures = ch.assert_route(conn, tenant, run_id, expect_sr_delegation=True)
    assert failures and "assert_route" in failures[0]


def test_assert_route_fails_when_unexpected_delegation_happened(dsn):
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    _new_campaign(dsn, tenant, run_id)
    with psycopg.connect(dsn, autocommit=True) as conn:
        failures = ch.assert_route(conn, tenant, run_id, expect_sr_delegation=False)
    assert failures and "assert_route" in failures[0]


def test_assert_route_passes_when_expected_delegation_happened(dsn):
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    _new_campaign(dsn, tenant, run_id)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch.assert_route(conn, tenant, run_id, expect_sr_delegation=True) == []


# --- tenant-wide scoping (run_id=None) — the multi-turn draft-then-approve case --------------------


def test_campaign_id_for_run_none_scope_finds_the_tenants_most_recent_campaign(dsn):
    """The exact gap this closes: the APPROVAL turn (turn N+1) has its OWN fresh run_id (a new
    inbound message gets its own pipeline_runs row even when it resumes an earlier suspended
    graph) — campaigns.run_id stays turn N's forever. Scoping by tenant (run_id=None) is how a
    later turn's assert finds turn N's campaign."""
    tenant = _new_tenant(dsn)
    draft_turn_run_id = _new_run(dsn, tenant)
    campaign_id = _new_campaign(dsn, tenant, draft_turn_run_id)
    approval_turn_run_id = _new_run(dsn, tenant)  # a DIFFERENT run_id for the later reply

    with psycopg.connect(dsn, autocommit=True) as conn:
        # scoped to the APPROVAL turn's own run_id -> nothing (campaigns.run_id is the draft turn's).
        assert ch._campaign_id_for_run(conn, tenant, approval_turn_run_id) is None
        # tenant-wide -> finds it.
        assert ch._campaign_id_for_run(conn, tenant, None) == campaign_id


def test_assert_side_effects_tenant_wide_checks_the_approved_send_from_an_earlier_turn(dsn):
    tenant = _new_tenant(dsn)
    draft_run = _new_run(dsn, tenant)
    campaign_id = _new_campaign(dsn, tenant, draft_run, cohort_size=2)
    _new_pending_approval(dsn, tenant, draft_run, campaign_id, decision="approved")
    _new_campaign_message(dsn, tenant, campaign_id, str(uuid4()), send_status="sent")
    _new_campaign_message(dsn, tenant, campaign_id, str(uuid4()), send_status="sent")
    approval_run = _new_run(dsn, tenant)  # the LATER turn that triggered the send

    with psycopg.connect(dsn, autocommit=True) as conn:
        # scoped to the approval turn's OWN run_id -> can't see the draft-turn campaign at all.
        assert ch.assert_side_effects(conn, tenant, approval_run, expect_campaign=True) != []
        # tenant-wide -> the real check the scenario actually wants.
        assert ch.assert_side_effects(
            conn, tenant, None, expect_approval_decision="approved", expect_sent_count=2,
        ) == []


# --- assert_grounded_count ------------------------------------------------------------------------


def test_assert_grounded_count_passes_on_match(dsn):
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    _new_campaign(dsn, tenant, run_id, cohort_size=8)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch.assert_grounded_count(conn, tenant, run_id, expected_count=8) == []


def test_assert_grounded_count_fails_on_fabricated_number(dsn):
    """The load-bearing case: the manager's OWN persisted plan says 8, but the scenario expected
    (seeded) 8 — a mismatch here means the reply likely fabricated a different number than what was
    actually planned/seeded."""
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    _new_campaign(dsn, tenant, run_id, cohort_size=8)
    with psycopg.connect(dsn, autocommit=True) as conn:
        failures = ch.assert_grounded_count(conn, tenant, run_id, expected_count=40)
    assert failures and "cohort_size" in failures[0]


def test_assert_grounded_count_fails_when_no_campaign_row(dsn):
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    with psycopg.connect(dsn, autocommit=True) as conn:
        failures = ch.assert_grounded_count(conn, tenant, run_id, expected_count=8)
    assert failures and "no campaigns row" in failures[0]


# --- assert_side_effects --------------------------------------------------------------------------


def test_assert_side_effects_expect_campaign_true_false(dsn):
    tenant = _new_tenant(dsn)
    run_with = _new_run(dsn, tenant)
    _new_campaign(dsn, tenant, run_with)
    run_without = _new_run(dsn, tenant)

    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch.assert_side_effects(conn, tenant, run_with, expect_campaign=True) == []
        assert ch.assert_side_effects(conn, tenant, run_without, expect_campaign=False) == []
        assert ch.assert_side_effects(conn, tenant, run_without, expect_campaign=True) != []
        assert ch.assert_side_effects(conn, tenant, run_with, expect_campaign=False) != []


def test_assert_side_effects_expect_approval_decision_pending_and_approved(dsn):
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    campaign_id = _new_campaign(dsn, tenant, run_id)
    _new_pending_approval(dsn, tenant, run_id, campaign_id, decision=None)

    with psycopg.connect(dsn, autocommit=True) as conn:
        # still NULL -> the "pending" sentinel matches; 'approved' does not.
        assert ch.assert_side_effects(conn, tenant, run_id, expect_approval_decision="pending") == []
        assert ch.assert_side_effects(conn, tenant, run_id, expect_approval_decision="approved") != []

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE pending_approvals SET decision = 'approved', status = 'approved' "
            "WHERE tenant_id = %s AND campaign_id = %s",
            (tenant, campaign_id),
        )
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch.assert_side_effects(conn, tenant, run_id, expect_approval_decision="approved") == []
        assert ch.assert_side_effects(conn, tenant, run_id, expect_approval_decision="pending") != []


def test_assert_side_effects_expect_approval_decision_with_no_campaign_is_a_failure(dsn):
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    with psycopg.connect(dsn, autocommit=True) as conn:
        failures = ch.assert_side_effects(conn, tenant, run_id, expect_approval_decision="approved")
    assert failures and "no campaigns row" in failures[0]


def test_assert_side_effects_expect_sent_count_zero_hold_off_scenario(dsn):
    """Sc #2's exact shape: a campaign was proposed but the owner said 'hold off' — zero sends."""
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    _new_campaign(dsn, tenant, run_id)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch.assert_side_effects(conn, tenant, run_id, expect_sent_count=0) == []


def test_assert_side_effects_expect_sent_count_matches_after_approved_send(dsn):
    """Sc #3's exact shape: 'haan bhej do' -> approved -> N campaign_messages land as 'sent',
    correlated via the idempotency_key campaign_id prefix (campaign_messages.campaign_id itself is
    never populated by the real send code — see the module-level Package H1 note)."""
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    campaign_id = _new_campaign(dsn, tenant, run_id, cohort_size=2)
    _new_pending_approval(dsn, tenant, run_id, campaign_id, decision="approved")
    _new_campaign_message(dsn, tenant, campaign_id, str(uuid4()), send_status="sent")
    _new_campaign_message(dsn, tenant, campaign_id, str(uuid4()), send_status="sent")
    # an unrelated ERROR row for the same campaign must NOT count toward expect_sent_count.
    _new_campaign_message(dsn, tenant, campaign_id, str(uuid4()), send_status="error")

    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch.assert_side_effects(conn, tenant, run_id, expect_sent_count=2) == []
        assert ch.assert_side_effects(conn, tenant, run_id, expect_sent_count=0) != []


def test_assert_side_effects_expect_sent_count_at_least_is_a_floor_not_an_exact_match(dsn):
    """The activation gate's own percentile floors may not clear the FULL seeded cohort (see
    delegation_winback_plan.json's notes on this same non-determinism) — ">0 actually sent" is the
    honest, robust claim for an approved-send scenario, not a brittle exact count."""
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    campaign_id = _new_campaign(dsn, tenant, run_id, cohort_size=4)
    _new_campaign_message(dsn, tenant, campaign_id, str(uuid4()), send_status="sent")

    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch.assert_side_effects(conn, tenant, run_id, expect_sent_count_at_least=1) == []
        failures = ch.assert_side_effects(conn, tenant, run_id, expect_sent_count_at_least=2)
    assert failures and ">= 2" in failures[0]


# --- assert_no_unapproved_effect (the safety-net default) -----------------------------------------


def test_assert_no_unapproved_effect_passes_when_nothing_sent(dsn):
    tenant = _new_tenant(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch.assert_no_unapproved_effect(conn, tenant) == []


def test_assert_no_unapproved_effect_passes_when_sent_send_is_approved(dsn):
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    campaign_id = _new_campaign(dsn, tenant, run_id)
    _new_pending_approval(dsn, tenant, run_id, campaign_id, decision="approved")
    _new_campaign_message(dsn, tenant, campaign_id, str(uuid4()), send_status="sent")
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch.assert_no_unapproved_effect(conn, tenant) == []


def test_assert_no_unapproved_effect_fails_on_a_sent_row_with_no_approval_at_all(dsn):
    """The direct DB proof B3 exists to close: a real (mocked-transport) send happened with NO
    pending_approvals row backing it at all — must be caught, not silently pass."""
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    campaign_id = _new_campaign(dsn, tenant, run_id)
    _new_campaign_message(dsn, tenant, campaign_id, str(uuid4()), send_status="sent")
    with psycopg.connect(dsn, autocommit=True) as conn:
        failures = ch.assert_no_unapproved_effect(conn, tenant)
    assert failures and "unapproved" in failures[0]


def test_assert_no_unapproved_effect_fails_on_a_sent_row_whose_approval_was_rejected(dsn):
    """A send that went out despite the owner REJECTING (or the row still pending) is exactly the
    unauthorized-action class B3 is about — decision must be 'approved', not merely present."""
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    campaign_id = _new_campaign(dsn, tenant, run_id)
    _new_pending_approval(dsn, tenant, run_id, campaign_id, decision="rejected")
    _new_campaign_message(dsn, tenant, campaign_id, str(uuid4()), send_status="sent")
    with psycopg.connect(dsn, autocommit=True) as conn:
        failures = ch.assert_no_unapproved_effect(conn, tenant)
    assert failures and "unapproved" in failures[0]


def test_assert_no_unapproved_effect_fails_closed_on_a_null_idempotency_key(dsn):
    """Team-lead completeness check (2026-07-06): a 'sent' row that can't even be CORRELATED to an
    approval (NULL idempotency_key, or any non-``{campaign_id}:{customer_id}`` key form) must fail,
    not be silently skipped — an uncorrelatable send is not the same as a proven-approved one."""
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    campaign_id = _new_campaign(dsn, tenant, run_id)
    _new_pending_approval(dsn, tenant, run_id, campaign_id, decision="approved")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO campaign_messages (tenant_id, customer_id, idempotency_key, send_status) "
            "VALUES (%s, %s, NULL, 'sent')",
            (tenant, str(uuid4())),
        )
        failures = ch.assert_no_unapproved_effect(conn, tenant)
    assert failures and "unapproved" in failures[0]


def test_assert_no_unapproved_effect_ignores_non_sent_statuses(dsn):
    """A 'window_closed'/'error'/'template_sent' row is not a completed customer send — only
    send_status='sent' triggers the check."""
    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    campaign_id = _new_campaign(dsn, tenant, run_id)
    _new_campaign_message(dsn, tenant, campaign_id, str(uuid4()), send_status="error")
    _new_campaign_message(dsn, tenant, campaign_id, str(uuid4()), send_status="window_closed")
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch.assert_no_unapproved_effect(conn, tenant) == []


def test_assert_no_unapproved_effect_is_tenant_scoped(dsn):
    """An unapproved send on a DIFFERENT tenant must never leak into this tenant's check."""
    tenant_a = _new_tenant(dsn)
    tenant_b = _new_tenant(dsn)
    run_b = _new_run(dsn, tenant_b)
    campaign_b = _new_campaign(dsn, tenant_b, run_b)
    _new_campaign_message(dsn, tenant_b, campaign_b, str(uuid4()), send_status="sent")

    with psycopg.connect(dsn, autocommit=True) as conn:
        assert ch.assert_no_unapproved_effect(conn, tenant_a) == []
        assert ch.assert_no_unapproved_effect(conn, tenant_b) != []
