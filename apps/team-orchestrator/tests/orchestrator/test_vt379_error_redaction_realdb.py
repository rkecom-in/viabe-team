"""VT-379 — pipeline_steps.error redaction (real Postgres).

The error column was the double blind spot: never redacted at write (the three
direct-INSERT writers — ``error_router._log_decision`` /
``sales_recovery._emit_self_evaluate_gate`` / ``collapse.record_terminal_verdict``
— bypassed the redacting writer) AND never swept by Detector-5. This suite pins
the closure end-to-end against a real migrated DB:

  (a) WRITE-TIME redaction — each of the three writers, called with a seeded
      tenant + a 2-word customer display name + a phone in the payload, now
      stores REDACTED error/output_envelope content at rest (read raw as
      service role; both the name token AND the phone gone).
  (b) THE INERT-REGISTRY TRIPWIRE (VT-374 / VT-361 lesson) — the registry that
      drives name redaction must actually populate from the seeded customer; an
      inert/empty registry (the VT-170 production-inert default) must NOT
      silently no-redact the name. We assert the populated registry IS a True
      predicate for the seeded name AND that patterns still redact the phone.
  (c) DETECTOR-5 — a planted phone in the ``error`` column (previously unswept)
      now raises a ``pii_in_log`` trigger.
  (d) BACKFILL — pre-redaction rows seeded by raw INSERT, the script redacts
      them, and a second run is idempotent (zero further changes).
  (e) DSR purge still covers pipeline_steps (cheap assert on _PURGE_ORDER).

Gated on DATABASE_URL + dbos (CL-422 — synthetic data only). Unique
uuid-suffixed identities per test so a recycled DB never collides. Reads raw
rows as the service role (RLS bypassed at seed/read time, like every realdb
suite).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")
pytest.importorskip("langgraph")  # orchestrator.graph imports langgraph transitively

import psycopg  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-379 error-redaction realdb tests skipped",
)

# The backfill script lives under scripts/ (not an importable package) — add it
# to the path like the other script-touching suites do.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


# ---------------------------------------------------------------------------
# Fixtures — apply migrations + launch DBOS so get_pool() exists (Detector-5
# reads it); the canonical realdb scaffold (mirrors test_run_control_realdb).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


@pytest.fixture(autouse=True)
def _fresh_registry_cache():
    """The in-process customer-name registry caches per tenant; a stale empty
    cache from a prior test would mask the populate. Clear before each test."""
    from orchestrator.privacy import customer_registry

    customer_registry.invalidate_all()
    yield
    customer_registry.invalidate_all()


# ---------------------------------------------------------------------------
# Seed helpers (service role — RLS bypassed at seed time).
# ---------------------------------------------------------------------------

# A 2-word display name: the only shape the redactor's free-text registry scan
# matches (bigram heuristic). A phone the pattern layer redacts regardless.
_NAME = "Rajesh Kumar"
_PHONE = "9876543210"


def _tenant(dsn: str) -> str:
    tid = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'founding', 'paid_active')",
            (tid, f"vt379-{tid[:8]}"),
        )
    return tid


def _customer(dsn: str, tenant_id: str, display_name: str = _NAME) -> None:
    """The REAL customers path the name registry reads (list_display_names)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customers (tenant_id, display_name) VALUES (%s, %s)",
            (tenant_id, display_name),
        )


def _run(dsn: str, tenant_id: str) -> str:
    run_id = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status, started_at) "
            "VALUES (%s, %s, 'twilio_inbound', 'completed', now())",
            (run_id, tenant_id),
        )
    return run_id


def _raw_step(dsn: str, step_id: str) -> dict:
    """Read a pipeline_steps row RAW as the service role (RLS bypassed)."""
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT error, output_envelope, decision_rationale, step_kind "
            "FROM pipeline_steps WHERE id = %s",
            (step_id,),
        ).fetchone()
    assert row is not None, f"step {step_id} not found"
    return row


