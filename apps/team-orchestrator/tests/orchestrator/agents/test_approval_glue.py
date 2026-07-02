"""VT-369 Gap-5 PR-1 — behavioral tests for ``orchestrator.agents.approval_glue``
+ the ``arm_pause_request`` per-tenant queue serialization (plan §4.1/F5).

Covered behaviours:
  - arm: persisted ``pending_approvals`` row carries draft_batch_id + counts
    ONLY (the binding no-PII-in-approvals rule — details keys pinned, no
    name-bearing values); the ``sample_message`` rides the WhatsApp template
    send exclusively (asserted on the injected ``send_fn``).
  - arm refusal: ANY other open approval for the tenant refuses the arm BEFORE
    the owner template send (typed ``ApprovalArmRefused``; defer-to-next-sweep).
  - race-loser: with the pre-check blinded, the migration-128 one-open-per-tenant
    partial unique index rejects the INSERT → same typed refusal, no second row.
  - resolution semantics (plan §4.3), via the REAL resolution choke point
    (``mark_approval_resolved``): approved → batch 'approved'; needs_changes →
    'edit_requested' + owner_feedback + edit_cycles=1, ONE regeneration max
    (second needs_changes → terminal 'rejected'); rejected → 'rejected';
    timeout → 'cancelled'.
  - shared owner-interrupt budget: ``count_recent_campaign_requests`` counts
    ``agent_customer_send`` rows alongside ``campaign_send`` (plan §4.3).

No live Twilio (send_fn injected), no LLM. DB substrate mirrors
``tests/orchestrator/business_plan/test_generator.py``: importorskip
psycopg+dbos, skipif no DATABASE_URL, migrations applied once + DBOS launched.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")
pytest.importorskip("langgraph")  # request_owner_approval imports interrupt()

import psycopg  # noqa: E402 — after dependency skip guards
from psycopg.types.json import Jsonb  # noqa: E402

from orchestrator.agents.approval_glue import (  # noqa: E402
    AGENT_APPROVAL_TEMPLATE_NAME,
    ALLOWED_DETAILS_KEYS,
    ApprovalArmRefused,
    arm_agent_send_approval,
)
from orchestrator.db import tenant_connection  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-369 approval_glue substrate tests skipped",
)

pytestmark = requires_db

_CUSTOMER_NAME = "Ravi Winbackwala"  # the PII canary string — must never reach the row


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the tenant_connection pool exists."""
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


