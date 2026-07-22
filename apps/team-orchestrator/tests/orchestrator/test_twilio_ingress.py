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
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
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
            # owner_inputs=true: these tenants have granted the data-inputs basis,
            # so substantive messages reach the brain (VT-303 consent gate). The
            # consent-OFF path is covered explicitly in test_brain_consent_gate +
            # canary vt303.
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "whatsapp_number, owner_inputs) "
            "VALUES ('VT-3.3 Test', 'founding', 'trial', now(), %s, true) "
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


# --- VT-691: WhatsApp-initiated signup (flag-gated) --------------------------


def test_whatsapp_signup_flag_off_is_byte_identical(ingress, monkeypatch):
    """ENABLE_WHATSAPP_SIGNUP unset/off → the unknown_sender drop is unchanged (the flag is
    the ONLY gate between today's behavior and the signup flow)."""
    monkeypatch.delenv("ENABLE_WHATSAPP_SIGNUP", raising=False)
    resp = _post(ingress, _fields(_phone()))
    assert resp.status_code == 200
    assert resp.json() == {"workflow_id": None, "reason": "unknown_sender"}


def test_whatsapp_signup_flag_on_starts_workflow_and_prompts_consent(ingress, monkeypatch):
    """Flag ON → an unknown inbound starts wa_signup_{sid}, the session row lands
    'consent_pending', the consent prompt goes out (send patched), and NO tenant is created
    (DPDP: tenant only after an explicit consent reply). A Twilio redelivery of the same sid
    reports dupe/recovering — never a second flow."""
    import orchestrator.onboarding.whatsapp_signup as ws

    monkeypatch.setenv("ENABLE_WHATSAPP_SIGNUP", "true")
    # Fazal 2026-07-22: the consent ask is the interactive I-agree/I-do-not-agree quick-reply.
    # Capture the interactive send (registry-resolved HX) and keep the freeform fallback
    # captured too — exactly one of the two must fire.
    import orchestrator.utils.twilio_send as ts

    sent: list[str] = []
    monkeypatch.setattr(
        ts, "send_interactive_message",
        lambda content_sid, phone, **k: (sent.append(f"interactive:{content_sid}"), "MKDEVtest")[1],
    )
    monkeypatch.setattr(ws, "_send", lambda p, text: sent.append(text))

    phone = _phone()
    fields = _fields(phone)
    resp = _post(ingress, fields)
    assert resp.status_code == 200
    body = resp.json()
    assert body["reason"] == "started"
    assert str(body["workflow_id"]).startswith("wa_signup_")

    # The workflow runs async — poll for the durable session row.
    deadline = time.time() + 20
    row = None
    while time.time() < deadline:
        with psycopg.connect(ingress.dsn, autocommit=True) as conn:
            row = conn.execute(
                "SELECT status, consent_prompt_count FROM whatsapp_signup_sessions "
                "WHERE phone_e164 = %s",
                (phone,),
            ).fetchone()
        if row is not None:
            break
        time.sleep(0.5)
    assert row is not None, "the signup session row must be durably created"
    assert row[0] == "consent_pending"
    from orchestrator.templates_registry import content_sid_for

    expected_sid = content_sid_for(ws.INTERACTIVE_CONSENT_TEMPLATE, "en")
    assert sent == [f"interactive:{expected_sid}"], sent

    # DPDP: no tenant yet.
    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        t = conn.execute(
            "SELECT 1 FROM tenants WHERE whatsapp_number = %s", (phone,)
        ).fetchone()
    assert t is None, "a cold inbound must NEVER create a tenant"

    # Same-sid redelivery: idempotent (no second workflow, no second prompt).
    resp2 = _post(ingress, fields)
    assert resp2.json()["reason"] in ("dupe", "recovering")


# --- VT-416 PR-3: whatsapp_number ambiguity guard (fail-closed) --------------
#
# The canonical guarantee is the DB constraint: migration 066's
# tenants_whatsapp_number_key (partial UNIQUE on whatsapp_number, VT-267 / Fazal
# D1) makes a duplicate number un-insertable, so the two-match case is
# schema-impossible via normal inserts (an INSERT of a second tenant on the same
# number raises UniqueViolation). The _lookup_tenant guard is DEFENCE-IN-DEPTH
# for a hypothetical future regression that drops the index. We therefore prove
# the guard's branch by stubbing the pool to return two rows (the only way to
# reach the ambiguity path without violating the live constraint), and keep a
# real single-match baseline against the DB.