def _latest_step_id(dsn: str, run_id: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT id FROM pipeline_steps WHERE run_id = %s "
            "ORDER BY step_seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    assert row is not None, f"no pipeline_steps for run {run_id}"
    return str(row[0])


# ---------------------------------------------------------------------------
# (a) + (b) — WRITE-TIME redaction through each of the three writers, and the
# inert-registry tripwire.
# ---------------------------------------------------------------------------


def test_error_router_stores_redacted_error(substrate) -> None:
    """error_router._log_decision (via route_failure) — the ``error`` column
    must store REDACTED failure.message + metadata at rest. The message carries
    the seeded customer name + a phone; both must be gone in the raw row."""
    from orchestrator.error_router import route_failure
    from orchestrator.failures import FailureRecord, FailureType

    dsn = substrate
    tid = _tenant(dsn)
    _customer(dsn, tid)
    rid = _run(dsn, tid)

    failure = FailureRecord(
        failure_type=FailureType.UNKNOWN_ERROR,
        message=f"send to {_NAME} at {_PHONE} blew up",
        occurred_at=datetime.now(UTC),
        tenant_id=UUID(tid),
        run_id=UUID(rid),
        vendor="twilio",
        metadata={"dropped_values": f"{_NAME} {_PHONE}"},
    )
    route_failure(failure)

    step_id = _latest_step_id(dsn, rid)
    raw = _raw_step(dsn, step_id)
    blob = json.dumps(raw["error"]) + " " + str(raw["decision_rationale"])
    assert _PHONE not in blob, f"raw error leaked the phone: {blob!r}"
    assert _NAME not in blob, f"raw error leaked the customer name: {blob!r}"
    assert "<customer_name>" in blob or "phone_tok_" in blob or "<redacted" in blob, (
        f"the error column shows no redaction token at all: {blob!r}"
    )


def test_self_evaluate_gate_stores_redacted_envelope(substrate) -> None:
    """sales_recovery._emit_self_evaluate_gate — the output_envelope
    feedback_messages free text must be redacted at rest."""
    from orchestrator.agent.sales_recovery import _emit_self_evaluate_gate

    dsn = substrate
    tid = _tenant(dsn)
    _customer(dsn, tid)
    rid = _run(dsn, tid)

    ctx = SimpleNamespace(tenant_id=tid, run_id=rid)
    outcome = SimpleNamespace(value="revise")
    feedback = [{"role": "user", "content": f"{_NAME} at {_PHONE} disliked it"}]

    _emit_self_evaluate_gate(
        context=ctx,
        attempt_number=1,
        outcome=outcome,
        rejection_feedback=None,
        feedback_messages=feedback,
    )

    step_id = _latest_step_id(dsn, rid)
    raw = _raw_step(dsn, step_id)
    blob = json.dumps(raw["output_envelope"])
    assert _PHONE not in blob, f"self_evaluate_gate envelope leaked the phone: {blob!r}"
    assert _NAME not in blob, f"self_evaluate_gate envelope leaked the name: {blob!r}"


def test_collapse_terminal_verdict_stores_redacted_envelope(substrate) -> None:
    """collapse.record_terminal_verdict — the out_of_scope_reason free text in
    output_envelope must be redacted at rest."""
    from orchestrator.agent.schemas.campaign_plan import CampaignPlanOutOfScope
    from orchestrator.collapse import record_terminal_verdict

    dsn = substrate
    tid = _tenant(dsn)
    _customer(dsn, tid)
    rid = _run(dsn, tid)

    plan = CampaignPlanOutOfScope(
        tenant_id=UUID(tid),
        run_id=UUID(rid),
        generated_at=datetime.now(UTC),
        out_of_scope_reason=f"customer {_NAME} on {_PHONE} asked an HR question",
    )
    record_terminal_verdict(UUID(tid), UUID(rid), plan)

    step_id = _latest_step_id(dsn, rid)
    raw = _raw_step(dsn, step_id)
    blob = json.dumps(raw["output_envelope"])
    assert _PHONE not in blob, f"terminal-verdict envelope leaked the phone: {blob!r}"
    assert _NAME not in blob, f"terminal-verdict envelope leaked the name: {blob!r}"


def test_inert_registry_tripwire_populates_not_silently_empty(substrate) -> None:
    """THE INERT-REGISTRY TRIPWIRE — input side (VT-374 / VT-361 lesson).

    A seeded customer must make make_name_registry a POPULATED True predicate
    for the seeded name. An inert/empty registry (the VT-170 production-inert
    default, or a stale-empty cache) returns False here and FAILS the test —
    the name token is the ONLY signal the registry did its job, because the
    phone is pattern-redacted regardless of the registry. This is the assertion
    the redaction output alone cannot give: it proves the registry actually
    read the seeded customer rather than silently no-redacting names.

    Negative control: a DIFFERENT tenant's registry (no seeded customer) is
    empty and returns False for the same name — proving the True above is the
    seeded read, not a registry that says True for everything.
    """
    from orchestrator.privacy import customer_registry

    dsn = substrate

    tid = _tenant(dsn)
    _customer(dsn, tid)
    customer_registry.invalidate_all()  # force a true read, never a stale-empty cache
    reg = customer_registry.make_name_registry(tid)
    assert reg(_NAME.casefold()) is True, (
        "make_name_registry returned an INERT/empty registry — the seeded "
        "customer was not read; write-time name redaction would silently no-op "
        "(VT-374 / VT-361 lesson)"
    )

    # Negative control: a tenant with NO seeded customer → empty registry → False.
    tid_empty = _tenant(dsn)
    reg_empty = customer_registry.make_name_registry(tid_empty)
    assert reg_empty(_NAME.casefold()) is False, (
        "an empty-customer tenant's registry returned True — it is not actually "
        "reading per-tenant customers (the tripwire would never catch an inert one)"
    )


def test_failed_registry_does_not_silently_no_redact_names(substrate) -> None:
    """A FAILED registry must NOT silently no-redact names — the redactor
    propagates the failure loudly (does NOT swallow it) so the WRITER is forced
    to fail-closed/fail-soft rather than committing an UNredacted row.

    The binding requirement (dispatch §b): an empty/FAILED registry must not
    silently no-redact. The redactor's contract is to surface a registry outage
    — NOT to quietly drop name redaction and write the name in the clear. We
    pin BOTH legs:
      (i) a raising registry propagates (not swallowed into a silent no-op);
      (ii) pattern-driven redaction (the phone) is registry-INDEPENDENT and
           still fires when no registry is supplied at all.
    """
    from orchestrator.observability.pii import redact_for_log

    payload = {"message": f"{_NAME} at {_PHONE}"}

    # (i) a raising registry is surfaced, not swallowed into a silent name-leak.
    def _boom(_text: str) -> bool:
        raise RuntimeError("synthetic customer-read outage")

    with pytest.raises(RuntimeError):
        redact_for_log(payload, name_registry=_boom)

    # (ii) patterns redact the phone with NO registry at all — the registry-
    # independent leg the writer can always rely on even mid-outage.
    out = redact_for_log(payload, name_registry=None)
    blob = json.dumps(out)
    assert _PHONE not in blob, (
        f"phone leaked with no registry — pattern redaction must be "
        f"registry-independent: {blob!r}"
    )


# ---------------------------------------------------------------------------
# (c) — Detector-5 catches a planted phone in the error column.
# ---------------------------------------------------------------------------


def test_detector5_catches_phone_in_error_column(substrate) -> None:
    """Detector-5 (detect_pii_in_logs) now scans ``error`` (previously
    envelope-only). A planted unredacted phone in the error column → a
    pii_in_log trigger. The companion clean tenant raises nothing."""
    from orchestrator.alerts.triggers import detect_pii_in_logs

    dsn = substrate

    # Dirty tenant: a step whose ERROR column (not the envelope) carries a phone.
    tid = _tenant(dsn)
    rid = _run(dsn, tid)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_steps "
            "(run_id, tenant_id, step_seq, step_kind, status, error, started_at) "
            "VALUES (%s, %s, 0, 'error', 'completed', %s, now())",
            (rid, tid, Jsonb({"message": f"call +91{_PHONE} now"})),
        )

    triggers = detect_pii_in_logs(UUID(tid))
    kinds = [t.trigger_kind for t in triggers]
    assert "pii_in_log" in kinds, (
        f"Detector-5 missed an unredacted phone in the error column; got {kinds}"
    )

    # Clean tenant: a step with no PII anywhere → no trigger.
    tid_clean = _tenant(dsn)
    rid_clean = _run(dsn, tid_clean)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_steps "
            "(run_id, tenant_id, step_seq, step_kind, status, error, started_at) "
            "VALUES (%s, %s, 0, 'error', 'completed', %s, now())",
            (rid_clean, tid_clean, Jsonb({"message": "validation failed: schema drift"})),
        )
    clean = detect_pii_in_logs(UUID(tid_clean))
    assert not any(t.trigger_kind == "pii_in_log" for t in clean), (
        "Detector-5 false-fired on a clean error column"
    )


