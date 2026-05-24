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


def test_body_is_redacted_from_pipeline_runs_and_pipeline_steps(ingress):
    """Component 0 — body redaction at the persistence seam.

    Sends an identifiable plaintext body; reads back both
    ``pipeline_runs.trigger_payload`` and ``pipeline_steps.input_envelope``;
    asserts the body key is absent in both AND that no row in the live
    DB carries the plaintext substring after the run completes. The
    MessageSid provenance handle is still present in
    ``trigger_payload``.
    """
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    secret_body = f"REDACT-PROBE-{uuid4().hex}-message"
    sid = f"SM{uuid4().hex}"
    resp = _post(ingress, _fields(phone, Body=secret_body, MessageSid=sid))
    result = _await_workflow(resp.json()["workflow_id"])

    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        trigger_payload = conn.execute(
            "SELECT trigger_payload FROM pipeline_runs WHERE id = %s",
            (result["run_id"],),
        ).fetchone()[0]
        step_envelope = conn.execute(
            "SELECT input_envelope FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'webhook_received'",
            (result["run_id"],),
        ).fetchone()[0]

    # (a) trigger_payload has no body key; (c) MessageSid provenance kept.
    assert "body" not in trigger_payload, (
        f"plaintext body leaked into trigger_payload: {trigger_payload!r}"
    )
    assert trigger_payload.get("twilio_message_sid") == sid

    # (b) input_envelope has no body key.
    assert "body" not in step_envelope, (
        f"plaintext body leaked into input_envelope: {step_envelope!r}"
    )
    assert step_envelope.get("twilio_message_sid") == sid

    # Defence in depth — the secret substring must not appear anywhere
    # in either JSONB payload, regardless of which key it might have
    # landed under.
    import json as _json
    serialised = _json.dumps(trigger_payload) + _json.dumps(step_envelope)
    assert secret_body not in serialised, (
        "plaintext body substring leaked into persisted JSONB"
    )


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


# --- VT-OIV: owner_inputs extraction flag-flip verification -----------------
#
# Brief goal-items 1-4, 6: flipping ``OWNER_INPUTS_EXTRACTION_ENABLED`` to
# True yields exactly one ``owner_inputs`` row carrying derived fields,
# leaks no raw body to any of the three under-our-control persistence
# sinks (``owner_inputs``, ``pipeline_runs.trigger_payload``,
# ``pipeline_steps.input_envelope``), and the Composer's read-path
# (``_build_pending_owner_inputs``) returns the just-written row. The
# fourth sink — ``dbos.workflow_status.inputs`` — retains the raw body
# for ~2.5h per CL-385 (intentional, replay-critical); this is the
# accepted-and-documented surface and is NOT asserted on here. See
# ``test_dbos_layer_not_synchronously_purged_documented_finding`` in
# ``test_dsr_purge_substrate.py`` for the lock that the DBOS layer
# stays time-based.