class _FakeCursorResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_args, **_kwargs):
        return _FakeCursorResult(self._rows)


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    def connection(self):
        return _FakeConn(self._rows)


def test_single_tenant_match_routes_correctly(ingress):
    """Sanity baseline for the ambiguity guard: exactly one tenant for the
    number → the happy path is unchanged (workflow starts, reason 'started').
    Runs against the real DB (single insert is allowed by the unique index)."""
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    resp = _post(ingress, _fields(phone, Body="hello"))
    assert resp.status_code == 200
    assert resp.json()["reason"] == "started"
    _await_workflow(resp.json()["workflow_id"])


def test_lookup_tenant_single_match_returns_id(monkeypatch):
    """Defence-in-depth unit test: exactly one row → that tenant id (happy path
    unchanged). Pool stubbed so this needs no DB."""
    import orchestrator.api.twilio_ingress as ti

    monkeypatch.setattr(ti, "get_pool", lambda: _FakePool([{"id": "tenant-xyz"}]))
    assert ti._lookup_tenant("+919999900001") == "tenant-xyz"


def test_lookup_tenant_returns_none_on_ambiguity(monkeypatch, caplog):
    """Defence-in-depth unit test: if the lookup ever returns >1 row (a future
    schema regression dropping tenants_whatsapp_number_key), _lookup_tenant
    fails CLOSED — returns None and logs an error, NOT a silent newest-wins
    pick. Pool stubbed to force the (otherwise schema-impossible) two-match."""
    import logging

    import orchestrator.api.twilio_ingress as ti

    monkeypatch.setattr(
        ti,
        "get_pool",
        lambda: _FakePool([{"id": "tenant-a"}, {"id": "tenant-b"}]),
    )
    with caplog.at_level(logging.ERROR, logger="orchestrator.api.twilio_ingress"):
        result = ti._lookup_tenant("+919999900002")
    assert result is None, "ambiguous match must NOT resolve to a tenant"
    assert any("ambiguous whatsapp_number" in r.message for r in caplog.records)


