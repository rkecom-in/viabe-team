"""VT-746 — every `TriggerKind` the code can dispatch must be writable by the database.

THE CLASS THIS CLOSES, which has now bitten twice:

- mig 172 (VT-632): the orphan reaper dispatched `orphaned_task` / `dead_letter_task` /
  `silent_terminal`; `tenant_alerts_trigger_kind_check` admitted none of them, so every reaper alert
  INSERT raised `CheckViolation` and the reaper's own observability was dark.
- mig 203 (VT-746): VT-735 declared `fast_budget_exhausted` in the Literal and never widened the
  CHECK, so the Fast-tier budget could exhaust in total silence.

Both recurred for the same reason: **nothing tied the Python Literal to the SQL CHECK.** Alert
dispatch is deliberately fail-soft — an alert failure must never break the caller — which means a
rejected kind is invisible at runtime by design. So the only place this can be caught is a test that
actually writes each kind.

This test is therefore the deliverable of VT-746; the migration is one line.

WHY IT LIVES IN `tests/orchestrator/` AND NOT `test_migrations.py`: importing
`orchestrator.alerts.triggers` pulls `orchestrator.graph.get_pool`, which imports langgraph. The
`migrations` CI job installs only psycopg + pytest, so that import raises ModuleNotFoundError there —
the exact trap that failed VT-723's first attempt twice. The `orchestrator` job has a Postgres service
AND the full dependency set, and its session `_migrated_db` fixture applies migrations.
"""

from __future__ import annotations

from typing import get_args
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("langgraph")  # triggers -> graph -> langgraph

import psycopg  # noqa: E402

from orchestrator.alerts.triggers import TriggerKind, severity_for  # noqa: E402


@pytest.mark.integration
def test_every_trigger_kind_in_the_literal_is_writable(_migrated_db) -> None:
    """INSERT every declared kind. A kind the CHECK refuses is an alert that can never fire."""

    kinds = list(get_args(TriggerKind))
    assert len(kinds) >= 17, f"expected the full TriggerKind set, got {len(kinds)}: {kinds}"
    assert "fast_budget_exhausted" in kinds, "VT-735's Fast-budget tripwire must still be declared"

    rejected: list[tuple[str, str]] = []
    with psycopg.connect(_migrated_db, autocommit=True) as conn:
        tenant_id = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-746 trigger-kind test', 'standard', 'onboarding') RETURNING id"
        ).fetchone()[0]

        for kind in kinds:
            # Each kind in its own savepoint: a CheckViolation aborts the transaction, and we want
            # the COMPLETE list of unwritable kinds, not just the first one.
            try:
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO tenant_alerts "
                        "(tenant_id, trigger_kind, severity, dedup_key, message_text) "
                        "VALUES (%s, %s, %s, %s, 'VT-746 writability probe')",
                        (tenant_id, kind, severity_for(kind), f"vt746:{kind}:{uuid4()}"),
                    )
            except psycopg.errors.CheckViolation as exc:
                rejected.append((kind, str(exc).splitlines()[0]))

        written = conn.execute(
            "SELECT count(DISTINCT trigger_kind) FROM tenant_alerts WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()[0]

    assert not rejected, (
        "these TriggerKind members are declared in Python but REJECTED by "
        "tenant_alerts_trigger_kind_check, so dispatching them writes nothing and pages nobody "
        f"(dispatch is fail-soft, so this is invisible at runtime): {rejected}. "
        "Widen the CHECK in a new migration — additively — in the same PR as the Literal change."
    )
    assert written == len(kinds), (
        f"expected all {len(kinds)} kinds persisted, found {written} distinct"
    )


@pytest.mark.integration
def test_the_fast_budget_path_actually_writes_an_alert(_migrated_db, _dbpool) -> None:
    """VT-746 exit gate (c) — the REAL dispatch path, not a hand-written INSERT.

    The migration and the Literal test above prove the kind is *writable*. This proves the thing the
    row actually cares about: that VT-735's tripwire, when it fires, lands a row somebody can be paged
    on. `_flag_on_vtr` wraps everything in a fail-soft `except` by design — so before mig 203 this
    call swallowed a CheckViolation and returned normally, which is exactly why the defect survived
    from VT-735 until now. A test that only checked "it didn't raise" would have passed all along.
    """
    from orchestrator.llm.fast_budget import _flag_on_vtr

    # `_dbpool` is the graph pool the dispatch path writes through; `_migrated_db` is the DSN
    # this test reads back with. Both are needed: wiring without a reader proves nothing.
    with psycopg.connect(_migrated_db, autocommit=True) as conn:
        tenant_id = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-746 fast-budget dispatch', 'standard', 'onboarding') RETURNING id"
        ).fetchone()[0]

    _flag_on_vtr(tenant_id, used=250, limit=200)

    with psycopg.connect(_migrated_db, autocommit=True) as conn:
        row = conn.execute(
            "SELECT trigger_kind, severity, message_text FROM tenant_alerts "
            "WHERE tenant_id = %s AND trigger_kind = 'fast_budget_exhausted'",
            (tenant_id,),
        ).fetchone()

    assert row is not None, (
        "the Fast-budget tripwire dispatched and NO tenant_alerts row exists — the alert is dark. "
        "`_flag_on_vtr` is fail-soft, so this is exactly how the defect stayed invisible."
    )
    assert row[0] == "fast_budget_exhausted"
    assert "250" in row[2] and "200" in row[2], (
        f"the alert must carry the numbers an operator needs to act: {row[2]!r}"
    )


