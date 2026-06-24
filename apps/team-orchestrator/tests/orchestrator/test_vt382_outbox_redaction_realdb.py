"""VT-382 — outbox body redaction on terminal completion (CL-437 ruling 3, real Postgres).

Fazal ruled 2026-06-12 (CL-437 ruling 3): owner-facing outbox bodies are retained
ONLY while needed for delivery / retry / replay / drain. On a TERMINAL transition the
body fields are redacted — params on ``agent_drafts``, ``owner_feedback`` on
``agent_draft_batches`` — keeping metadata + hashes. The exact owner-facing SENT text is
captured FIRST into the new tenant-scoped ``owner_message_audit`` surface (STEP-0 proved
no surface holds it today). Non-terminal rows (``drafted`` / ``sending`` / batch
``edit_requested``) are NEVER redacted — retain-while-needed is itself the policy, not
just the redaction.

This is B2: the sweep + the real-DB acceptance suite. It exercises the B1-built
``orchestrator.agents.outbox_redaction`` module (the two pinned public functions —
``capture_then_redact_draft`` + ``sweep_terminal_rows``) and the inline terminal hooks
through the REAL transitions wherever practical (``agent_send_draft`` for the sent path,
``apply_agent_decision`` for batch rejected/cancelled, ``cancel_open_batches`` for halt).

Harness mirrors ``tests/orchestrator/agents/test_customer_send.py`` +
``test_run_control_realdb.py``: importorskip psycopg+dbos, skipif no DATABASE_URL,
migrations applied through the module-scoped fixture, rows seeded via a direct
service-role psycopg connection, the code under test exercised through
``tenant_connection`` (real RLS path).

VT-379 CI lesson (binding): the migration runner's PROGRAMMATIC ``apply(dsn=...)`` path
runs UNGUARDED (``expected_env=None``) and NEVER stamps the ``app_environment`` sentinel —
so NOTHING here may assume the sentinel exists or is stamped. The ``substrate`` fixture
uses exactly that unguarded path (same as every sibling realdb suite); a fresh CI DB with
no sentinel must pass.

CL-422 synthetic data only; CL-390 no PII in logs (we assert on the persisted redaction
shape + hashes, never surface raw bodies).
"""

from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after dependency skip guards
from psycopg.types.json import Jsonb  # noqa: E402

import orchestrator.templates_registry as reg  # noqa: E402
from orchestrator.agents import customer_send  # noqa: E402
from orchestrator.db import tenant_connection  # noqa: E402

# The B1-built module under test. Imported lazily-safe: if B1 has not landed yet the
# whole suite skips (the integrator re-runs once B1 + mig-135 are in the tree) rather
# than erroring collection — fresh-DB-collection-safe (VT-379 spirit: never hard-fail
# collection on an absent-but-expected build artifact).
outbox_redaction = pytest.importorskip(
    "orchestrator.agents.outbox_redaction",
    reason="VT-382 B1 module (outbox_redaction) not yet in tree — integrator re-runs",
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-382 outbox-redaction realdb suite skipped",
)


