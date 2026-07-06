"""VT-603 — integration_agent tools derive tenant from the run context, never the model.

VT-608 (Loop Package 5) update: the tool surface was replaced (start_connector_setup ->
start_oauth; propose_field_mapping_stub/confirm_field_mapping_stub -> propose_mapping/
confirm_mapping (de-stubbed, real VT-209 reasoner); setup_recurring_ingestion_stub ->
schedule_recurring_pull). This file re-proves the SAME VT-603 tenancy invariant against the new
names for every tool that can be exercised without a real Postgres connection (no DB, no live
Anthropic key needed for the heuristic path). The DB-touching tools
(read_integration_state/check_oauth_status/confirm_mapping/commit_ingestion/verify_connector,
which route through ``onboarding.shopify_onboarding``'s module-top-bound ``tenant_connection``)
get their tenancy + persistence proof in ``test_integration_agent_tools_realdb.py`` instead,
mirroring the codebase's own established mock-vs-real-DB test split.

Mirrors ``test_marketing_lane_tenant_scope.py`` (VT-599): every tool calls
``resolve_lane_tenant`` first — the ambient dispatch ``ObservabilityContext`` is ALWAYS
authoritative; a model value that disagrees (a business name, a foreign UUID) is observed +
logged (mismatch WARNING) but never trusted; no context + an unparseable model value returns the
structured ``lane_tenant_error`` dict, never a raise. ``schedule_recurring_pull`` gets an
ADDITIONAL adversarial test proving the write lands on the CONTEXT tenant via the RLS-scoped
``tenant_connection`` seam — never on a model-supplied foreign tenant, and never through the raw
``get_pool()`` bypass (the same WRITE this suite has always guarded, VT-268/VT-603 unchanged).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")

from orchestrator.observability.decorators import observability_context  # noqa: E402

_LOGGER_NAME = "orchestrator.agent.lane_tenant"


# --- scenario helper (mirrors test_marketing_lane_tenant_scope.py) ----------------------------


def _assert_context_wins_no_raise(
    caplog: pytest.LogCaptureFixture,
    *,
    call: Any,
    tool_name: str,
) -> Any:
    """Runs ``call`` (a zero-arg closure invoking the tool) inside a caplog scope; returns the
    tool's result. Asserts exactly one mismatch warning naming ``tool_name`` was logged."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = call()
    mismatches = [r for r in caplog.records if tool_name in r.getMessage()]
    assert len(mismatches) == 1, caplog.text
    assert "mismatch" in mismatches[0].getMessage().lower()
    return result


# --- (1) start_oauth ---------------------------------------------------------------------------


def test_start_oauth_business_name_from_model_uses_context_tenant(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from orchestrator.agent.integration_agent import start_oauth

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: start_oauth.func(  # type: ignore[attr-defined]
                connector_id="unsupported_platform", tenant_id="Sundaram Stores"
            ),
            tool_name="start_oauth",
        )
    assert out["status"] == "unsupported"


def test_start_oauth_foreign_uuid_from_model_overridden(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A syntactically-valid but WRONG tenant UUID is overridden — proven via the Shopify path
    (the one branch that forwards ``tenant_id`` to a downstream call we can observe)."""
    import orchestrator.onboarding.shopify_onboarding as shopify_onboarding_mod
    from orchestrator.agent.integration_agent import start_oauth

    seen: dict[str, Any] = {}

    def _fake_start_shopify_setup(tenant_id: Any, shop: str, **kwargs: Any) -> dict[str, str]:
        seen["tenant_id"] = tenant_id
        return {"authorize_url": "https://example.test/authorize"}

    monkeypatch.setattr(shopify_onboarding_mod, "start_shopify_setup", _fake_start_shopify_setup)

    run_id, tenant_id, foreign = uuid4(), uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: start_oauth.func(  # type: ignore[attr-defined]
                connector_id="shopify", tenant_id=str(foreign), shop="teststore.myshopify.com"
            ),
            tool_name="start_oauth",
        )
    assert out["authorize_url"] == "https://example.test/authorize"
    assert seen["tenant_id"] == str(tenant_id)
    assert seen["tenant_id"] != str(foreign)


def test_start_oauth_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.integration_agent import start_oauth

    out = start_oauth.func(  # type: ignore[attr-defined]
        connector_id="unsupported_platform", tenant_id="Sundaram Stores"
    )
    assert out == {
        "status": "error",
        "error": "start_oauth: no resolvable tenant context",
    }


# --- (2) pull_sample ------------------------------------------------------------------------


def test_pull_sample_business_name_from_model_uses_context_tenant(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from orchestrator.agent.integration_agent import pull_sample

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: pull_sample.func(  # type: ignore[attr-defined]
                tenant_id="Sundaram Stores", connector_id="unsupported_platform"
            ),
            tool_name="pull_sample",
        )
    assert out["status"] == "unsupported"


def test_pull_sample_foreign_uuid_from_model_overridden(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The Shopify branch forwards the tenant into ``ShopifyConnector.pull_sample`` — proves the
    CONTEXT tenant reaches it, never a model-supplied foreign UUID."""
    import orchestrator.integrations.connectors.shopify as shopify_mod
    from orchestrator.agent.integration_agent import pull_sample

    seen: dict[str, Any] = {}

    class _FakeShopifyConnector:
        def pull_sample(self, tenant_id: Any) -> list[dict[str, Any]]:
            seen["tenant_id"] = tenant_id
            return []

    monkeypatch.setattr(shopify_mod, "ShopifyConnector", _FakeShopifyConnector)

    run_id, tenant_id, foreign = uuid4(), uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: pull_sample.func(  # type: ignore[attr-defined]
                tenant_id=str(foreign), connector_id="shopify"
            ),
            tool_name="pull_sample",
        )
    assert out["row_count"] == 0
    assert seen["tenant_id"] == tenant_id
    assert seen["tenant_id"] != foreign