@pytest.mark.integration
def test_exhausting_the_budget_through_the_real_entry_point_pages_someone(
    _migrated_db, _dbpool
) -> None:
    """VT-746 exit gate (c), airtight: drive `fast_budget_check`, not the dispatch helper.

    The test above proves dispatch→row. This proves condition→dispatch→row through the function the
    product actually calls, by seeding the two things `_read` derives its verdict from: the tenant's
    `max_fast_calls_day` and today's `llm_call_events` rows at `service_tier='fast'`.

    Without this, "the tripwire works" would rest on my reading of two lines in `fast_budget_check` —
    and the entire reason VT-746 exists is that reading code is how the previous gap survived.
    """
    from orchestrator.llm.fast_budget import fast_budget_check

    with psycopg.connect(_migrated_db, autocommit=True) as conn:
        tenant_id = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-746 budget exhaustion', 'standard', 'onboarding') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO tenant_llm_limits (tenant_id, max_fast_calls_day, set_by) "
            "VALUES (%s, 1, 'vt746-test') "
            "ON CONFLICT (tenant_id) DO UPDATE SET max_fast_calls_day = 1",
            (tenant_id,),
        )
        for _ in range(2):  # 2 used against a cap of 1 => exhausted
            conn.execute(
                "INSERT INTO llm_call_events "
                "(tenant_id, agent, call_site, provider, model, service_tier, tokens_in, "
                " tokens_out, cost_usd, request_id, occurred_at) "
                "VALUES (%s, 'manager', 'vt746-test', 'openai', 'test-model', 'fast', 1, 1, 0, %s, "
                " now())",
                (tenant_id, f"vt746-{uuid4()}"),
            )

    allowed = fast_budget_check(tenant_id)

    with psycopg.connect(_migrated_db, autocommit=True) as conn:
        alert = conn.execute(
            "SELECT trigger_kind, message_text FROM tenant_alerts "
            "WHERE tenant_id = %s AND trigger_kind = 'fast_budget_exhausted'",
            (tenant_id,),
        ).fetchone()

    assert allowed is False, "2 fast calls against a cap of 1 must not be allowed"
    assert alert is not None, (
        "the budget was exhausted through the real entry point and no alert row landed — "
        "enforcement without notification is precisely the VT-746 defect"
    )


@pytest.mark.integration
def test_the_check_still_refuses_an_undeclared_kind(_migrated_db) -> None:
    """The constraint must stay a constraint.

    The fix for VT-746 is to WIDEN the CHECK, and the failure mode of a careless widening is dropping
    it altogether — which would trade a silent alert for a silently misspelled one. This asserts the
    CHECK is still enforcing.
    """
    with psycopg.connect(_migrated_db, autocommit=True) as conn:
        tenant_id = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-746 negative test', 'standard', 'onboarding') RETURNING id"
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO tenant_alerts "
                "(tenant_id, trigger_kind, severity, dedup_key, message_text) "
                "VALUES (%s, 'fast_budget_exhaused', 'warning', %s, 'typo on purpose')",
                (tenant_id, f"vt746:typo:{uuid4()}"),
            )
