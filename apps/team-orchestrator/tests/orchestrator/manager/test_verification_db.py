"""VT-606 (team-lead ruling round 2) — ``verification.verify_completion`` end-to-end (live
Postgres for the task/steps read; the opus call is mocked — no real Anthropic call).
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("anthropic")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — verify_completion DB tests skipped",
)


@pytest.fixture(scope="module")
def pool():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _seed_tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"ver-{tid[:8]}"),
        )
    return tid


def _create_and_claim_and_complete(pool, tid: str, *, criteria: list[str], evidence_kind: str | None):
    from orchestrator.manager import plan_store, task_store
    from orchestrator.manager.plan_models import EvidenceRef, ManagerPlan, PlanStep

    plan = ManagerPlan(
        objective="win back lapsed customers",
        acceptance_criteria=["3+ customers recovered"],
        steps=[PlanStep(step_seq=1, kind="verification", acceptance_criteria=criteria)],
    )
    task_id = plan_store.create_plan(tid, plan, source_message_sid=f"SM{uuid4().hex}")
    step = plan_store.claim_next_step(tid, task_id)
    evidence = EvidenceRef(kind=evidence_kind, ref="ref-1") if evidence_kind else None
    plan_store.complete_step(tid, step["step_id"], "done", evidence=evidence, expected_from=("running",))
    task_store.set_task_status(tid, task_id, "verifying", expected_from=("running",))
    return task_id


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResp:
    def __init__(self, content: list) -> None:
        self.content = content


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    @property
    def messages(self):
        payload = self._payload

        class _M:
            @staticmethod
            def create(**kwargs):  # noqa: ANN003, ANN201
                return _FakeResp([_FakeTextBlock(json.dumps(payload))])

        return _M()


def test_deterministic_floor_blocks_before_any_llm_call(pool):
    """A step declared acceptance criteria but recorded ZERO evidence — the floor must reject this
    WITHOUT ever calling the (deliberately absent) client."""
    from orchestrator.manager.verification import verify_completion

    tid = _seed_tenant(pool)
    task_id = _create_and_claim_and_complete(pool, tid, criteria=["3+ recovered"], evidence_kind=None)

    result = verify_completion(tid, task_id, client=None)  # would raise if it ever reached a real call
    assert result.verdict == "not_verified"
    assert "step_seq=1" in result.reason


def test_verified_when_floor_passes_and_llm_agrees(pool):
    from orchestrator.manager.verification import verify_completion

    tid = _seed_tenant(pool)
    task_id = _create_and_claim_and_complete(
        pool, tid, criteria=["3+ recovered"], evidence_kind="campaign_plan"
    )

    result = verify_completion(
        tid, task_id, client=_FakeClient({"verdict": "verified", "reason": "evidence supports it"})
    )
    assert result.verdict == "verified"


def test_not_verified_when_llm_disagrees(pool):
    from orchestrator.manager.verification import verify_completion

    tid = _seed_tenant(pool)
    task_id = _create_and_claim_and_complete(
        pool, tid, criteria=["3+ recovered"], evidence_kind="campaign_plan"
    )

    result = verify_completion(
        tid, task_id,
        client=_FakeClient({"verdict": "not_verified", "reason": "evidence only shows 1 recovered"}),
    )
    assert result.verdict == "not_verified"


def test_fail_closed_on_malformed_llm_response(pool):
    """A garbled/non-JSON verification response must never crash or fabricate 'verified'."""
    from orchestrator.manager.verification import verify_completion

    tid = _seed_tenant(pool)
    task_id = _create_and_claim_and_complete(
        pool, tid, criteria=["3+ recovered"], evidence_kind="campaign_plan"
    )

    class _BrokenClient:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kwargs):  # noqa: ANN003, ANN201
                return _FakeResp([_FakeTextBlock("not json")])

    result = verify_completion(tid, task_id, client=_BrokenClient())
    assert result.verdict == "not_verified"
    assert "verification_extraction_failed" in result.reason


def test_fail_closed_on_client_exception(pool):
    """A raised network/API error (not just a parse mismatch) must ALSO fail closed — never a
    crash that would leave the workflow's own step call unhandled."""
    from orchestrator.manager.verification import verify_completion

    tid = _seed_tenant(pool)
    task_id = _create_and_claim_and_complete(
        pool, tid, criteria=["3+ recovered"], evidence_kind="campaign_plan"
    )

    class _RaisingClient:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kwargs):  # noqa: ANN003, ANN201
                raise RuntimeError("network down")

    result = verify_completion(tid, task_id, client=_RaisingClient())
    assert result.verdict == "not_verified"
    assert "verification_extraction_failed" in result.reason


def test_no_criteria_declared_still_reaches_the_llm(pool):
    """The floor is a NO-OP (never blocks) when no step declared acceptance criteria at all —
    verification still runs the judgment call rather than trivially auto-passing."""
    from orchestrator.manager.verification import verify_completion

    tid = _seed_tenant(pool)
    task_id = _create_and_claim_and_complete(pool, tid, criteria=[], evidence_kind=None)

    result = verify_completion(
        tid, task_id, client=_FakeClient({"verdict": "verified", "reason": "no criteria declared"})
    )
    assert result.verdict == "verified"
