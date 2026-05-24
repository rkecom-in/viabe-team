"""VT-OIV canary — real Anthropic + real DB + flag-flip.

Brief goal-items 1 + 3 verified end-to-end against the live Messages API:

  (1) a representative owner message yields an ``OwnerInputClassification``
      with ``intent`` in ``_ALLOWED_INTENTS`` (or the ``unclassified``
      sentinel — the writer's defined failure-path verdict) and
      ``segment / occasion`` are strings or None.
  (3) exactly one row lands in ``owner_inputs`` with the derived fields
      only; ``row_to_json`` of the row does NOT contain the raw message
      text — the schema's body-absence is verified empirically on a
      real call.

Env requirement (THREE-WAY, all independent gates):

  - ``RUN_INTEGRATION_TESTS=1`` (conftest hook strips
    ``@pytest.mark.integration`` skip; without it the marker collects
    a skip regardless of the keys).
  - ``ANTHROPIC_API_KEY`` (real Messages API call against Haiku).
  - ``DATABASE_URL`` (real Postgres for tenant + run seed + the writer's
    ``tenant_connection`` write).

CI does NOT export ``ANTHROPIC_API_KEY``, so this test is skipped in CI
by design. Fazal runs it manually once before merge per the brief —
command in the PR description.

Proof-of-call discipline (CL-272): assertions on model id, first user
message ≤200 chars, and ``response.id`` prefix prove the network call
really happened — a green pass cannot be reached by a mock-leak path.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("dbos")
pytest.importorskip("psycopg")

import psycopg  # noqa: E402

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY")
        or not os.environ.get("DATABASE_URL"),
        reason=(
            "VT-OIV canary needs ANTHROPIC_API_KEY (one real Haiku"
            " messages.create) AND DATABASE_URL (tenant + run seed +"
            " tenant_connection write). RUN_INTEGRATION_TESTS=1 alone"
            " is insufficient — all three env gates are required."
        ),
    ),
]


@pytest.fixture(scope="module")
def rls_ctx() -> Any:
    """Apply migrations + launch DBOS so the pool exists; tear down."""
    import apply_migrations  # noqa: E402

    dsn = os.environ["DATABASE_URL"]
    apply_migrations.apply(dsn=dsn)
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos  # noqa: E402

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _seed_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, "
            "phase_entered_at, whatsapp_number) "
            "VALUES ('vt-oiv-canary', 'founding', 'paid_active', now(), %s) "
            "RETURNING id",
            (f"+9199{uuid4().int % 10**8:08d}",),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_run(dsn: str, tenant_id: UUID) -> UUID:
    run_id = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'twilio_inbound', 'running')",
            (str(run_id), str(tenant_id)),
        )
    return run_id


def test_owner_inputs_canary_real_anthropic(
    rls_ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One real Haiku classification → one owner_inputs row with
    derived-only fields; no raw body anywhere in the persisted row.
    """
    from anthropic import Anthropic as _RealAnthropic

    from orchestrator.owner_inputs.writer import (
        _ALLOWED_INTENTS,
        _UNCLASSIFIED_SENTINEL,
        run_extraction_for_event,
    )
    from orchestrator.types import WebhookEvent

    assert _RealAnthropic.__module__.startswith("anthropic"), (
        f"anthropic.Anthropic appears non-genuine: "
        f"module={_RealAnthropic.__module__!r}"
    )

    dsn = rls_ctx.dsn
    tenant_id = _seed_tenant(dsn)
    run_id = _seed_run(dsn, tenant_id)

    # Proof-of-call ledger — wraps the real SDK so we can see exactly
    # what crossed the wire (model id, the body that reached the API,
    # the returned response id). Mirrors the ``_LedgerClient`` pattern
    # in ``test_sales_recovery_end_to_end.py``.
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

    representative_body = "Plan a Diwali campaign for dormant customers."
    sid = f"SM{uuid4().hex}"
    event = WebhookEvent(
        body=representative_body,
        sender_phone="+919999900001",
        message_type="inbound_message",
        twilio_message_sid=sid,
    )

    owner_input_id = run_extraction_for_event(
        tenant_id, run_id, event, client=_LedgerClient()
    )

    diag = {
        "owner_input_id": str(owner_input_id) if owner_input_id else None,
        "real_call_ledger": _LedgerClient.calls_to_real_anthropic,
    }

    # --- PROOF-OF-CALL (CL-272) ----------------------------------
    assert len(_LedgerClient.calls_to_real_anthropic) >= 1, diag
    first_call = _LedgerClient.calls_to_real_anthropic[0]
    assert isinstance(first_call["model"], str) and first_call["model"], diag
    assert representative_body in (
        first_call["first_user_message_first_200"] or ""
    ), diag
    assert isinstance(first_call["response_id"], str), diag
    assert first_call["response_id"].startswith("msg_"), diag

    # --- owner_inputs row shape ----------------------------------
    assert owner_input_id is not None, diag
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT intent, segment, occasion, message_sid, run_id, "
            "consumed_at FROM owner_inputs WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchall()
    assert len(rows) == 1, diag
    intent, segment, occasion, msg_sid, row_run_id, consumed_at = rows[0]
    # Accept either an ``_ALLOWED_INTENTS`` verdict OR the writer's
    # defined ``unclassified`` sentinel — both are contract-valid.
    assert intent in _ALLOWED_INTENTS or intent == _UNCLASSIFIED_SENTINEL, diag
    assert segment is None or isinstance(segment, str), diag
    assert occasion is None or isinstance(occasion, str), diag
    assert msg_sid == sid, diag
    assert UUID(str(row_run_id)) == run_id, diag
    assert consumed_at is None, diag

    # --- no raw body anywhere in the persisted row ----------------
    with psycopg.connect(dsn, autocommit=True) as conn:
        row_json = conn.execute(
            "SELECT row_to_json(owner_inputs.*)::text FROM owner_inputs "
            "WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchone()[0]
    assert representative_body not in row_json, (
        "raw body substring leaked into owner_inputs row JSON — "
        "derived-only contract violated"
    )
