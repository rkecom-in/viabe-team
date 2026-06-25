"""VT-423 — send-marker HARDENING canary (Rule #15, real Postgres, money-send).

Hardens the VT-420 'sending' in-flight marker against the two NON-BLOCKING residuals
the VT-420 adversarial verify surfaced. The fakes in tests/agent/tools/ prove the
control flow; THIS canary proves the SQL itself — the conditional `ON CONFLICT DO UPDATE
... WHERE send_status NOT IN ('sent','sending') RETURNING id` claim — behaves on a real
Postgres (a fake cursor cannot catch a malformed upsert / a wrong RETURNING-vs-WHERE
interaction). No mock cursors; only the Twilio leaf (`send_fn`) is stubbed so the canary
never touches the live WABA.

Residual #1 — self-serializing marker: two TRUE-parallel first-attempts on ONE draft_id
must result in exactly ONE Twilio dispatch. The marker INSERT self-serializes: Postgres'
UNIQUE(tenant_id, idempotency_key) + the conditional claim lets exactly one attempt claim
the 'sending' row; the loser observes rowcount=0 and does NOT send.

Residual #2 — the 24h stale-marker window: a 'sending' marker older than 24h must STILL
block the re-send (NOT fall out of a time window and re-fire). Money-SAFE: a 'sending' row
blocks regardless of age; only a deliberate terminal resolution (or a reconciler sweep)
unblocks it — the tool NEVER auto-re-sends.

Gated on DATABASE_URL + the dbos stack (mirrors test_send_gate_optin_realdb.py); runs in
the CI orchestrator job + the pre-push orchestrator/migrations job. CL-422 synthetic data
only; CL-390 no PII (assert on status/rowcount, never surface the raw number).
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("langchain")

import psycopg  # noqa: E402 — after dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-423 send-marker hardening canary skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so get_pool()/tenant_connection exist."""
    import apply_migrations

    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt423-marker-test-salt")
    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


# --- helpers ---------------------------------------------------------------