def test_lookup_tenant_no_match_returns_none(monkeypatch):
    """No row → None (unmatched path), unchanged. Pool stubbed; no DB."""
    import orchestrator.api.twilio_ingress as ti

    monkeypatch.setattr(ti, "get_pool", lambda: _FakePool([]))
    assert ti._lookup_tenant("+919999900003") is None


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
    (Pillar 7) — with an 'agent_invocation' step carrying the pre-filter reason."""
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    resp = _post(ingress, _fields(phone, Body="I want to plan a campaign"))
    result = _await_workflow(resp.json()["workflow_id"])
    assert result["routed"] == "brain"

    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM pipeline_runs WHERE id = %s", (result["run_id"],)
        ).fetchone()[0]
        # VT-464 D4: the dispatch reason lives in the validated input_envelope
        # (AgentInvocationInput.reason) — output_envelope is None per schema.
        step = conn.execute(
            "SELECT input_envelope, error FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'agent_invocation'",
            (result["run_id"],),
        ).fetchone()
    assert status == "escalated"
    assert step is not None, "no agent_invocation step record written"
    assert "owner message" in step[0]["reason"]
    assert step[0]["agent_role"] == "orchestrator"
    # VT-464 D4: the envelope must validate — no payload_validation_failed flag.
    assert not (step[1] or {}).get("payload_validation_failed"), (
        f"agent_invocation envelope failed schema validation: {step[1]!r}"
    )


# --- VT-303: owner_inputs consent gate on the brain transmit (Option B) -------


def _new_tenant_no_consent(dsn: str, whatsapp_number: str) -> str:
    """Seed a tenant WITHOUT owner_inputs consent (the gate's FALSE path)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "whatsapp_number, owner_inputs) "
            "VALUES ('VT-303 NoConsent', 'founding', 'trial', now(), %s, false) "
            "RETURNING id",
            (whatsapp_number,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_no_owner_inputs_consent_routes_to_consent_required(ingress):
    """A substantive owner message from a tenant WITHOUT owner_inputs consent is
    NOT transmitted to the brain — it degrades to consent_required (Option B).
    No agent_invocation step is written → no Anthropic transmit (CL-425)."""
    phone = _phone()
    _new_tenant_no_consent(ingress.dsn, phone)
    resp = _post(ingress, _fields(phone, Body="I want to plan a campaign"))
    result = _await_workflow(resp.json()["workflow_id"])

    assert result["routed"] == "consent_required"
    assert result["handler"] == "consent_required_handler"
    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        brain_steps = conn.execute(
            "SELECT count(*) FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'agent_invocation'",
            (result["run_id"],),
        ).fetchone()[0]
    assert brain_steps == 0, "brain was invoked despite no owner_inputs consent"


def test_enable_keyword_grants_consent_then_brain_runs(ingress):
    """The owner sends the enable phrase → owner_inputs flips true → a
    subsequent substantive message then reaches the brain (full enable loop)."""
    phone = _phone()
    tenant_id = _new_tenant_no_consent(ingress.dsn, phone)

    # 1. Enable phrase routes to the enable handler and flips owner_inputs.
    r_enable = _await_workflow(
        _post(ingress, _fields(phone, Body="ACTIVATE TEAM")).json()["workflow_id"]
    )
    assert r_enable["routed"] == "direct_handler"
    assert r_enable["handler"] == "data_inputs_enable_handler"
    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        owner_inputs = conn.execute(
            "SELECT owner_inputs FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()[0]
    assert owner_inputs is True

    # 2. With consent granted, a substantive message reaches the brain.
    r_brain = _await_workflow(
        _post(ingress, _fields(phone, Body="plan a diwali campaign")).json()[
            "workflow_id"
        ]
    )
    assert r_brain["routed"] == "brain"


def test_status_callback_delivered_completes_clean(ingress):
    """A 'delivered' status callback routes to the deterministic delivery reconciler
    (VT-564 — was a bare Reject pre-batch) — the run still completes cleanly with
    status 'completed' and NO agent_invocation record (zero LLM on machine events)."""
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    resp = _post(ingress, _fields(phone, MessageStatus="delivered"))
    result = _await_workflow(resp.json()["workflow_id"])
    assert result["routed"] == "direct_handler"
    assert result["handler"] == "customer_send_delivery_handler"

    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM pipeline_runs WHERE id = %s", (result["run_id"],)
        ).fetchone()[0]
        brain_steps = conn.execute(
            "SELECT count(*) FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'agent_invocation'",
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


# --- VT-416 PR-3: per-tenant preferred_language WIRED into state (end-to-end) -
#
# PR-3 made the composer READ state['preferred_language'], but nothing populated
# that key at runtime, so the fix was latent — every tenant hit the global
# default and a Hindi-preference owner still got English. These tests prove the
# wiring is LIVE: the runner's _load_preferred_language reads the real tenant
# row, the runner threads it into SubscriberState exactly as webhook_pipeline_run
# does, and the real compose_owner_output then renders the Hindi variant. The
# proof is END-TO-END through the live node — NOT a hand-set state key.


def _new_tenant_with_language(
    dsn: str, whatsapp_number: str, *, preferred_language: str
) -> str:
    """Seed a tenant carrying an explicit tenants.preferred_language value."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "whatsapp_number, owner_inputs, preferred_language) "
            "VALUES ('VT-416 Lang Test', 'founding', 'trial', now(), %s, true, %s) "
            "RETURNING id",
            (whatsapp_number, preferred_language),
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_load_preferred_language_reads_explicit_hindi_choice(ingress):
    """The live runner node reads tenants.preferred_language='hi' for a real
    tenant — the column that the composer's per-tenant resolver consumes."""
    from orchestrator.runner import _load_preferred_language

    tenant_id = _new_tenant_with_language(
        ingress.dsn, _phone(), preferred_language="hi"
    )
    assert _load_preferred_language(tenant_id) == "hi"


def test_load_preferred_language_falls_back_to_language_preference(ingress):
    """When preferred_language is NULL, the read falls back to the NOT-NULL
    language_preference column (mirrors get_business_profile's locale rule)."""
    from orchestrator.runner import _load_preferred_language

    # _new_tenant inserts no preferred_language → column is NULL → fallback to
    # language_preference (DEFAULT 'en').
    tenant_id = _new_tenant(ingress.dsn, _phone())
    assert _load_preferred_language(tenant_id) == "en"


def test_load_preferred_language_none_for_missing_tenant(ingress):
    """A tenant id with no row returns None (best-effort) — the composer then
    uses its global default. Never raises (dispatch must not break)."""
    from orchestrator.runner import _load_preferred_language

    assert _load_preferred_language(str(uuid4())) is None


def test_hindi_tenant_state_renders_hindi_variant_end_to_end(ingress):
    """END-TO-END proof of the PR-3 wiring: a Hindi-preference tenant →
    _load_preferred_language populates SubscriberState exactly as the runner
    does → the real compose_owner_output returns the Hindi variant.

    This is the load-bearing test: it does NOT hand-set the state key. It runs
    the LIVE runner read against the real tenant row, threads it into state via
    new_subscriber_state + the same assignment webhook_pipeline_run performs,
    then exercises the real composer. Before the wiring this returned 'en'.
    """
    from datetime import datetime, timezone
    from uuid import UUID, uuid4

    from orchestrator.output_composer import compose_owner_output
    from orchestrator.runner import _load_preferred_language
    from orchestrator.state import new_subscriber_state

    tenant_id = _new_tenant_with_language(
        ingress.dsn, _phone(), preferred_language="hi"
    )

    # Build state the way webhook_pipeline_run does (live node populates the key).
    state = new_subscriber_state(UUID(tenant_id), uuid4())
    state["preferred_language"] = _load_preferred_language(tenant_id)
    assert state["preferred_language"] == "hi", (
        "live node did not populate the per-tenant language into state"
    )

    # Real composer (welcome flow, template path) renders the Hindi variant.
    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    out = compose_owner_output(None, state, "welcome", now=now)
    assert out.preferred_language == "hi", (
        "Hindi-owner bug NOT closed: composer rendered English for a hi tenant"
    )


def test_english_tenant_state_renders_english_variant_end_to_end(ingress):
    """Sibling of the Hindi proof — an 'en' tenant resolves to the English
    variant through the same live path (no accidental Hindi spillover)."""
    from datetime import datetime, timezone
    from uuid import UUID, uuid4

    from orchestrator.output_composer import compose_owner_output
    from orchestrator.runner import _load_preferred_language
    from orchestrator.state import new_subscriber_state

    tenant_id = _new_tenant_with_language(
        ingress.dsn, _phone(), preferred_language="en"
    )
    state = new_subscriber_state(UUID(tenant_id), uuid4())
    state["preferred_language"] = _load_preferred_language(tenant_id)
    assert state["preferred_language"] == "en"

    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    out = compose_owner_output(None, state, "welcome", now=now)
    assert out.preferred_language == "en"


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
            "WHERE run_id = %s AND step_kind = 'agent_invocation'",
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


# ---------------------------------------------------------------------------
# VT-567 — live WhatsApp inbound is channel-prefixed ('whatsapp:+91…'); the
# ingress must normalize to plain E.164 for lookups AND the workflow fields.
# Live-drill root cause 2026-07-02: without this, every real inbound was
# reason='unknown_sender' → 200-and-drop.
# ---------------------------------------------------------------------------

def test_normalize_wa_fields_strips_prefix_pure() -> None:
    from orchestrator.api.twilio_ingress import _normalize_wa_fields

    raw = {"From": "whatsapp:+919800000001", "To": "whatsapp:+910000000000",
           "WaId": "919800000001", "Body": "Hi", "MessageSid": "SMx"}
    out = _normalize_wa_fields(raw)
    assert out["From"] == "+919800000001"
    assert out["To"] == "+910000000000"
    assert out["Body"] == "Hi" and out["MessageSid"] == "SMx"
    # plain payload passes byte-identical
    plain = {"From": "+919800000001", "To": "+910000000000"}
    assert _normalize_wa_fields(plain) == plain


def test_whatsapp_prefixed_inbound_resolves_tenant_and_runs(ingress):
    """A REAL-format inbound (From='whatsapp:+91…') must resolve the tenant whose
    whatsapp_number is stored plain-E.164 and start the webhook workflow — the exact
    live-drill failure (COMPLETE_SETUP tap dropped as unknown_sender)."""
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    resp = _post(ingress, _fields(f"whatsapp:{phone}", Body="Complete Setup"))
    body = resp.json()
    assert body.get("reason") != "unknown_sender", body
    assert body.get("workflow_id"), body
    result = _await_workflow(body["workflow_id"])
    assert result["run_id"]
    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM pipeline_runs WHERE id = %s", (result["run_id"],)
        ).fetchone()[0]
    assert status in ("completed", "escalated")


# ---------------------------------------------------------------------------
# VT-582 — the DEV-ONLY ingress secret (DEV_TEST_INGRESS_SECRET) the conversation
# harness authenticates the deployed dev orchestrator with. Accepted ONLY on a
# positively-dev env (EXPECTED_ENV in {dev,development}); on prod / unset it is
# ignored (CL-431 prod gate). A dev-secret-authenticated request must behave
# IDENTICALLY downstream to a prod-secret one (same tenant resolve + workflow start).
# ---------------------------------------------------------------------------

_DEV_INGRESS_SECRET = "vt-582-harness-dev-ingress-secret"


def test_dev_ingress_secret_accepted_on_dev_starts_workflow(ingress, monkeypatch):
    """EXPECTED_ENV=dev + the dev secret → the request authenticates and starts the SAME workflow a
    prod-secret request would (behaves identically downstream)."""
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv("DEV_TEST_INGRESS_SECRET", _DEV_INGRESS_SECRET)
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    resp = _post(ingress, _fields(phone, Body="hello from the harness"), secret=_DEV_INGRESS_SECRET)
    assert resp.status_code == 200
    assert resp.json()["reason"] == "started"
    result = _await_workflow(resp.json()["workflow_id"])
    assert result["run_id"]


def test_dev_ingress_secret_rejected_on_prod(ingress, monkeypatch):
    """EXPECTED_ENV=prod → the dev secret is NOT accepted (403), even though it is configured."""
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    monkeypatch.setenv("DEV_TEST_INGRESS_SECRET", _DEV_INGRESS_SECRET)
    resp = _post(ingress, _fields(_phone()), secret=_DEV_INGRESS_SECRET)
    assert resp.status_code == 403


def test_dev_ingress_secret_rejected_when_env_unset(ingress, monkeypatch):
    """No EXPECTED_ENV (fail-closed) → the dev secret is inert (403)."""
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    monkeypatch.setenv("DEV_TEST_INGRESS_SECRET", _DEV_INGRESS_SECRET)
    resp = _post(ingress, _fields(_phone()), secret=_DEV_INGRESS_SECRET)
    assert resp.status_code == 403


def test_prod_secret_still_accepted_when_dev_ingress_enabled(ingress, monkeypatch):
    """The prod INTERNAL_API_SECRET path is unchanged even on a dev env with the dev secret set."""
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv("DEV_TEST_INGRESS_SECRET", _DEV_INGRESS_SECRET)
    phone = _phone()
    _new_tenant(ingress.dsn, phone)
    resp = _post(ingress, _fields(phone, Body="hello"), secret=_SECRET)  # the prod secret
    assert resp.status_code == 200
    assert resp.json()["reason"] == "started"
    _await_workflow(resp.json()["workflow_id"])


def test_whatsapp_signup_never_solicits_a_rate_limited_tenants_customer(ingress, monkeypatch):
    """Adversarial-verify finding B: a message TO a live business WABA whose tenant is
    rate-limited must fall through to the silent drop — NEVER into a Viabe signup
    solicitation of that business's customer (flag ON)."""
    import orchestrator.onboarding.whatsapp_signup as ws

    monkeypatch.setenv("ENABLE_WHATSAPP_SIGNUP", "true")
    monkeypatch.setattr(
        ws, "_send", lambda p, text: pytest.fail("a third party's customer must never be solicited")
    )

    waba_number = _phone()
    tenant_id = _new_tenant(ingress.dsn, _phone())
    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_whatsapp_accounts (tenant_id, phone_number, status) "
            "VALUES (%s, %s, 'live')",
            (tenant_id, waba_number),
        )
        # Saturate this tenant's per-minute bucket so the customer-inbound branch declines.
        conn.execute(
            "INSERT INTO rate_limit_buckets (tenant_id, window_start, count) "
            "VALUES (%s, date_trunc('minute', now()), 30) "
            "ON CONFLICT (tenant_id, window_start) DO UPDATE SET count = 30",
            (tenant_id,),
        )

    customer = _phone()  # unknown number, messaging the BUSINESS's WABA
    resp = _post(ingress, _fields(customer, To=waba_number))
    assert resp.status_code == 200
    body = resp.json()
    assert body["reason"] == "unknown_sender", body
    assert body["workflow_id"] is None

    with psycopg.connect(ingress.dsn, autocommit=True) as conn:
        s = conn.execute(
            "SELECT 1 FROM whatsapp_signup_sessions WHERE phone_e164 = %s", (customer,)
        ).fetchone()
    assert s is None, "no signup session may be opened for a business's customer"
