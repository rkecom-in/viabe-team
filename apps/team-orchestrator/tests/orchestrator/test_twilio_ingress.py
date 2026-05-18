"""VT-3.3a/b tests — orchestrator ingress: tenant lookup, rate limiting, DBOS start.

Require a live Postgres via ``DATABASE_URL`` plus the dbos / fastapi stack;
run in the CI ``orchestrator`` job.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("fastapi")

import psycopg  # noqa: E402 — imported after the dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — twilio ingress tests skipped",
)

_SECRET = "vt-3-3-test-internal-secret"
_WORKSPACE_SENTINEL = "00000000-0000-0000-0000-000000000000"
_WORKER = Path(__file__).parent / "_ingress_resume_worker.py"


@pytest.fixture(scope="module")
def ingress():
    """Apply migrations, set test env, expose a TestClient with DBOS launched."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    apply_migrations.apply(dsn=dsn)
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ["INTERNAL_API_SECRET"] = _SECRET
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt-3-3-test-salt")

    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:  # lifespan launches DBOS
        yield SimpleNamespace(dsn=dsn, client=client)


def _new_tenant(dsn: str, whatsapp_number: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "whatsapp_number) VALUES ('VT-3.3 Test', 'founding', 'trial', now(), %s) "
            "RETURNING id",
            (whatsapp_number,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _phone() -> str:
    return f"+9199{uuid4().int % 10**8:08d}"


def _fields(sender: str, **overrides) -> dict:
    base = {
        "From": sender,
        "To": "+910000000000",
        "Body": "hello",
        "MessageSid": f"SM{uuid4().hex}",
        "NumMedia": "0",
    }
    base.update(overrides)
    return base


def _post(ingress, fields: dict, secret: str | None = _SECRET):
    headers = {"X-Internal-Secret": secret} if secret is not None else {}
    return ingress.client.post(
        "/api/orchestrator/twilio-ingress",
        json={"twilio_fields": fields},
        headers=headers,
    )


def _await_workflow(workflow_id: str):
    from dbos import DBOS

    return DBOS.retrieve_workflow(workflow_id).get_result()


def _wait_for_count(dsn: str, sql: str, params: tuple, target: int, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with psycopg.connect(dsn, autocommit=True) as conn:
            count = conn.execute(sql, params).fetchone()[0]
        if count >= target:
            return
        time.sleep(0.5)
    raise AssertionError(f"condition not met within {timeout}s: {sql}")


# --- PR-fix-1 (VT-3.3a-fix-1): ingress hardening -----------------------------


def test_empty_message_sid_returns_400(ingress):
    """C3 (CL-73): a payload with no MessageSid is rejected with 400 before
    any side-effect — not collapsed into a shared workflow_id."""
    resp = ingress.client.post(
        "/api/orchestrator/twilio-ingress",
        json={"twilio_fields": {"From": _phone(), "Body": "hello"}},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "missing MessageSid"}


def test_workflow_start_failure_writes_no_inbound_row(ingress, monkeypatch):
    """C2 (CL-72): if the workflow never starts, no twilio_inbound_events row is
    left behind — dedup recording lives inside the durable workflow boundary."""
    from dbos import DBOS

    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    fields = _fields(phone)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated DBOS.start_workflow failure")

    monkeypatch.setattr(DBOS, "start_workflow", _boom)
    resp = _post(ingress, fields)
    assert resp.status_code == 200
    assert resp.json()["reason"] == "error_logged"

    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        count = conn.execute(
            "SELECT count(*) FROM twilio_inbound_events WHERE message_sid = %s",
            (fields["MessageSid"],),
        ).fetchone()[0]
    assert count == 0


def test_health_endpoint(ingress):
    resp = ingress.client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_known_tenant_starts_workflow(ingress):
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    resp = _post(ingress, _fields(phone, Body="STOP"))
    assert resp.status_code == 200
    assert resp.json()["reason"] == "started"
    result = _await_workflow(resp.json()["workflow_id"])
    assert result["routed"] == "direct_handler"
    assert result["handler"] == "opt_out_handler"


def test_invalid_secret_returns_403(ingress):
    resp = _post(ingress, _fields(_phone()), secret="wrong")
    assert resp.status_code == 403


def test_missing_secret_header_returns_403(ingress):
    resp = _post(ingress, _fields(_phone()), secret=None)
    assert resp.status_code == 403


def test_unknown_sender_does_not_start_workflow(ingress):
    resp = _post(ingress, _fields(_phone()))  # no tenant for this number
    assert resp.status_code == 200
    assert resp.json() == {"workflow_id": None, "reason": "unknown_sender"}


def test_duplicate_message_sid_short_circuits(ingress):
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    fields = _fields(phone, Body="hello again")
    first = _post(ingress, fields)
    # Let the first workflow reach SUCCESS before the redelivery so the prior
    # status is SUCCESS -> reason 'dupe' (VT-3.3a-fix-3 four-bucket reason).
    _await_workflow(first.json()["workflow_id"])
    second = _post(ingress, fields)  # same MessageSid

    assert first.json()["reason"] == "started"
    assert second.json()["reason"] == "dupe"
    assert first.json()["workflow_id"] == second.json()["workflow_id"]

    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        count = conn.execute(
            "SELECT count(*) FROM twilio_inbound_events WHERE message_sid = %s",
            (fields["MessageSid"],),
        ).fetchone()[0]
    assert count == 1


def test_image_attachment_flagged(ingress):
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    fields = _fields(phone, NumMedia="2", MediaUrl0="https://example.com/img.jpg")
    result = _await_workflow(_post(ingress, fields).json()["workflow_id"])

    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        payload = conn.execute(
            "SELECT trigger_payload FROM pipeline_runs WHERE id = %s",
            (result["run_id"],),
        ).fetchone()[0]
    assert payload["num_media"] == 2


def test_status_callback_failed_routes_to_template_error_handler(ingress):
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    resp = _post(ingress, _fields(phone, MessageStatus="failed"))
    result = _await_workflow(resp.json()["workflow_id"])
    assert result["handler"] == "template_error_handler"


def test_webhook_received_step_record_written(ingress):
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    resp = _post(ingress, _fields(phone, Body="any update"))
    result = _await_workflow(resp.json()["workflow_id"])

    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        count = conn.execute(
            "SELECT count(*) FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'webhook_received'",
            (result["run_id"],),
        ).fetchone()[0]
    assert count == 1


def test_phone_is_tokenised_not_plaintext(ingress):
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    resp = _post(ingress, _fields(phone, Body="hello"))
    result = _await_workflow(resp.json()["workflow_id"])

    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        payload = conn.execute(
            "SELECT trigger_payload FROM pipeline_runs WHERE id = %s",
            (result["run_id"],),
        ).fetchone()[0]
    assert payload["sender_phone"] != phone
    assert payload["sender_phone"].startswith("phone_tok_")


def test_rate_limit_per_tenant_exceeded(ingress):
    phone = _phone()
    tenant_id = _new_tenant(ingress.dsn, phone)
    # Seed this tenant's current-minute bucket at the per-tenant limit (30).
    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO rate_limit_buckets (tenant_id, window_start, count) "
            "VALUES (%s, date_trunc('minute', now()), 30)",
            (tenant_id,),
        )
    resp = _post(ingress, _fields(phone))
    assert resp.status_code == 200
    assert resp.json()["reason"] == "rate_limit_exceeded"


def test_rate_limit_workspace_exceeded(ingress):
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM rate_limit_buckets WHERE tenant_id = %s", (_WORKSPACE_SENTINEL,)
        )
        conn.execute(
            "INSERT INTO rate_limit_buckets (tenant_id, window_start, count) "
            "VALUES (%s, date_trunc('minute', now()), 500)",
            (_WORKSPACE_SENTINEL,),
        )
    try:
        resp = _post(ingress, _fields(phone))
        assert resp.status_code == 200
        assert resp.json()["reason"] == "rate_limit_exceeded"
    finally:
        # Reset so later tests in this minute window are not throttled.
        with psycopg.connect(ingress.dsn, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM rate_limit_buckets WHERE tenant_id = %s",
                (_WORKSPACE_SENTINEL,),
            )


def test_hash_phone_deterministic_and_salt_sensitive():
    from orchestrator.utils.phone_token import hash_phone

    os.environ["TEAM_PHONE_HASH_SALT"] = "salt-a"
    a1 = hash_phone("+919999900001")
    a2 = hash_phone("+919999900001")
    os.environ["TEAM_PHONE_HASH_SALT"] = "salt-b"
    b = hash_phone("+919999900001")
    os.environ["TEAM_PHONE_HASH_SALT"] = "vt-3-3-test-salt"  # restore

    assert a1 == a2
    assert a1 != b


# --- PR-fix-5 (VT-3.3a-fix-2): RouteToBrain → escalated + brain-pending step --


def test_substantive_message_marks_run_escalated(ingress):
    """A substantive owner message routes to the brain. The brain is not yet
    wired (VT-3.4), so the run must end 'escalated' — never silently 'completed'
    (Pillar 7) — with an 'awaiting_brain' step carrying the pre-filter reason."""
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    resp = _post(ingress, _fields(phone, Body="I want to plan a campaign"))
    result = _await_workflow(resp.json()["workflow_id"])
    assert result["routed"] == "brain"

    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM pipeline_runs WHERE id = %s", (result["run_id"],)
        ).fetchone()[0]
        step = conn.execute(
            "SELECT output_envelope FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'awaiting_brain'",
            (result["run_id"],),
        ).fetchone()
    assert status == "escalated"
    assert step is not None, "no awaiting_brain step record written"
    assert "owner message" in step[0]["reason"]