class _SpySend:
    """Counts Twilio calls; each call returns a distinct SID so a double-send shows."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self, *_a, **_k):  # type: ignore[no-untyped-def]
        self.n += 1
        return _SendResult(success=True, message_sid=f"MK{'s' * 28}{self.n:02d}")


class _SendResult:
    def __init__(self, *, success: bool, message_sid: str | None = None) -> None:
        self.success = success
        self.message_sid = message_sid
        self.error_code = None
        self.error_message = None


def _synthetic_phone() -> str:
    return f"+9197{uuid4().int % 10**8:08d}"


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, whatsapp_number) "
            "VALUES ('VT423 marker', 'founding', 'paid_active', %s) RETURNING id",
            (f"+9199{uuid4().int % 10**8:08d}",),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _new_consented_customer(dsn: str, tenant: UUID) -> UUID:
    """A subscribed customer WITH a recorded WhatsApp opt-in (passes every gate so the
    canary exercises the MARKER path, not an upstream refusal)."""
    from orchestrator.privacy import consent

    phone = _synthetic_phone()
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO customers (tenant_id, phone_e164, opt_out_status) "
            "VALUES (%s, %s, 'subscribed') RETURNING id",
            (str(tenant), phone),
        ).fetchone()
    assert row is not None
    consent.record_consent(tenant, phone, consent_text_version="wa_inbound_optin_v0")
    return UUID(str(row[0]))


def _send(tenant: UUID, customer: UUID, idem: str, *, send_fn):  # type: ignore[no-untyped-def]
    from orchestrator.agent.tools.send_whatsapp_template import (
        SendWhatsappTemplateInput,
        send_whatsapp_template,
    )
    from orchestrator.graph import get_pool

    payload = SendWhatsappTemplateInput(
        tenant_id=str(tenant),
        customer_id=str(customer),
        template_id="team_weekly_approval",
        language="en",
        template_params={
            "customer_segment": "SMB",
            "campaign_mode": "recovery",
            "projected_recovery_inr": "5000",
        },
        idempotency_key=idem,
    )
    return send_whatsapp_template(payload, pool=get_pool(), send_fn=send_fn)


def _marker_status(dsn: str, tenant: UUID, idem: str) -> str | None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT send_status FROM send_idempotency_keys "
            "WHERE tenant_id = %s AND idempotency_key = %s",
            (str(tenant), idem),
        ).fetchone()
    return None if row is None else str(row[0])


# --- residual #1: self-serializing marker (real-SQL claim) -----------------


def test_vt423_marker_self_serializes_against_real_pg(substrate):
    """The conditional claim works on real Postgres: when a 'sending' marker already
    exists, a second _write_inflight_marker on the same key returns False (claim
    refused) — exactly the rowcount=0 the self-serialize check reads. So two attempts
    that both passed _check_idempotency cannot both reach Twilio."""
    from orchestrator.agent.tools.send_whatsapp_template import _write_inflight_marker
    from orchestrator.graph import get_pool

    tenant = _new_tenant(substrate)
    customer = _new_consented_customer(substrate, tenant)
    idem = f"vt423-serialize-{uuid4()}"

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant),))
        first = _write_inflight_marker(cur, str(tenant), idem, str(customer))
        second = _write_inflight_marker(cur, str(tenant), idem, str(customer))

    assert first is True, "the first attempt must CLAIM the marker (real INSERT landed)"
    assert second is False, (
        "the second attempt must LOSE the claim — a 'sending' row already holds the key "
        "(this is what blocks the double-send; the WHERE excludes 'sending')"
    )
    assert _marker_status(substrate, tenant, idem) == "sending"


def test_vt423_two_parallel_first_attempts_one_twilio_call(substrate):
    """End-to-end through send_whatsapp_template: two sends on ONE idempotency_key.
    The first claims + dispatches; the second finds the 'sending' marker (via the
    in-flight hit OR the lost claim) and does NOT re-dispatch. Exactly ONE Twilio call."""
    tenant = _new_tenant(substrate)
    customer = _new_consented_customer(substrate, tenant)
    idem = f"vt423-parallel-{uuid4()}"
    spy = _SpySend()

    out_a = _send(tenant, customer, idem, send_fn=spy)
    out_b = _send(tenant, customer, idem, send_fn=spy)

    assert spy.n == 1, (
        f"DOUBLE-SEND: same-key sends dispatched twice (n={spy.n}, expected 1)"
    )
    assert out_a.status == "sent"
    assert out_b.status == "sent"  # the 2nd is an idempotent/in-flight hit, never error
    assert _marker_status(substrate, tenant, idem) == "sent"


# --- residual #2: the 24h stale-marker window ------------------------------


def test_vt423_stale_sending_marker_still_blocks_after_24h(substrate):
    """A 'sending' marker backdated >24h must STILL block the re-send — it is no longer
    bounded by _check_idempotency's 24h window. ZERO Twilio calls; the marker is left
    untouched for a reconciler (the tool never auto-re-sends a possibly-delivered send)."""
    tenant = _new_tenant(substrate)
    customer = _new_consented_customer(substrate, tenant)
    idem = f"vt423-stale-{uuid4()}"
    spy = _SpySend()

    # Plant a 25h-old 'sending' marker directly (a crash-orphaned in-flight row).
    with psycopg.connect(substrate, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant),))
        conn.execute(
            "INSERT INTO send_idempotency_keys "
            "(tenant_id, idempotency_key, customer_id, message_sid, send_status, created_at) "
            "VALUES (%s, %s, %s, NULL, 'sending', now() - interval '25 hours')",
            (str(tenant), idem, str(customer)),
        )

    out = _send(tenant, customer, idem, send_fn=spy)

    assert spy.n == 0, (
        f"DOUBLE-SEND: a >24h-stale 'sending' marker re-sent (n={spy.n}, expected 0) — "
        "the stale-window tail is open"
    )
    assert out.status == "sent"  # fail-SAFE probably-already-delivered terminal
    assert out.message_sid is None
    assert _marker_status(substrate, tenant, idem) == "sending", (
        "the tool must NOT auto-resolve/re-send the stale marker — a reconciler does"
    )


def test_vt423_fresh_marker_within_24h_blocks_too(substrate):
    """Control: a fresh (well under 24h) crash-orphaned 'sending' marker also blocks —
    the change did not loosen the in-window behaviour, only extended it past 24h."""
    tenant = _new_tenant(substrate)
    customer = _new_consented_customer(substrate, tenant)
    idem = f"vt423-fresh-{uuid4()}"
    spy = _SpySend()

    with psycopg.connect(substrate, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant),))
        conn.execute(
            "INSERT INTO send_idempotency_keys "
            "(tenant_id, idempotency_key, customer_id, message_sid, send_status, created_at) "
            "VALUES (%s, %s, %s, NULL, 'sending', now() - interval '2 minutes')",
            (str(tenant), idem, str(customer)),
        )

    out = _send(tenant, customer, idem, send_fn=spy)

    assert spy.n == 0
    assert out.status == "sent"
    assert out.message_sid is None