def test_pull_sample_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.integration_agent import pull_sample

    out = pull_sample.func(  # type: ignore[attr-defined]
        tenant_id="Sundaram Stores", connector_id="unsupported_platform"
    )
    assert out == {"status": "error", "error": "pull_sample: no resolvable tenant context"}


# --- (3) propose_mapping — the real VT-209 reasoner, heuristic-only path, no DB ----------------


def test_propose_mapping_business_name_from_model_uses_context_tenant(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from orchestrator.agent.integration_agent import propose_mapping

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: propose_mapping.func(  # type: ignore[attr-defined]
                tenant_id="Sundaram Stores", connector_id="google_sheet", source_fields=["phone"]
            ),
            tool_name="propose_mapping",
        )
    # "phone" exact-matches a GLOBAL_FIELD_HINT alias — confidence 1.0, no LLM fallback reached.
    assert out["proposals"] == [
        {
            "source_field": "phone",
            "canonical_field": "phone",
            "confidence": 1.0,
            "decided_by": "heuristic",
            "routing": "commit_silently",
        }
    ]


def test_propose_mapping_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.integration_agent import propose_mapping

    out = propose_mapping.func(  # type: ignore[attr-defined]
        tenant_id="Sundaram Stores", connector_id="google_sheet", source_fields=["phone"]
    )
    assert out == {
        "status": "error",
        "error": "propose_mapping: no resolvable tenant context",
    }


# --- (4) schedule_recurring_pull — THE WORST offender's rename: the write itself ---------------


def _fake_tenant_connection_factory(seen: dict[str, Any]) -> Any:
    @contextmanager
    def _fake_tenant_connection(tenant_id: Any, *, pool: Any = None) -> Any:
        seen["tenant_arg"] = tenant_id

        class _FakeConn:
            def execute(self, sql: str, params: Any) -> None:
                seen["insert_params"] = params

        yield _FakeConn()

    return _fake_tenant_connection


def test_schedule_recurring_pull_business_name_from_model_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.db as db_mod
    from orchestrator.agent.integration_agent import schedule_recurring_pull

    seen: dict[str, Any] = {}
    monkeypatch.setattr(db_mod, "tenant_connection", _fake_tenant_connection_factory(seen))

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: schedule_recurring_pull.func(  # type: ignore[attr-defined]
                tenant_id="Sundaram Stores", connector_id="shopify", cadence="0 9 * * *"
            ),
            tool_name="schedule_recurring_pull",
        )
    assert out["scheduled"] == "true"
    assert seen["tenant_arg"] == tenant_id
    assert seen["insert_params"][0] == str(tenant_id)


def test_schedule_recurring_pull_no_context_garbage_value_returns_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No context + a garbage model value -> structured error; the DB seam is NEVER touched."""
    import orchestrator.db as db_mod
    from orchestrator.agent.integration_agent import schedule_recurring_pull

    def _forbidden_tenant_connection(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("tenant_connection must not be reached with no resolvable tenant")

    monkeypatch.setattr(db_mod, "tenant_connection", _forbidden_tenant_connection)

    out = schedule_recurring_pull.func(  # type: ignore[attr-defined]
        tenant_id="Sundaram Stores", connector_id="shopify", cadence="0 9 * * *"
    )
    assert out == {
        "status": "error",
        "error": "schedule_recurring_pull: no resolvable tenant context",
    }


def test_schedule_recurring_pull_adversarial_write_never_lands_on_foreign_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """THE adversarial test: ambient context tenant A; the MODEL supplies a foreign (syntactically
    valid) tenant B. The write MUST land on A — never on B — and MUST go through the RLS-scoped
    ``tenant_connection`` seam, never the raw BYPASSRLS pool (``orchestrator.graph.get_pool``)."""
    import orchestrator.db as db_mod
    import orchestrator.graph as graph_mod
    from orchestrator.agent.integration_agent import schedule_recurring_pull

    seen: dict[str, Any] = {}
    monkeypatch.setattr(db_mod, "tenant_connection", _fake_tenant_connection_factory(seen))

    def _forbidden_get_pool() -> Any:
        raise AssertionError(
            "schedule_recurring_pull must never use the raw BYPASSRLS pool"
        )

    monkeypatch.setattr(graph_mod, "get_pool", _forbidden_get_pool)

    run_id, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_a):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: schedule_recurring_pull.func(  # type: ignore[attr-defined]
                tenant_id=str(tenant_b), connector_id="shopify", cadence="0 9 * * *"
            ),
            tool_name="schedule_recurring_pull",
        )
    assert out["scheduled"] == "true"
    # the RLS-scoped connection was opened for the CONTEXT tenant, never the foreign one
    assert seen["tenant_arg"] == tenant_a
    assert seen["tenant_arg"] != tenant_b
    # the bound tenant_id param in the INSERT itself is tenant A's string form, never B's
    assert seen["insert_params"][0] == str(tenant_a)
    assert seen["insert_params"][0] != str(tenant_b)