# ---------------------------------------------------------------------------
# Substrate — migrations (UNGUARDED, no sentinel assumption) + DBOS launch
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so ``tenant_connection`` / ``get_pool`` exist.

    The unguarded ``apply(dsn=...)`` path (expected_env=None) is deliberate: it is the
    throwaway-local-DB path the runner exposes for tests, it skips the VT-362 env guard,
    and it NEVER stamps ``app_environment``. A fresh CI DB with no sentinel passes here —
    the VT-379 lesson, made structural.
    """
    import apply_migrations

    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt382-outbox-redaction-test-salt")
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


# ---------------------------------------------------------------------------
# Seeding helpers (direct service-role connection — RLS bypassed at seed only).
# Unique tenants/customers per test (uuid-suffixed) so a recycled DB never collides.
# ---------------------------------------------------------------------------

_FAKE_SID = "HX" + "0123456789abcdef" * 2  # matches ^HX[0-9a-f]{32}$
_TEST_TEMPLATE = "team_winback_vt382_itest"  # injected registry-only; never in the yaml
_REAL_YAML_PATH = None  # resolved lazily in the registry fixture


def _new_tenant(dsn: str, *, name: str = "VT-382 outbox-redaction") -> UUID:
    # VT-421: agent_send_draft now has a Gate-0 ONBOARDED gate. These send/redaction tests must
    # reach the send, so the tenant is fully onboarded: paid_active + gstin_verified + ≥1 enabled+ok
    # connector (the per-test _seed_customer satisfies the ≥1-customer leg).
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, verification_status, whatsapp_number) "
            "VALUES (%s, 'founding', 'paid_active', now(), 'restaurant', 'gstin_verified', %s) "
            "RETURNING id",
            (f"{name} {uuid4().hex[:8]}", f"+9198{uuid4().int % 10**8:08d}"),
        ).fetchone()
        assert row is not None
        tenant = UUID(str(row[0]))
        conn.execute(
            "INSERT INTO tenant_connector_status (tenant_id, connector_id, enabled, last_status, "
            "last_ingested_date) VALUES (%s, %s, TRUE, 'ok', CURRENT_DATE)",
            (str(tenant), f"conn-{uuid4().hex[:8]}"),
        )
    return tenant


def _seed_customer(
    dsn: str, tenant: UUID, *, opt_out_status: str = "subscribed",
    complaint_status: str = "none", phone: str | None = None,
) -> tuple[UUID, str]:
    phone = phone or f"+9197{uuid4().int % 10**8:08d}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO customers (tenant_id, display_name, phone_e164, opt_out_status, "
            "complaint_status) VALUES (%s, 'Ravi', %s, %s, %s) RETURNING id",
            (str(tenant), phone, opt_out_status, complaint_status),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0])), phone


def _seed_work_item(dsn: str, tenant: UUID, *, agent: str = "sales_recovery") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
            "VALUES (%s, %s, %s, 'approved') RETURNING id",
            (str(tenant), f"item-{uuid4().hex[:12]}", agent),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_batch(
    dsn: str, tenant: UUID, work_item: UUID, *, status: str = "approved",
    agent: str = "sales_recovery", owner_feedback: str | None = None,
    edit_cycles: int = 0,
) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status, "
            "owner_feedback, edit_cycles) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (str(tenant), str(work_item), agent, status, owner_feedback, edit_cycles),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_draft(
    dsn: str, tenant: UUID, batch: UUID, customer: UUID, *,
    template_name: str = _TEST_TEMPLATE, params: dict[str, Any] | None = None,
    status: str = "drafted", skip_reason: str | None = None,
) -> UUID:
    body = params if params is not None else {"customer_name": "Ravi", "business_name": "Test Cafe"}
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_drafts (tenant_id, batch_id, customer_id, template_name, "
            "params, status, skip_reason) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (str(tenant), str(batch), str(customer), template_name, Jsonb(body),
             status, skip_reason),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_consent(dsn: str, tenant: UUID, phone: str, *, version: str) -> None:
    from orchestrator.utils.phone_token import hash_phone

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO record_of_consent (tenant_id, phone_token, consent_text_version) "
            "VALUES (%s, %s, %s)",
            (str(tenant), hash_phone(phone), version),
        )


def _seed_pipeline_run(dsn: str, tenant: UUID) -> UUID:
    """A minimal pipeline_run so pending_approvals.run_id FK is satisfied."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, status) VALUES (%s, 'running') RETURNING id",
            (str(tenant),),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_pending_approval(dsn: str, tenant: UUID, batch: UUID) -> UUID:
    """A resolvable agent_customer_send approval linked to the batch (so the REAL
    ``apply_agent_decision`` resolve path can drive the batch terminal). Seeds the
    pipeline_run the run_id FK requires."""
    run_id = _seed_pipeline_run(dsn, tenant)
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, summary, "
            "details, draft_batch_id, timeout_at) "
            "VALUES (%s, %s, 'agent_customer_send', %s, %s, %s, now() + interval '1 hour') "
            "RETURNING id",
            (str(tenant), str(run_id),
             f"Agent drafted message(s) — batch {batch}. Approve to send?",
             Jsonb({"draft_batch_id": str(batch), "draft_count": 1}), str(batch)),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


# --- readback helpers --------------------------------------------------------


def _draft_params(dsn: str, tenant: UUID, draft: UUID) -> tuple[str, dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, params FROM agent_drafts WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(draft)),
        ).fetchone()
    assert row is not None
    return str(row[0]), dict(row[1] or {})


def _batch_row(dsn: str, tenant: UUID, batch: UUID) -> tuple[str, str | None]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, owner_feedback FROM agent_draft_batches "
            "WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        ).fetchone()
    assert row is not None
    return str(row[0]), row[1]


def _audit_rows(dsn: str, tenant: UUID, draft: UUID) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        from psycopg.rows import dict_row

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM owner_message_audit WHERE tenant_id = %s AND draft_id = %s",
                (str(tenant), str(draft)),
            )
            return [dict(r) for r in cur.fetchall()]


# --- redaction-shape assertions (the durable, contract-pinned behaviour) -----
#
# The contract pins the AT-REST shape, not B1's private helper name:
#   params jsonb value -> {"redacted": true, "sha256": <hex>}; idempotent.
# We assert against the persisted rows so B2 is decoupled from B1's internals.


def _is_redacted_value(v: Any) -> bool:
    return (
        isinstance(v, dict)
        and v.get("redacted") is True
        and isinstance(v.get("sha256"), str)
        and len(v["sha256"]) == 64
        and all(c in "0123456789abcdef" for c in v["sha256"])
    )


def _assert_params_redacted(params: dict[str, Any]) -> None:
    assert params, "redacted params must keep the KEY set (hashes kept), only values redacted"
    for key, val in params.items():
        assert _is_redacted_value(val), f"param {key!r} not in the redacted shape: {val!r}"