# --- seeding helpers (direct service-role connection — RLS bypassed at seed) ---


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, whatsapp_number) "
            "VALUES ('VT-369 glue test', 'founding', 'trial', now(), 'restaurant', %s) "
            "RETURNING id",
            (f"+9198{uuid4().int % 10**8:08d}",),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _new_run(dsn: str, tenant: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
            "VALUES (%s, 'orchestrator', 'running') RETURNING id",
            (str(tenant),),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_customer(dsn: str, tenant: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO customers (tenant_id, display_name, phone_e164) "
            "VALUES (%s, %s, %s) RETURNING id",
            (str(tenant), _CUSTOMER_NAME, f"+9197{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_batch(dsn: str, tenant: UUID, *, status: str = "awaiting_approval") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        wi = conn.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
            "VALUES (%s, %s, 'sales_recovery', 'drafting') RETURNING id",
            (str(tenant), f"item-{uuid4().hex[:12]}"),
        ).fetchone()
        assert wi is not None
        row = conn.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status) "
            "VALUES (%s, %s, 'sales_recovery', %s) RETURNING id",
            (str(tenant), str(wi[0]), status),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_draft(dsn: str, tenant: UUID, batch: UUID, customer: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_drafts (tenant_id, batch_id, customer_id, template_name, "
            "params) VALUES (%s, %s, %s, 'team_winback_simple', %s) RETURNING id",
            (
                str(tenant), str(batch), str(customer),
                Jsonb({"customer_name": _CUSTOMER_NAME, "days_since_last_visit": "45"}),
            ),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_open_campaign_approval(dsn: str, tenant: UUID, run: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, summary, "
            "status, timeout_at) VALUES (%s, %s, 'campaign_send', 'approve?', 'pending', "
            "now() + interval '2 days') RETURNING id",
            (str(tenant), str(run)),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _approval_row(dsn: str, tenant: UUID, approval_id: UUID | str) -> dict[str, Any]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT approval_type, draft_batch_id::text AS draft_batch_id, summary, "
            "details, decision, status, resolved_at FROM pending_approvals "
            "WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(approval_id)),
        ).fetchone()
    assert row is not None
    return {
        "approval_type": row[0], "draft_batch_id": row[1], "summary": row[2],
        "details": row[3], "decision": row[4], "status": row[5], "resolved_at": row[6],
    }


def _batch_row(dsn: str, tenant: UUID, batch: UUID) -> dict[str, Any]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, edit_cycles, owner_feedback FROM agent_draft_batches "
            "WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        ).fetchone()
    assert row is not None
    return {"status": row[0], "edit_cycles": row[1], "owner_feedback": row[2]}


class _OkSend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, tenant_id, template_name, params, *, recipient_phone=None):
        self.calls.append((template_name, dict(params)))
        return SimpleNamespace(success=True, message_sid="SM" + "1" * 32)


def _arm(dsn: str, tenant: UUID, batch: UUID, send: _OkSend):
    run = _new_run(dsn, tenant)
    return arm_agent_send_approval(
        str(tenant), str(run), str(batch), {"drafted": 1}, send_fn=send
    )


def _resolve(tenant: UUID, approval_id, decision: str, *, owner_feedback=None) -> bool:
    """Drive the REAL resolution choke point the runner/timeout-sweep use."""
    from orchestrator.agent.approval_resume import mark_approval_resolved

    with tenant_connection(tenant) as conn, conn.transaction():
        return mark_approval_resolved(
            conn, tenant, approval_id, decision, owner_feedback=owner_feedback
        )


# ---------------------------------------------------------------------------
# ARM — no PII in the row; sample_message rides the send only
# ---------------------------------------------------------------------------


def test_arm_details_shape_has_no_pii_and_sample_rides_the_send_only(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    send = _OkSend()

    result = _arm(dsn, tenant, batch, send)
    assert result.status == "armed"
    assert result.approval_id is not None

    row = _approval_row(dsn, tenant, result.approval_id)
    assert row["approval_type"] == "agent_customer_send"
    assert row["draft_batch_id"] == str(batch)
    # The binding no-PII rule (plan §3d-1): batch id + counts ONLY.
    assert set(row["details"].keys()) <= ALLOWED_DETAILS_KEYS
    assert row["details"]["draft_count"] == 1
    persisted = json.dumps(row["details"]) + (row["summary"] or "")
    assert _CUSTOMER_NAME not in persisted, "customer PII leaked into pending_approvals"

    # The sample_message (which DOES carry the display name, for the owner's
    # eyes) went into the template send — and ONLY there.
    assert len(send.calls) == 1
    template_name, params = send.calls[0]
    assert template_name == AGENT_APPROVAL_TEMPLATE_NAME
    assert _CUSTOMER_NAME in params.get("sample_message", "")
    assert params.get("draft_count") == "1"


# ---------------------------------------------------------------------------
# ARM — queue serialization (plan §4.1/F5) + the unique-index race backstop
# ---------------------------------------------------------------------------


def test_arm_refused_when_another_approval_is_open(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_open_campaign_approval(dsn, tenant, _new_run(dsn, tenant))
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    send = _OkSend()

    with pytest.raises(ApprovalArmRefused) as exc:
        _arm(dsn, tenant, batch, send)
    assert exc.value.code == "approval_queue_busy"
    # Refused BEFORE the owner send — no template went out, no second row written.
    assert send.calls == []
    with psycopg.connect(dsn, autocommit=True) as conn:
        n = conn.execute(
            "SELECT count(*) FROM pending_approvals WHERE tenant_id = %s",
            (str(tenant),),
        ).fetchone()[0]
    assert n == 1
    # The batch is untouched — the caller (executor) owns the fail-closed cancel.
    assert _batch_row(dsn, tenant, batch)["status"] == "awaiting_approval"


def test_arm_race_loser_hits_unique_index_and_refuses(substrate, monkeypatch) -> None:
    """Blind the step-0b pre-check to simulate the lost race: the migration-128
    one-open-per-tenant partial unique index must reject the INSERT, and the
    glue must surface the SAME typed refusal (the structural backstop)."""
    from orchestrator.db.wrappers import PendingApprovalsWrapper

    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_open_campaign_approval(dsn, tenant, _new_run(dsn, tenant))
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    send = _OkSend()

    monkeypatch.setattr(
        PendingApprovalsWrapper, "find_open_for_tenant", lambda self, *a, **k: None
    )
    with pytest.raises(ApprovalArmRefused) as exc:
        _arm(dsn, tenant, batch, send)
    assert exc.value.code == "approval_queue_busy"
    with psycopg.connect(dsn, autocommit=True) as conn:
        n = conn.execute(
            "SELECT count(*) FROM pending_approvals "
            "WHERE tenant_id = %s AND resolved_at IS NULL",
            (str(tenant),),
        ).fetchone()[0]
    assert n == 1, "the race loser must not leave a second open row"


def test_arm_empty_batch_refuses(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)  # no drafts
    run = _new_run(dsn, tenant)
    with pytest.raises(ApprovalArmRefused) as exc:
        # No counts dict — the draft count falls through to the RLS read.
        arm_agent_send_approval(str(tenant), str(run), str(batch), send_fn=_OkSend())
    assert exc.value.code == "empty_batch"


# ---------------------------------------------------------------------------
# RESOLUTION semantics (plan §4.3) through the real choke point
# ---------------------------------------------------------------------------


def test_approved_flips_batch_to_approved(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    armed = _arm(dsn, tenant, batch, _OkSend())

    assert _resolve(tenant, armed.approval_id, "approved") is True
    row = _approval_row(dsn, tenant, armed.approval_id)
    assert row["decision"] == "approved" and row["resolved_at"] is not None
    assert _batch_row(dsn, tenant, batch)["status"] == "approved"


def test_needs_changes_stores_feedback_then_second_is_terminal(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    armed = _arm(dsn, tenant, batch, _OkSend())

    # 1st needs_changes → edit_requested + owner_feedback (RLS row) + edit_cycles=1.
    assert _resolve(
        tenant, armed.approval_id, "needs_changes", owner_feedback="make it softer"
    ) is True
    b = _batch_row(dsn, tenant, batch)
    assert b["status"] == "edit_requested"
    assert b["edit_cycles"] == 1
    assert b["owner_feedback"] == "make it softer"

    # Regeneration re-arms (the prior row is resolved, so the queue is free) and
    # the batch flips back to awaiting_approval.
    rearmed = _arm(dsn, tenant, batch, _OkSend())
    assert rearmed.status == "armed"
    assert _batch_row(dsn, tenant, batch)["status"] == "awaiting_approval"

    # 2nd needs_changes → terminal 'rejected' (ONE regeneration max, plan §4.3).
    assert _resolve(
        tenant, rearmed.approval_id, "needs_changes", owner_feedback="again"
    ) is True
    b2 = _batch_row(dsn, tenant, batch)
    assert b2["status"] == "rejected"
    assert b2["edit_cycles"] == 1, "the terminal path must not burn a second cycle"


def test_rejected_flips_batch_to_rejected(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    armed = _arm(dsn, tenant, batch, _OkSend())

    assert _resolve(tenant, armed.approval_id, "rejected") is True
    assert _batch_row(dsn, tenant, batch)["status"] == "rejected"


def test_timeout_cancels_batch(substrate) -> None:
    """The 30-min sweep resolves with decision='timeout' through the SAME choke
    point — the batch must land 'cancelled' (no send, plan §4.3)."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    armed = _arm(dsn, tenant, batch, _OkSend())

    assert _resolve(tenant, armed.approval_id, "timeout") is True
    row = _approval_row(dsn, tenant, armed.approval_id)
    assert row["status"] == "timed_out"
    assert _batch_row(dsn, tenant, batch)["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Shared owner-interrupt budget (plan §4.3)
# ---------------------------------------------------------------------------


def test_budget_counts_agent_rows_alongside_campaign_rows(substrate) -> None:
    from orchestrator.db.wrappers import PendingApprovalsWrapper

    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    # One agent arm (then resolve it so the queue frees) + one campaign row.
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    armed = _arm(dsn, tenant, batch, _OkSend())
    _resolve(tenant, armed.approval_id, "rejected")
    _seed_open_campaign_approval(dsn, tenant, _new_run(dsn, tenant))

    with tenant_connection(tenant) as conn:
        n = PendingApprovalsWrapper().count_recent_campaign_requests(
            tenant, days=7, conn=conn
        )
    assert n == 2, "agent_customer_send must count against the shared 2/week budget"


# ---------------------------------------------------------------------------
# VT-531 (C3) — reviewer-correction store: captured BEFORE redact_batch_close
# ---------------------------------------------------------------------------


def _corrections(dsn: str, tenant: UUID, batch: UUID) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT correction_kind, decision_verb, correction_text, retrieval_eligible "
            "FROM agent_corrections WHERE tenant_id = %s AND batch_id = %s ORDER BY created_at",
            (str(tenant), str(batch)),
        ).fetchall()
    return [
        {"kind": r[0], "verb": r[1], "text": r[2], "eligible": r[3]} for r in rows
    ]


def test_needs_changes_captures_edit_correction(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    armed = _arm(dsn, tenant, batch, _OkSend())

    _resolve(tenant, armed.approval_id, "needs_changes", owner_feedback="make it softer")
    corr = _corrections(dsn, tenant, batch)
    assert len(corr) == 1
    assert corr[0]["kind"] == "edit"
    assert corr[0]["verb"] == "needs_changes"
    assert "softer" in corr[0]["text"]
    assert corr[0]["eligible"] is False  # capture-now, retrieve-later (default closed)


def test_rejected_captures_correction(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    armed = _arm(dsn, tenant, batch, _OkSend())

    _resolve(tenant, armed.approval_id, "rejected", owner_feedback="off-brand tone")
    corr = _corrections(dsn, tenant, batch)
    assert any(c["kind"] == "reject" and "brand" in (c["text"] or "") for c in corr)


def test_correction_survives_redact_batch_close(substrate) -> None:
    """The whole point: the batch's raw owner_feedback is sha256'd on the terminal close, but the
    correction store kept the READABLE substance (captured BEFORE the destroy)."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))

    armed = _arm(dsn, tenant, batch, _OkSend())
    _resolve(tenant, armed.approval_id, "needs_changes", owner_feedback="make it softer")
    rearmed = _arm(dsn, tenant, batch, _OkSend())
    _resolve(tenant, rearmed.approval_id, "needs_changes", owner_feedback="still off")  # terminal

    # the batch's raw feedback is destroyed …
    assert _batch_row(dsn, tenant, batch)["owner_feedback"].startswith("redacted:sha256:")
    # … but the correction store kept the readable edit correction.
    corr = _corrections(dsn, tenant, batch)
    assert any(c["text"] == "make it softer" for c in corr)


def test_correction_redacts_pii(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    armed = _arm(dsn, tenant, batch, _OkSend())

    _resolve(tenant, armed.approval_id, "needs_changes",
             owner_feedback="call them on +919876543210 first")
    corr = _corrections(dsn, tenant, batch)
    assert corr and "9876543210" not in (corr[0]["text"] or "")


# ---------------------------------------------------------------------------
# VT-561 — trainable (proposal → verdict → correction) pairs
# ---------------------------------------------------------------------------


def _corrections_full(dsn: str, tenant: UUID, batch: UUID) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT correction_kind, decision_verb, correction_text, run_id::text AS run_id, "
            "proposal_snapshot, corrected_snapshot, outcome "
            "FROM agent_corrections WHERE tenant_id = %s AND batch_id = %s ORDER BY created_at",
            (str(tenant), str(batch)),
        ).fetchall()
    return [
        {
            "kind": r[0], "verb": r[1], "text": r[2], "run_id": r[3],
            "proposal": r[4], "corrected": r[5], "outcome": r[6],
        }
        for r in rows
    ]


def _draft_params(dsn: str, tenant: UUID, batch: UUID) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT params FROM agent_drafts WHERE tenant_id = %s AND batch_id = %s",
            (str(tenant), str(batch)),
        ).fetchall()
    return [r[0] or {} for r in rows]


def _approval_run_id(dsn: str, tenant: UUID, approval_id: UUID | str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT run_id::text FROM pending_approvals WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(approval_id)),
        ).fetchone()
    assert row is not None
    return row[0]


def test_reject_captures_proposal_snapshot_surviving_redaction(substrate) -> None:
    """finding (a): the draft params are sha256-DESTROYED by redact_batch_close in the resolution
    txn, but the correction row kept a PII-REDACTED, still-readable snapshot of the proposal — the
    label now points at a surviving example."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    armed = _arm(dsn, tenant, batch, _OkSend())

    _resolve(tenant, armed.approval_id, "rejected", owner_feedback="off-brand tone")

    # the drafts' raw params are sha256-destroyed (outbox_redaction shape) …
    params = _draft_params(dsn, tenant, batch)
    assert params and all(
        isinstance(v, dict) and v.get("redacted") is True and isinstance(v.get("sha256"), str)
        for p in params for v in p.values()
    ), "redact_batch_close must have sha256'd the draft params"

    # … but the correction row kept the readable, PII-redacted proposal.
    reject = next(c for c in _corrections_full(dsn, tenant, batch) if c["kind"] == "reject")
    snap = reject["proposal"]
    assert snap is not None and snap["draft_count"] == 1 and snap["captured"] == 1
    assert snap["drafts"][0]["template_name"] == "team_winback_simple", "template signal kept"
    # the params SURVIVE as substance (the key set is intact) but the PII value is redacted.
    assert "customer_name" in snap["drafts"][0]["params"]
    assert _CUSTOMER_NAME not in json.dumps(snap), "customer PII leaked into the snapshot"
    assert reject["run_id"] == _approval_run_id(dsn, tenant, armed.approval_id)


def test_edit_captures_proposal_snapshot(substrate) -> None:
    """finding (a) on the edit path: the 1st needs_changes keeps the batch 'edit_requested' for the
    one regeneration, so its drafts are still readable — the snapshot is the edited-FROM artifact."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    armed = _arm(dsn, tenant, batch, _OkSend())

    _resolve(tenant, armed.approval_id, "needs_changes", owner_feedback="make it shorter")
    edit = next(c for c in _corrections_full(dsn, tenant, batch) if c["kind"] == "edit")
    snap = edit["proposal"]
    assert snap is not None
    assert snap["drafts"][0]["template_name"] == "team_winback_simple"
    assert _CUSTOMER_NAME not in json.dumps(snap)
    assert edit["run_id"] == _approval_run_id(dsn, tenant, armed.approval_id)
    assert edit["corrected"] is None  # no corrected artifact yet (ahead-of-consumer)


def test_approve_writes_positive_lesson(substrate) -> None:
    """finding (b): approve-as-is now writes a labeled POSITIVE example (kind='approve', no
    correction_text) with the proposal snapshot — the dataset is no longer all-negatives."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    armed = _arm(dsn, tenant, batch, _OkSend())

    assert _resolve(tenant, armed.approval_id, "approved") is True
    corr = _corrections_full(dsn, tenant, batch)
    approve = [c for c in corr if c["kind"] == "approve"]
    assert len(approve) == 1, "approve-as-is must write exactly one positive lesson"
    assert approve[0]["verb"] == "approved"
    assert approve[0]["text"] is None, "a positive lesson carries no correction prose"
    assert approve[0]["proposal"] is not None
    assert approve[0]["proposal"]["drafts"][0]["template_name"] == "team_winback_simple"
    assert _CUSTOMER_NAME not in json.dumps(approve[0]["proposal"])
    assert approve[0]["run_id"] == _approval_run_id(dsn, tenant, armed.approval_id)


def test_run_id_lands_on_every_correction_kind(substrate) -> None:
    """finding (c): each correction row carries the arm-time run_id, so decision→action→outcome
    joins — even though the resolution choke point passes no run_id (it is sourced off the durable
    pending_approvals row)."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    _seed_draft(dsn, tenant, batch, _seed_customer(dsn, tenant))
    armed = _arm(dsn, tenant, batch, _OkSend())
    expected = _approval_run_id(dsn, tenant, armed.approval_id)

    _resolve(tenant, armed.approval_id, "needs_changes", owner_feedback="softer")
    corr = _corrections_full(dsn, tenant, batch)
    assert corr and all(c["run_id"] == expected for c in corr)
    assert expected is not None, "the arm-time run_id must be non-NULL to be joinable"


def test_migration_160_is_idempotent(substrate) -> None:
    """The migration must be safe to re-apply (fixtures re-apply): IF NOT EXISTS columns +
    DROP CONSTRAINT IF EXISTS before the re-add. Running the raw SQL twice is a no-op, and the
    widened CHECK admits 'approve'."""
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[5]
        / "migrations"
        / "160_vt561_agent_corrections_pairs.sql"
    )
    sql = migration.read_text()
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(sql)  # re-apply once
        conn.execute(sql)  # …and again — must not raise
        # the widened CHECK admits 'approve' (drop + re-add landed the superset constraint).
        tenant = _new_tenant(substrate.dsn)
        conn.execute(
            "INSERT INTO agent_corrections (tenant_id, correction_kind, decision_verb) "
            "VALUES (%s, 'approve', 'approved')",
            (str(tenant),),
        )
