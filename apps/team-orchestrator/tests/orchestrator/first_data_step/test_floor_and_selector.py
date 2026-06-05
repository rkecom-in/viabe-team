"""VT-267 PR-B — first-data-step machinery canary (Rule #15).

method_selector (fake Anthropic client — no network) + floor state machine (real PG,
synthetic tenant, no mock cursors) + classify intent (Literal + externalised prompt).
CL-422 synthetic. DR-15: real DB for the floor persistence.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("pydantic")


# --- fake Anthropic client ----------------------------------------------------

class _FakeMessages:
    def __init__(self, payload: dict):
        self._payload = payload

    def create(self, **kwargs):
        block = SimpleNamespace(type="text", text=json.dumps(self._payload))
        return SimpleNamespace(content=[block])


class _FakeClient:
    def __init__(self, payload: dict):
        self.messages = _FakeMessages(payload)


# --- method_selector (no network) --------------------------------------------

def test_method_selector_recommends_candidate(monkeypatch):
    monkeypatch.setenv("VIABE_ENV", "test")
    from orchestrator.first_data_step.method_selector import (
        MethodSelectorInput,
        rank_method,
    )

    client = _FakeClient(
        {"recommended_method": "owner_typed", "confidence": 0.8,
         "alternatives": ["contacts", "upi"]}
    )
    out = rank_method(
        MethodSelectorInput(tenant_id=str(uuid4()), business_context="small kirana, no POS"),
        client=client,
    )
    assert out.recommended_method == "owner_typed"
    assert out.alternatives == ["contacts", "upi"]


def test_method_selector_rejects_scrape(monkeypatch):
    monkeypatch.setenv("VIABE_ENV", "test")
    from orchestrator.first_data_step.method_selector import (
        MethodSelectorInput,
        rank_method,
    )

    # model wrongly recommends a scrape method → MUST be rejected.
    client = _FakeClient({"recommended_method": "zomato", "confidence": 0.9, "alternatives": []})
    with pytest.raises(ValueError, match="not in candidates"):
        rank_method(MethodSelectorInput(tenant_id=str(uuid4())), client=client)


def test_method_selector_drops_scrape_alternatives(monkeypatch):
    monkeypatch.setenv("VIABE_ENV", "test")
    from orchestrator.first_data_step.method_selector import (
        MethodSelectorInput,
        rank_method,
    )

    client = _FakeClient(
        {"recommended_method": "upi", "confidence": 0.7, "alternatives": ["gbp", "contacts"]}
    )
    out = rank_method(MethodSelectorInput(tenant_id=str(uuid4())), client=client)
    assert "gbp" not in out.alternatives and "contacts" in out.alternatives


# --- classify intent ----------------------------------------------------------

def test_classify_has_new_intent_and_prompt():
    from orchestrator.agent.tools import classify_owner_message as cm

    # the new intent is in the Literal source of truth
    assert "first_data_step_onboarding" in cm.Classification.__args__
    # output model accepts it
    out = cm.ClassifyOwnerMessageOutput(
        classification="first_data_step_onboarding", confidence=0.9,
        suggested_action="begin floor",
    )
    assert out.classification == "first_data_step_onboarding"
    # externalised prompt loaded + carries the intent + version header (VT-84: now v3.0)
    assert "first_data_step_onboarding" in cm._SYSTEM_PROMPT
    assert "version=3.0" in cm._SYSTEM_PROMPT


# --- floor state machine (real PG) -------------------------------------------

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-267 PR-B floor substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-267 PR-B test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


@_DB
def test_floor_propose_confirm_completes(substrate):
    from orchestrator.first_data_step import floor

    t = _tenant(substrate.dsn)
    assert floor.is_floor_complete(t) is False           # no floor yet → fail-closed
    floor.propose_method(t, "owner_typed", "Shall we start with typed entries?")
    st = floor.get_floor_state(t)
    assert st.state == "propose_confirm" and st.method == "owner_typed" and st.floor_complete is False
    floor.confirm(t)
    assert floor.is_floor_complete(t) is True
    assert floor.get_floor_state(t).state == "confirmed"


@_DB
def test_floor_ghost_after_max_nudges_holds(substrate):
    from orchestrator.first_data_step import floor

    t = _tenant(substrate.dsn)
    floor.propose_method(t, "contacts", "Import your contacts?")
    floor.record_nudge(t)   # 1
    floor.record_nudge(t)   # 2
    st = floor.record_nudge(t)   # 3 → ghost
    assert st.state == "ghost" and st.nudge_count == 3
    assert floor.is_floor_complete(t) is False           # HOLD-safe-minimal


@_DB
def test_floor_nudge_sets_next_timestamp(substrate):
    from orchestrator.first_data_step import floor

    t = _tenant(substrate.dsn)
    floor.propose_method(t, "upi", "Upload your UPI export?")
    st = floor.record_nudge(t)   # 1 (< max) → schedules next
    assert st.last_nudge_at is not None and st.next_nudge_at is not None


@_DB
def test_floor_cross_tenant_isolation(substrate):
    from orchestrator.first_data_step import floor

    t_a = _tenant(substrate.dsn)
    t_b = _tenant(substrate.dsn)
    floor.propose_method(t_a, "owner_typed", "start?")
    floor.confirm(t_a)
    assert floor.is_floor_complete(t_a) is True
    assert floor.is_floor_complete(t_b) is False         # B's floor untouched
    UUID(t_b)