def test_owner_inputs_extraction_writes_structured_row(ingress, monkeypatch):
    """Brief goal-items 1-4: extraction on → exactly one derived row,
    no raw body in any of the three under-our-control sinks, and the
    Composer's ``_build_pending_owner_inputs`` returns it.

    Goal #4 (Composer read-path) is closed by the assertion block at
    the end of this test — the just-written row must surface through
    the real Composer read path under the ``consumed_at IS NULL``
    filter.
    """
    from orchestrator import runner as runner_mod
    from orchestrator.context_builder import _build_pending_owner_inputs
    from orchestrator.owner_inputs.writer import OwnerInputClassification

    monkeypatch.setattr(runner_mod, "OWNER_INPUTS_EXTRACTION_ENABLED", True)
    # The writer's classifier seam is gated on the env var (see
    # ``run_extraction_for_event``'s early-skip) — set a sentinel so
    # the gate opens; the real SDK call is bypassed by the
    # ``classify_message`` patch below.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-sentinel")
    monkeypatch.setattr(
        "orchestrator.owner_inputs.writer.classify_message",
        lambda body, client=None: OwnerInputClassification(
            intent="winback", segment="dormant_60d", occasion="diwali"
        ),
    )

    phone = _phone()
    tenant_id = _new_tenant(ingress.dsn, phone)
    secret_body = f"OWNER-INPUT-PROBE-{uuid4().hex}-msg"
    sid = f"SM{uuid4().hex}"
    resp = _post(ingress, _fields(phone, Body=secret_body, MessageSid=sid))
    result = _await_workflow(resp.json()["workflow_id"])

    # --- owner_inputs row shape ------------------------------------
    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT intent, segment, occasion, message_sid, run_id, "
            "consumed_at, created_at FROM owner_inputs "
            "WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchall()
    assert len(rows) == 1, (
        f"expected exactly one owner_inputs row for tenant {tenant_id}; "
        f"got {len(rows)}"
    )
    intent, segment, occasion, msg_sid, row_run_id, consumed_at, created_at = rows[0]
    assert intent == "winback"
    assert segment == "dormant_60d"
    assert occasion == "diwali"
    assert msg_sid == sid
    assert str(row_run_id) == result["run_id"]
    assert consumed_at is None
    assert created_at is not None

    # --- sink 1: owner_inputs row JSON does not contain raw body ---
    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        row_json = conn.execute(
            "SELECT row_to_json(owner_inputs.*)::text FROM owner_inputs "
            "WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()[0]
    assert secret_body not in row_json, (
        "raw body substring leaked into owner_inputs row JSON"
    )

    # --- sink 2: pipeline_runs.trigger_payload --------------------
    # --- sink 3: pipeline_steps.input_envelope --------------------
    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        trigger_payload = conn.execute(
            "SELECT trigger_payload FROM pipeline_runs WHERE id = %s",
            (result["run_id"],),
        ).fetchone()[0]
        step_envelope = conn.execute(
            "SELECT input_envelope FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'webhook_received'",
            (result["run_id"],),
        ).fetchone()[0]
    assert "body" not in trigger_payload
    assert "body" not in step_envelope
    import json as _json
    serialised = _json.dumps(trigger_payload) + _json.dumps(step_envelope)
    assert secret_body not in serialised, (
        "raw body substring leaked into pipeline_runs / pipeline_steps"
    )

    # --- Composer read-path (brief goal #4) -----------------------
    # The just-written row must surface through the real Composer
    # read path under the ``consumed_at IS NULL`` filter. Other
    # tenants in this module's fixture do not produce owner_inputs
    # rows (the flag is False outside this test), so the read is
    # tenant-scoped to exactly one row.
    from uuid import UUID as _UUID

    pending, oi_ok = _build_pending_owner_inputs(_UUID(tenant_id))
    assert oi_ok is True
    assert len(pending) == 1
    composer_row = pending[0]
    assert composer_row.intent == "winback"
    assert composer_row.segment == "dormant_60d"
    assert composer_row.occasion == "diwali"


def test_ingress_resilient_on_classifier_failure(ingress, monkeypatch):
    """Brief goal-item 6: classify_message raises → webhook still ACKs
    200, the rest of the pipeline runs, and NO owner_inputs row is
    written. The writer's outer try/except in
    ``orchestrator/owner_inputs/writer.py:240`` (``run_extraction_for_event``)
    is the contract under test.
    """
    from orchestrator import runner as runner_mod

    def _boom(body, client=None):
        raise RuntimeError("simulated classifier failure")

    monkeypatch.setattr(runner_mod, "OWNER_INPUTS_EXTRACTION_ENABLED", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-sentinel")
    monkeypatch.setattr(
        "orchestrator.owner_inputs.writer.classify_message", _boom
    )

    phone = _phone()
    tenant_id = _new_tenant(ingress.dsn, phone)
    # Substantive owner message → pre_filter routes to brain → final
    # status 'escalated' (VT-3.4 brain not wired). Proves the pipeline
    # ran past the extraction seam even though the classifier raised.
    resp = _post(
        ingress, _fields(phone, Body="I want to plan a Diwali campaign")
    )
    assert resp.status_code == 200
    assert resp.json()["reason"] == "started"
    result = _await_workflow(resp.json()["workflow_id"])

    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM pipeline_runs WHERE id = %s",
            (result["run_id"],),
        ).fetchone()[0]
        webhook_step = conn.execute(
            "SELECT count(*) FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'webhook_received'",
            (result["run_id"],),
        ).fetchone()[0]
        brain_step = conn.execute(
            "SELECT count(*) FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'awaiting_brain'",
            (result["run_id"],),
        ).fetchone()[0]
        owner_inputs_count = conn.execute(
            "SELECT count(*) FROM owner_inputs WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()[0]
    assert status == "escalated"
    assert webhook_step == 1
    assert brain_step == 1
    assert owner_inputs_count == 0