def test_status_callback_delivered_completes_clean(ingress):
    """A 'delivered' status callback is a Reject (observability-only) — the run
    completes cleanly with status 'completed' and NO awaiting_brain record."""
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    resp = _post(ingress, _fields(phone, MessageStatus="delivered"))
    result = _await_workflow(resp.json()["workflow_id"])
    assert result["routed"] == "reject"

    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM pipeline_runs WHERE id = %s", (result["run_id"],)
        ).fetchone()[0]
        brain_steps = conn.execute(
            "SELECT count(*) FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'awaiting_brain'",
            (result["run_id"],),
        ).fetchone()[0]
    assert status == "completed"
    assert brain_steps == 0


def test_dbos_auto_resumes_mid_ingress(ingress):
    """A webhook workflow SIGKILLed after the ingress step resumes cleanly."""
    dsn = ingress.dsn
    tenant_id = _new_tenant(dsn, _phone())
    workflow_id = f"ingress-resume-{uuid4()}"

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _resume_probe ("
            "id serial PRIMARY KEY, workflow_id text, step_label text, "
            "at timestamptz DEFAULT now())"
        )

    proc1 = subprocess.Popen([sys.executable, str(_WORKER), dsn, workflow_id, tenant_id])
    try:
        _wait_for_count(
            dsn,
            "SELECT count(*) FROM pipeline_runs WHERE tenant_id = %s",
            (tenant_id,),
            1,
            timeout=50,
        )
    finally:
        proc1.kill()
    proc1.wait(timeout=15)

    proc2 = subprocess.Popen([sys.executable, str(_WORKER), dsn, workflow_id, tenant_id])
    try:
        _wait_for_count(
            dsn,
            "SELECT count(*) FROM _resume_probe WHERE workflow_id = %s",
            (workflow_id,),
            1,
            timeout=90,
        )
    finally:
        proc2.kill()
        proc2.wait(timeout=15)

    with psycopg.connect(dsn, autocommit=True) as conn:
        runs = conn.execute(
            "SELECT count(*) FROM pipeline_runs WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()[0]
        probes = conn.execute(
            "SELECT count(*) FROM _resume_probe WHERE workflow_id = %s", (workflow_id,)
        ).fetchone()[0]
    assert runs == 1, f"ingress step produced {runs} runs — expected exactly 1"
    assert probes >= 1, "workflow did not resume + finish after SIGKILL"


# --- VT-3.3a-fix-3 (CL-96): four-bucket ingress reason -----------------------


def test_ingress_reason_mapping():
    """_ingress_reason classifies a redelivery by the prior workflow's DBOS
    status. Statuses verified live against dbos WorkflowStatusString —
    'recovering' = still in flight, 'terminal_failure' = dead (Pillar 7)."""
    from orchestrator.api.twilio_ingress import _ingress_reason

    assert _ingress_reason(None) == "started"
    assert _ingress_reason(SimpleNamespace(status="SUCCESS")) == "dupe"
    for status in ("PENDING", "ENQUEUED", "DELAYED"):
        assert _ingress_reason(SimpleNamespace(status=status)) == "recovering"
    for status in ("ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"):
        assert _ingress_reason(SimpleNamespace(status=status)) == "terminal_failure"


def test_started_reason_for_new_workflow_id(ingress):
    """A first-time MessageSid — no prior workflow — returns reason 'started'."""
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    resp = _post(ingress, _fields(phone, Body="hello there"))
    assert resp.status_code == 200
    assert resp.json()["reason"] == "started"
    _await_workflow(resp.json()["workflow_id"])