# ---------------------------------------------------------------------------
# (d) — Backfill: seed pre-redaction rows, run the script, assert redacted +
# idempotent on re-run.
# ---------------------------------------------------------------------------


def test_backfill_redacts_existing_rows_and_is_idempotent(substrate) -> None:
    import backfill_redact_error_column as backfill

    dsn = substrate
    tid = _tenant(dsn)
    _customer(dsn, tid)
    rid = _run(dsn, tid)

    # Seed a PRE-redaction row by raw INSERT (bypassing every writer) — exactly
    # the shape a row written before VT-379 has: raw name + phone in error,
    # decision_rationale, and an identifiable-kind output_envelope.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_steps "
            "(run_id, tenant_id, step_seq, step_kind, status, error, "
            " decision_rationale, output_envelope, started_at) "
            "VALUES (%s, %s, 0, 'error', 'completed', %s, %s, %s, now())",
            (
                rid,
                tid,
                Jsonb({"message": f"{_NAME} at {_PHONE} failed", "vendor": "twilio"}),
                f"unknown_error for {_NAME} {_PHONE}",
                Jsonb({"out_of_scope_reason": f"{_NAME} on {_PHONE} off-topic"}),
            ),
        )
    step_id = _latest_step_id(dsn, rid)

    # Dry-run (default) writes nothing.
    dry = backfill.run(dsn=dsn, expected_env="dev", execute=False)
    assert dry["rows_changed"] >= 1
    pre = _raw_step(dsn, step_id)
    assert _PHONE in json.dumps(pre["error"]), "dry-run must not have written"

    # Execute — the row is redacted at rest.
    res = backfill.run(dsn=dsn, expected_env="dev", execute=True)
    assert res["rows_changed"] >= 1
    assert res["error_changed"] >= 1

    post = _raw_step(dsn, step_id)
    full = (
        json.dumps(post["error"])
        + " "
        + str(post["decision_rationale"])
        + " "
        + json.dumps(post["output_envelope"])
    )
    assert _PHONE not in full, f"backfill left a phone behind: {full!r}"
    assert _NAME not in full, f"backfill left the customer name behind: {full!r}"

    # Idempotent: a second execute touches nothing.
    again = backfill.run(dsn=dsn, expected_env="dev", execute=True)
    assert again["rows_changed"] == 0, (
        f"backfill is NOT idempotent — second run changed {again['rows_changed']} rows"
    )


def test_backfill_env_guard_refuses_wrong_env(substrate) -> None:
    """The VT-362 sentinel guard refuses when --expected-env disagrees with the
    stamped app_environment (the local DB is stamped 'dev')."""
    import apply_migrations

    import backfill_redact_error_column as backfill

    dsn = substrate
    with pytest.raises(apply_migrations.EnvironmentGuardError):
        backfill.run(dsn=dsn, expected_env="prod", execute=False)


# ---------------------------------------------------------------------------
# (e) — DSR purge still covers pipeline_steps (cheap assert).
# ---------------------------------------------------------------------------


def test_dsr_purge_covers_pipeline_steps() -> None:
    from orchestrator.dsr_purge import _PURGE_ORDER

    assert "pipeline_steps" in _PURGE_ORDER, (
        "DSR purge must hard-delete pipeline_steps (the error column lives "
        "there) — regression guard on the purge inventory"
    )