# ---------------------------------------------------------------------------
# Registry + send fixtures (drive the REAL agent_send_draft sent path)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_registry_cache():
    reg._invalidate_cache()
    yield
    reg._invalidate_cache()


@pytest.fixture()
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Real yaml + one fully-sendable customer_marketing entry (SID + opt-out line)."""
    from pathlib import Path

    yaml_path = Path(__file__).resolve().parents[2] / "config" / "twilio_templates.yaml"
    data = dict(reg._load_raw(yaml_path))
    data[_TEST_TEMPLATE] = {
        "audience": "customer",
        "category": "customer_marketing",
        "optout_line": True,
        "variables": ["customer_name", "business_name"],
        "languages": {"en": _FAKE_SID},
    }
    monkeypatch.setattr(reg, "_get_cached", lambda path=None: data)
    return data


@pytest.fixture()
def allow_test_consent_version(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions", lambda: frozenset({"test-v1"})
    )
    return "test-v1"


class _FakeSendFn:
    """Records every transport call; mimics twilio_send.send_template_message's
    SendResult contract. NEVER touches the network (CL-390: no raw phone retained)."""

    def __init__(self, *, success: bool = True):
        self.success = success
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.sids: list[str] = []  # every transport-resolved SID, in call order (F2)

    def __call__(
        self, tenant_id: Any, template_name: str, params: dict[str, Any],
        *, recipient_phone: str | None = None,
    ) -> SimpleNamespace:
        self.calls.append((str(tenant_id), template_name, dict(params)))
        if self.success:
            sid = "SM" + uuid4().hex[:30]
            self.sids.append(sid)
            return SimpleNamespace(
                success=True, message_sid=sid,
                error_code=None, error_message=None,
            )
        return SimpleNamespace(
            success=False, message_sid=None,
            error_code="21211", error_message="simulated permanent failure",
        )


def _send(tenant: UUID, draft: UUID, send_fn: Any, **kw: Any):  # type: ignore[no-untyped-def]
    with tenant_connection(tenant) as conn:
        return customer_send.agent_send_draft(tenant, draft, conn=conn, send_fn=send_fn, **kw)


def _sendable_stack(dsn: str) -> SimpleNamespace:
    """A fully-sendable batch+draft (approved batch, subscribed customer, consent),
    ready for the REAL agent_send_draft sent path."""
    tenant = _new_tenant(dsn)
    customer, phone = _seed_customer(dsn, tenant)
    work_item = _seed_work_item(dsn, tenant)
    batch = _seed_batch(dsn, tenant, work_item, status="approved")
    draft = _seed_draft(dsn, tenant, batch, customer)
    _seed_consent(dsn, tenant, phone, version="test-v1")
    return SimpleNamespace(
        tenant=tenant, customer=customer, phone=phone,
        work_item=work_item, batch=batch, draft=draft,
    )


# ===========================================================================
# (b) SENT path: capture-then-redact — audit row holds EXACT text, atomic
# ===========================================================================


@pytest.mark.usefixtures("fake_registry", "allow_test_consent_version")
def test_sent_path_captures_exact_text_then_redacts_params(substrate):  # type: ignore[no-untyped-def]
    """The agent_drafts -> 'sent' terminal: the EXACT owner-facing rendered text lands in
    owner_message_audit, AND the outbox params are redacted — same txn (capture present,
    raw outbox copy gone)."""
    s = _sendable_stack(substrate.dsn)
    send_fn = _FakeSendFn()

    result = _send(s.tenant, s.draft, send_fn)
    assert result.status == "sent", f"unexpected: {result}"

    # Outbox copy redacted (values gone, key set + hashes kept).
    status, params = _draft_params(substrate.dsn, s.tenant, s.draft)
    assert status == "sent"
    _assert_params_redacted(params)

    # Audit surface holds exactly one row with the EXACT pre-redaction text.
    audit = _audit_rows(substrate.dsn, s.tenant, s.draft)
    assert len(audit) == 1, "exactly one owner_message_audit row per sent draft"
    row = audit[0]
    # F2: the audit row stores the REAL transport-resolved Twilio SID — the exact
    # value the (fake) transport returned, identical end-to-end: transport ->
    # send result -> agent_drafts.message_sid -> owner_message_audit.message_sid.
    assert len(send_fn.sids) == 1
    assert row["message_sid"] == send_fn.sids[0] == result.message_sid
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        sid_row = conn.execute(
            "SELECT message_sid FROM agent_drafts WHERE tenant_id = %s AND id = %s",
            (str(s.tenant), str(s.draft)),
        ).fetchone()
    assert sid_row is not None and sid_row[0] == send_fn.sids[0]
    assert str(row["batch_id"]) == str(s.batch)
    rendered = row["rendered_text"]
    assert rendered, "rendered_text must hold the exact owner-facing message"
    # The exact rendered text carries the real (pre-redaction) param values.
    assert "Ravi" in rendered and "Test Cafe" in rendered, rendered


@pytest.mark.usefixtures("fake_registry", "allow_test_consent_version")
def test_sent_capture_and_redact_are_atomic_both_or_neither(  # type: ignore[no-untyped-def]
    substrate, monkeypatch,
):
    """Force a mid-txn failure AFTER capture: the whole send txn rolls back, so there is
    NO window where the audit row exists but the outbox copy is gone (or vice-versa).
    Both-or-neither by construction (the contract's same-txn atomicity claim)."""
    s = _sendable_stack(substrate.dsn)

    # Poison capture_then_redact_draft to raise after it would have written the audit row,
    # proving the outer send transaction rolls BOTH back.
    real = outbox_redaction.capture_then_redact_draft

    def _boom(conn, draft_row, *a, **k):  # type: ignore[no-untyped-def]
        real(conn, draft_row, *a, **k)
        raise RuntimeError("forced mid-txn failure after capture+redact")

    monkeypatch.setattr(outbox_redaction, "capture_then_redact_draft", _boom)

    with pytest.raises(RuntimeError, match="forced mid-txn failure"):
        _send(s.tenant, s.draft, _FakeSendFn())

    # Neither side persisted: no audit row, params NOT redacted (still raw, retain-while-needed).
    assert _audit_rows(substrate.dsn, s.tenant, s.draft) == []
    status, params = _draft_params(substrate.dsn, s.tenant, s.draft)
    # F4: the 'sent' flip is INSIDE the same explicit transaction as capture+redact —
    # the rollback must take the flip with it. Pins the flip's in-txn position against
    # a regression that moves it outside the BEGIN/COMMIT (a flipped-but-uncaptured
    # row would look terminal to every redaction leg).
    assert status != "sent", "status flip must roll back with the failed capture txn"
    assert status == "drafted", f"draft must return to pre-send state, got {status!r}"
    assert params.get("customer_name") == "Ravi", "params must be intact after rollback"
    assert not any(_is_redacted_value(v) for v in params.values())


# ===========================================================================
# (a) Each terminal draft path redacts params; skipped/halted write NO audit
# ===========================================================================


@pytest.mark.usefixtures("fake_registry")
def test_skipped_draft_redacts_params_without_capture(substrate):  # type: ignore[no-untyped-def]
    """A draft skipped at a gate (opted-out customer) is terminal: params redacted,
    NO audit row (nothing was sent — capture is for owner-facing SENT text only)."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant, opt_out_status="opted_out")
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="approved")
    draft = _seed_draft(substrate.dsn, tenant, batch, customer)

    result = _send(tenant, draft, _FakeSendFn())
    assert result.status == "skipped"

    status, params = _draft_params(substrate.dsn, tenant, draft)
    assert status == "skipped"
    _assert_params_redacted(params)
    assert _audit_rows(substrate.dsn, tenant, draft) == [], "skipped sends capture nothing"


def test_halted_draft_redacts_params_without_capture(substrate):  # type: ignore[no-untyped-def]
    """The autonomy revoke/freeze cancel-all path halts non-terminal drafts; halted is
    terminal -> params redacted, no audit row."""
    from orchestrator.agents.autonomy import cancel_open_batches

    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="sending")
    draft = _seed_draft(substrate.dsn, tenant, batch, customer)

    with tenant_connection(tenant) as conn:
        cancel_open_batches(tenant, "sales_recovery", reason="freeze_test", conn=conn)

    status, params = _draft_params(substrate.dsn, tenant, draft)
    assert status == "halted"
    _assert_params_redacted(params)
    assert _audit_rows(substrate.dsn, tenant, draft) == []


# ===========================================================================
# (a) Batch terminal paths redact owner_feedback via the REAL resolve path —
#     AND (gate F1) halt+redact the batch's still-'drafted' children
# ===========================================================================


@pytest.mark.parametrize(
    "decision,seed_edit_cycles,expected_status,expected_halt_reason",
    [
        ("rejected", 0, "rejected", "halted_batch_rejected"),
        ("timeout", 0, "cancelled", "halted_batch_cancelled"),
        # Edit-exhausted: a SECOND needs_changes (edit_cycles already at the max)
        # resolves terminal 'rejected' — same close, same halt+redact obligations.
        ("needs_changes", 1, "rejected", "halted_batch_rejected"),
    ],
)
def test_batch_terminal_resolve_redacts_feedback_and_halts_children(  # type: ignore[no-untyped-def]
    substrate, decision, seed_edit_cycles, expected_status, expected_halt_reason,
):
    """Drive the REAL apply_agent_decision resolve path: rejected / timeout-cancelled /
    edit-exhausted are terminal batch closes -> owner_feedback redacted to its sha256
    marker (a non-PII string), AND — the gate-F1 blocker — the batch's child 'drafted'
    rows flip terminal 'halted' with params redacted. Pre-fix those children stayed
    'drafted' with raw params FOREVER (outside every redaction leg: the sweep correctly
    excludes non-terminal rows and nothing else ever flipped them). No audit rows:
    nothing was sent."""
    from orchestrator.agents.approval_glue import apply_agent_decision

    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    raw_feedback = "Please make the tone warmer and mention the festival"
    batch = _seed_batch(
        substrate.dsn, tenant, work_item, status="awaiting_approval",
        owner_feedback=raw_feedback, edit_cycles=seed_edit_cycles,
    )
    drafts = [_seed_draft(substrate.dsn, tenant, batch, customer) for _ in range(2)]
    approval = _seed_pending_approval(substrate.dsn, tenant, batch)

    with tenant_connection(tenant) as conn:
        out = apply_agent_decision(conn, tenant, {"id": str(approval)}, decision)
    assert out is not None and out.batch_status == expected_status

    status, owner_feedback = _batch_row(substrate.dsn, tenant, batch)
    assert status == expected_status
    assert owner_feedback is not None
    assert owner_feedback != raw_feedback, "owner_feedback must be redacted on terminal"
    # The marker is the value's sha256 (forensics/idempotency) — never the raw body.
    assert raw_feedback not in owner_feedback
    expected_hash = hashlib.sha256(raw_feedback.encode()).hexdigest()
    assert expected_hash in owner_feedback, f"sha256 marker missing: {owner_feedback!r}"

    # Gate F1: children must not be stranded 'drafted' under a terminally-closed batch.
    for draft in drafts:
        d_status, d_params = _draft_params(substrate.dsn, tenant, draft)
        assert d_status == "halted", (
            f"child draft stranded {d_status!r} under a {expected_status} batch"
        )
        _assert_params_redacted(d_params)
        assert _audit_rows(substrate.dsn, tenant, draft) == [], (
            "halted children were never sent — capture must write nothing"
        )
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT DISTINCT skip_reason FROM agent_drafts "
            "WHERE tenant_id = %s AND batch_id = %s",
            (str(tenant), str(batch)),
        ).fetchall()
    assert [r[0] for r in rows] == [expected_halt_reason]


# ===========================================================================
# (c) Retry pre-terminal keeps raw params; a later send succeeds end-to-end
# ===========================================================================


@pytest.mark.usefixtures("fake_registry", "allow_test_consent_version")
def test_transient_failure_keeps_raw_params_then_send_succeeds(substrate):  # type: ignore[no-untyped-def]
    """A transient Twilio failure leaves the draft 'drafted' (NON-terminal): params MUST
    survive raw for the retry. A subsequent send then succeeds end-to-end and only THEN
    redacts + captures."""
    s = _sendable_stack(substrate.dsn)

    failing = _FakeSendFn(success=False)
    r1 = _send(s.tenant, s.draft, failing)
    assert r1.status == "failed", r1

    # Pre-terminal: params intact (retain-while-needed), no audit, no redaction.
    status, params = _draft_params(substrate.dsn, s.tenant, s.draft)
    assert status == "drafted"
    assert params.get("customer_name") == "Ravi"
    assert not any(_is_redacted_value(v) for v in params.values())
    assert _audit_rows(substrate.dsn, s.tenant, s.draft) == []

    # VT-387 FIXED the VT-262 retry-window bug: 'error' is no longer in
    # _IDEMPOTENT_HIT_STATUSES, so a within-window retry of a transiently-errored
    # draft re-evaluates + re-sends (no longer echoes the cached error for 24h).
    # This backdate is now redundant (the cached 'error' row already isn't a hit)
    # but kept as a defensive no-op so this test stays focused on VT-382's
    # retain-while-needed policy regardless of the idempotency TTL.
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE send_idempotency_keys SET created_at = now() - interval '25 hours' "
            "WHERE tenant_id = %s AND idempotency_key = %s",
            (s.tenant, f"agent:{s.draft}"),
        )

    # Retry succeeds end-to-end -> NOW terminal -> redact + capture.
    r2 = _send(s.tenant, s.draft, _FakeSendFn())
    assert r2.status == "sent", r2
    status2, params2 = _draft_params(substrate.dsn, s.tenant, s.draft)
    assert status2 == "sent"
    _assert_params_redacted(params2)
    assert len(_audit_rows(substrate.dsn, s.tenant, s.draft)) == 1


# ===========================================================================
# (d) FORCED mid-retry redaction must NOT occur: the sweep leaves
#     drafted / sending / edit_requested rows UNTOUCHED
# ===========================================================================


def test_sweep_does_not_touch_nonterminal_rows(substrate):  # type: ignore[no-untyped-def]
    """The sweep redacts ONLY terminal rows. A 'drafted' draft, a 'sending' draft, and an
    'edit_requested' batch (owner_feedback IS the regeneration input) are ALL left raw —
    retain-while-needed is the policy, not just the redaction."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)

    # drafted (pre-send) + sending (mid-send) drafts, both non-terminal.
    b_drafted = _seed_batch(substrate.dsn, tenant, work_item, status="approved")
    d_drafted = _seed_draft(substrate.dsn, tenant, b_drafted, customer, status="drafted")
    b_sending = _seed_batch(substrate.dsn, tenant, work_item, status="sending")
    d_sending = _seed_draft(substrate.dsn, tenant, b_sending, customer, status="drafted")
    # edit_requested batch: owner_feedback is the regeneration input — NEVER redacted.
    raw_fb = "regenerate with a discount mention"
    b_edit = _seed_batch(
        substrate.dsn, tenant, work_item, status="edit_requested", owner_feedback=raw_fb,
    )

    n = outbox_redaction.sweep_terminal_rows()
    assert isinstance(n, dict) or isinstance(n, int)  # counts-only return

    for draft in (d_drafted, d_sending):
        _, params = _draft_params(substrate.dsn, tenant, draft)
        assert params.get("customer_name") == "Ravi", "non-terminal draft must stay raw"
        assert not any(_is_redacted_value(v) for v in params.values())
    _, fb = _batch_row(substrate.dsn, tenant, b_edit)
    assert fb == raw_fb, "edit_requested owner_feedback is the regeneration input — untouched"


# ===========================================================================
# (N1) Sweep crash-window backstop: 'drafted' children STRANDED under an
#      already-terminal batch are halted + redacted — the children, not just
#      the parent (the privacy capstone). Idempotent; no audit; isolation held.
# ===========================================================================


@pytest.mark.parametrize("batch_status", ["cancelled", "rejected"])
def test_sweep_halts_drafted_children_under_terminal_batch(substrate, batch_status):  # type: ignore[no-untyped-def]
    """The crash window: a terminal batch close flipped the PARENT terminal but died
    before the child halt-flip (redact_batch_close / apply_agent_decision). The child
    sits 'drafted' with RAW params OUTSIDE every other redaction leg — the inline hooks
    and Legs 1-2 all require a terminal status, and nothing else ever flips it. We seed
    that exact crash state with a raw INSERT (status='drafted' under a terminal batch,
    bypassing every hook), run the sweep, and assert Leg 3 halts + redacts the child:
    status 'halted' (skip_reason halted_sweep_terminal_batch), params redacted, ZERO
    audit rows (never sent — capturing never-sent text would itself violate retention),
    and a second sweep is a no-op (idempotent).

    Isolation guard in the SAME pass: a 'drafted' child under a NON-terminal ('approved')
    batch stays raw + 'drafted' — Leg 3's parent-terminal SQL guard never touches it."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)

    # Crash state: terminal batch, two 'drafted' children still holding raw params.
    term_batch = _seed_batch(substrate.dsn, tenant, work_item, status=batch_status)
    stranded = [
        _seed_draft(substrate.dsn, tenant, term_batch, customer, status="drafted")
        for _ in range(2)
    ]
    # Isolation control: a 'drafted' child under a still-LIVE (non-terminal) batch.
    live_batch = _seed_batch(substrate.dsn, tenant, work_item, status="approved")
    live_child = _seed_draft(substrate.dsn, tenant, live_batch, customer, status="drafted")

    counts = outbox_redaction.sweep_terminal_rows()
    assert isinstance(counts, dict)
    assert counts.get("children_halted", 0) >= 2

    # Stranded children: halted + params redacted + NO audit row.
    for draft in stranded:
        status, params = _draft_params(substrate.dsn, tenant, draft)
        assert status == "halted", f"stranded child not halted: {status!r}"
        _assert_params_redacted(params)
        assert _audit_rows(substrate.dsn, tenant, draft) == [], (
            "halted child was never sent — capture must write nothing"
        )
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        reasons = conn.execute(
            "SELECT DISTINCT skip_reason FROM agent_drafts "
            "WHERE tenant_id = %s AND batch_id = %s",
            (str(tenant), str(term_batch)),
        ).fetchall()
    assert [r[0] for r in reasons] == ["halted_sweep_terminal_batch"]

    # Isolation: the child under the live batch is untouched (still drafted + raw).
    live_status, live_params = _draft_params(substrate.dsn, tenant, live_child)
    assert live_status == "drafted", "child under a non-terminal batch must stay drafted"
    assert live_params.get("customer_name") == "Ravi"
    assert not any(_is_redacted_value(v) for v in live_params.values())

    # Idempotent: a second sweep flips/redacts nothing new for these children.
    after_1 = {d: _draft_params(substrate.dsn, tenant, d) for d in stranded}
    counts2 = outbox_redaction.sweep_terminal_rows()
    for draft in stranded:
        assert _draft_params(substrate.dsn, tenant, draft) == after_1[draft], (
            "second sweep must not re-touch an already-halted+redacted child"
        )
        assert _audit_rows(substrate.dsn, tenant, draft) == []
    # The live child is STILL untouched after the second pass.
    assert _draft_params(substrate.dsn, tenant, live_child)[0] == "drafted"
    assert isinstance(counts2, dict)


# ===========================================================================
# (f) Sweep: backfills EXISTING terminal rows + historical-capture leg + idempotent
# ===========================================================================


def test_sweep_backfills_existing_terminal_rows(substrate):  # type: ignore[no-untyped-def]
    """The backfill clause (CL-437 ruling 3.3): a row ALREADY in a terminal status with raw
    params (e.g. a pre-VT-382 sent row, or one the inline hook missed) is redacted by the
    sweep. owner_feedback on an already-terminal batch redacts too."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    raw_fb = "older feedback still raw at rest"
    batch = _seed_batch(
        substrate.dsn, tenant, work_item, status="rejected", owner_feedback=raw_fb,
    )
    # An already-'sent' draft still holding raw params (pre-VT-382 history).
    sent_draft = _seed_draft(
        substrate.dsn, tenant, batch, customer, status="sent",
        params={"customer_name": "Old Ravi", "business_name": "Old Cafe"},
    )
    skipped_draft = _seed_draft(
        substrate.dsn, tenant, batch, customer, status="skipped",
        skip_reason="skipped_opt_out",
    )

    outbox_redaction.sweep_terminal_rows()

    for draft in (sent_draft, skipped_draft):
        _, params = _draft_params(substrate.dsn, tenant, draft)
        _assert_params_redacted(params)
    _, fb = _batch_row(substrate.dsn, tenant, batch)
    assert fb is not None and raw_fb not in fb
    assert hashlib.sha256(raw_fb.encode()).hexdigest() in fb


def test_sweep_historical_capture_leg_for_sent_rows_with_raw_params(substrate):  # type: ignore[no-untyped-def]
    """Policy-honesty leg (contract B2.1): a historical 'sent' draft STILL holding raw
    params has its exact owner-facing text reconstructed + captured into owner_message_audit
    BEFORE the sweep redacts it — so no owner-facing text is silently lost. One-shot."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="sent")
    sent_draft = _seed_draft(
        substrate.dsn, tenant, batch, customer, status="sent",
        params={"customer_name": "Historical Ravi", "business_name": "Historical Cafe"},
    )

    # No audit row exists yet (pre-VT-382 history never captured).
    assert _audit_rows(substrate.dsn, tenant, sent_draft) == []

    outbox_redaction.sweep_terminal_rows()

    audit = _audit_rows(substrate.dsn, tenant, sent_draft)
    assert len(audit) == 1, "historical sent row must be captured before redaction"
    rendered = audit[0]["rendered_text"]
    assert "Historical Ravi" in rendered and "Historical Cafe" in rendered, rendered
    # And the outbox copy is now redacted.
    _, params = _draft_params(substrate.dsn, tenant, sent_draft)
    _assert_params_redacted(params)


def test_sweep_is_idempotent(substrate):  # type: ignore[no-untyped-def]
    """A second sweep pass is a no-op on already-redacted rows (idempotent: already-redacted
    values pass through unchanged; no duplicate historical-capture audit rows)."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="sent")
    sent_draft = _seed_draft(
        substrate.dsn, tenant, batch, customer, status="sent",
        params={"customer_name": "Idem Ravi", "business_name": "Idem Cafe"},
    )

    outbox_redaction.sweep_terminal_rows()
    _, params_after_1 = _draft_params(substrate.dsn, tenant, sent_draft)
    audit_after_1 = _audit_rows(substrate.dsn, tenant, sent_draft)

    outbox_redaction.sweep_terminal_rows()
    _, params_after_2 = _draft_params(substrate.dsn, tenant, sent_draft)
    audit_after_2 = _audit_rows(substrate.dsn, tenant, sent_draft)

    assert params_after_1 == params_after_2, "second sweep must not re-hash redacted values"
    assert len(audit_after_2) == len(audit_after_1) == 1, "no duplicate historical capture"


# ===========================================================================
# (e) DSR purge covers owner_message_audit + does not break the agent-table purge
# ===========================================================================


def test_owner_message_audit_in_purge_order(substrate):  # type: ignore[no-untyped-def]
    """owner_message_audit is a new at-rest owner-facing-text surface — it MUST be in
    _PURGE_ORDER (children-first, beside the agent tables) or a DSR-delete leaves the
    exact owner-facing text behind."""
    from orchestrator.dsr_purge import _PURGE_ORDER

    assert "owner_message_audit" in _PURGE_ORDER, (
        "owner_message_audit must be swept on DSR (it holds exact owner-facing text)"
    )
    # Children-first: the audit row FKs tenants but holds draft_id/batch_id linkage — it
    # must be swept no later than the agent tables it references for linkage hygiene.
    assert _PURGE_ORDER.index("owner_message_audit") <= _PURGE_ORDER.index("agent_work_items")


@pytest.mark.usefixtures("fake_registry", "allow_test_consent_version")
def test_dsr_purge_hard_deletes_audit_and_agent_tables(substrate):  # type: ignore[no-untyped-def]
    """A tenant DSR-delete hard-deletes owner_message_audit AND still purges the agent
    tables cleanly (redaction shrinks at-rest PII; DSR still hard-deletes)."""
    from orchestrator.dsr_purge import purge_tenant_data

    s = _sendable_stack(substrate.dsn)
    result = _send(s.tenant, s.draft, _FakeSendFn())
    assert result.status == "sent"
    assert len(_audit_rows(substrate.dsn, s.tenant, s.draft)) == 1

    # Open a DSR deletion ticket for the tenant.
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        trow = conn.execute(
            "INSERT INTO dsr_tickets (tenant_id, request_type, status, requested_at) "
            "VALUES (%s, 'deletion', 'open', now()) RETURNING id",
            (str(s.tenant),),
        ).fetchone()
    assert trow is not None
    purge_tenant_data(UUID(str(trow[0])))

    # Audit + agent rows hard-deleted; the purge did not error.
    assert _audit_rows(substrate.dsn, s.tenant, s.draft) == []
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        n_drafts = conn.execute(
            "SELECT count(*) FROM agent_drafts WHERE tenant_id = %s", (str(s.tenant),)
        ).fetchone()
    assert n_drafts is not None and int(n_drafts[0]) == 0


# ===========================================================================
# (F5) owner_message_audit deny-direction canary — zero VTR grants + tenant RLS
# ===========================================================================


def _seed_audit_row(dsn: str, tenant: UUID) -> UUID:
    """Direct service-role INSERT of an audit row (RLS bypassed at seed only —
    same posture as every other seed helper here). Synthetic text (CL-422)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO owner_message_audit (tenant_id, draft_id, batch_id, "
            "template_name, rendered_text, message_sid) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (str(tenant), str(uuid4()), str(uuid4()), _TEST_TEMPLATE,
             "[deny-canary] customer_name: Ravi", "SM" + uuid4().hex[:30]),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def test_owner_message_audit_vtr_roles_cannot_select(substrate):  # type: ignore[no-untyped-def]
    """Deny-direction canary (mig-135 'ZERO app_vtr_role grants — BY DESIGN'): BOTH VTR
    roles are denied SELECT on owner_message_audit — proven by has_table_privilege
    (catches PUBLIC / default-privilege leakage) AND a direct denied probe (the standing
    real-DB-RLS lesson: assert the deny direction, never just the allow)."""
    from psycopg import errors as pg_errors

    _seed_audit_row(substrate.dsn, _new_tenant(substrate.dsn))  # a real row to be denied
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        for role in ("app_vtr_role", "app_vtr_admin_role"):
            has = conn.execute(
                "SELECT has_table_privilege(%s, 'owner_message_audit', 'SELECT')",
                (role,),
            ).fetchone()[0]
            assert has is False, f"{role} unexpectedly has SELECT on owner_message_audit"
            with conn.cursor() as cur:
                cur.execute(f"SET ROLE {role}")  # noqa: S608 — fixed two-role allowlist
                with pytest.raises(pg_errors.InsufficientPrivilege):
                    cur.execute("SELECT 1 FROM owner_message_audit LIMIT 1")
                cur.execute("ROLLBACK")
                cur.execute("RESET ROLE")


def test_owner_message_audit_cross_tenant_read_is_zero_rows(substrate):  # type: ignore[no-untyped-def]
    """Tenant-RLS isolation on the audit surface: under tenant B's app_current_tenant
    GUC (tenant_connection — the real RLS path) tenant A's audit row is ZERO rows;
    tenant A sees its own row. FORCE RLS + the mig-135 per-command policies."""
    tenant_a = _new_tenant(substrate.dsn)
    tenant_b = _new_tenant(substrate.dsn)
    audit_id = _seed_audit_row(substrate.dsn, tenant_a)

    def _count(tenant: UUID) -> int:
        with tenant_connection(tenant) as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM owner_message_audit WHERE id = %s",
                (str(audit_id),),
            ).fetchone()
        return int(row["n"] if isinstance(row, dict) else row[0])

    assert _count(tenant_b) == 0, "cross-tenant audit read must be zero rows (RLS)"
    assert _count(tenant_a) == 1, "the owning tenant must see its own audit row"


# ===========================================================================
# (g) Sentinel/bootstrap-safe on a FRESH DB — the VT-379 CI lesson, asserted
# ===========================================================================


def test_no_app_environment_sentinel_assumption(substrate):  # type: ignore[no-untyped-def]
    """VT-379 lesson, made an explicit assertion: this suite's substrate uses the UNGUARDED
    apply() path, which never stamps the app_environment sentinel. The suite must work whether
    or not the sentinel exists — so we never read it. If a prior guarded run stamped it that's
    fine; if a fresh CI DB has none, the suite still passes (this very fixture proved it by
    migrating + running every test above without touching app_environment)."""
    # owner_message_audit (mig-135) is present after the unguarded migrate — bootstrap-safe.
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        reg_row = conn.execute("SELECT to_regclass('public.owner_message_audit')").fetchone()
    assert reg_row is not None and reg_row[0] is not None, (
        "mig-135 owner_message_audit must apply via the unguarded fresh-DB path"
    )
