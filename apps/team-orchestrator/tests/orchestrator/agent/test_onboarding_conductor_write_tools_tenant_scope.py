"""VT-609 — onboarding_conductor's REAL tool surface derives tenant from the run context, never
the model. Same defect class VT-603 closed for the original two tools (``next_required_question``
/ ``profile_completion_check`` — tested in ``test_onboarding_conductor_tenant_scope.py``), extended
here to the NEW read/write/policy tools this row adds: ``read_onboarding_state``,
``extract_owner_answer``, ``record_answer``, ``record_skip``, ``apply_correction``,
``activation_check``, ``confirm_business_policy``.

Mirrors the existing pattern exactly: the ambient dispatch ``ObservabilityContext`` is ALWAYS
authoritative; a disagreeing model-supplied value (a business name, a foreign UUID) is observed +
logged (mismatch WARNING) but never trusted; no context + an unparseable model value returns the
structured ``lane_tenant_error`` dict, never a raise.
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


@contextmanager
def _fake_tenant_connection(tenant_id: Any):
    yield object()


# --- (1) read_onboarding_state -----------------------------------------------------------------


def test_read_onboarding_state_business_name_from_model_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.onboarding.journey as journey_mod
    from orchestrator.agent.onboarding_conductor import read_onboarding_state

    seen: dict[str, Any] = {}

    def _fake_get_journey(tid: Any) -> dict[str, Any]:
        seen["tenant_id"] = tid
        return {"status": "active", "answers": {"city": "Pune", "__flow__": "profile_previewed"}, "skipped": ["hours"]}

    monkeypatch.setattr(journey_mod, "get_journey", _fake_get_journey)
    # populate_profile_from_draft touches the DB (get_draft) — stub it so this stays a pure unit
    # test (no DATABASE_URL needed) and so its own no-op doesn't emit an unrelated log line that
    # would pollute the mismatch-count assertion below.
    monkeypatch.setattr(journey_mod, "populate_profile_from_draft", lambda tid: {})

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: read_onboarding_state.func(tenant_id="Sundaram Stores"),  # type: ignore[attr-defined]
            tool_name="read_onboarding_state",
        )
    assert out["status"] == "active"
    assert out["answers"] == {"city": "Pune"}  # __flow__ sentinel stripped
    assert out["skipped"] == ["hours"]
    assert out["flow"] == "profile_previewed"
    assert out["populated"] == {}
    assert seen["tenant_id"] == tenant_id


def test_read_onboarding_state_runs_populate_first_and_surfaces_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VT-609 gap fix: read_onboarding_state must run the SAME populate-first pass the interceptor
    ran eagerly at its lazy-start seam (CL-2026-07-03) — otherwise a fresh enforce-mode dispatch
    would interrogate the owner for facts public discovery already found. Every call re-checks it
    (idempotent + card-once); the delta is surfaced as ``populated`` so the specialist can present
    a card instead of asking one-by-one."""
    import orchestrator.onboarding.journey as journey_mod
    from orchestrator.agent.onboarding_conductor import read_onboarding_state

    seen: dict[str, Any] = {}

    def _fake_populate(tid: Any) -> dict[str, Any]:
        seen["populate_tenant_id"] = tid
        return {"business_type": "restaurant", "city": "Pune"}

    monkeypatch.setattr(journey_mod, "populate_profile_from_draft", _fake_populate)
    monkeypatch.setattr(
        journey_mod,
        "get_journey",
        lambda tid: {"status": "active", "answers": {"business_type": "restaurant", "city": "Pune"}, "skipped": []},
    )

    tenant_id = uuid4()
    with observability_context(run_id=uuid4(), tenant_id=tenant_id):
        out = read_onboarding_state.func(tenant_id=str(tenant_id))  # type: ignore[attr-defined]

    assert out["populated"] == {"business_type": "restaurant", "city": "Pune"}
    assert seen["populate_tenant_id"] == tenant_id


def test_read_onboarding_state_populate_first_failure_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A populate-first read failure (e.g. a transient DB error) must NEVER raise out of the tool —
    the state read still succeeds with populated={}."""
    import orchestrator.onboarding.journey as journey_mod
    from orchestrator.agent.onboarding_conductor import read_onboarding_state

    def _boom(tid: Any) -> dict[str, Any]:
        raise RuntimeError("simulated populate-first failure")

    monkeypatch.setattr(journey_mod, "populate_profile_from_draft", _boom)
    monkeypatch.setattr(
        journey_mod, "get_journey", lambda tid: {"status": "active", "answers": {}, "skipped": []}
    )

    with observability_context(run_id=uuid4(), tenant_id=uuid4()):
        out = read_onboarding_state.func(tenant_id="whatever")  # type: ignore[attr-defined]

    assert out["status"] == "active"
    assert out["populated"] == {}


def test_read_onboarding_state_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.onboarding_conductor import read_onboarding_state

    out = read_onboarding_state.func(tenant_id="not-a-uuid")  # type: ignore[attr-defined]
    assert out == {
        "status": "error",
        "error": "read_onboarding_state: no resolvable tenant context",
    }


# --- (2) extract_owner_answer ------------------------------------------------------------------


def test_extract_owner_answer_business_name_from_model_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.onboarding.journey as journey_mod
    from orchestrator.agent.onboarding_conductor import extract_owner_answer

    seen: dict[str, Any] = {}

    def _fake_record_extracted_answer(tid: Any, field: str, value: str) -> dict[str, Any]:
        seen["tenant_id"] = tid
        return {"recorded": True, "field": field}

    monkeypatch.setattr(journey_mod, "record_extracted_answer", _fake_record_extracted_answer)

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: extract_owner_answer.func(  # type: ignore[attr-defined]
                tenant_id="Sundaram Stores", field="hours", value="9am-9pm"
            ),
            tool_name="extract_owner_answer",
        )
    assert out == {"recorded": True, "field": "hours"}
    assert seen["tenant_id"] == tenant_id


def test_extract_owner_answer_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.onboarding_conductor import extract_owner_answer

    out = extract_owner_answer.func(  # type: ignore[attr-defined]
        tenant_id="not-a-uuid", field="hours", value="9am-9pm"
    )
    assert out == {
        "status": "error",
        "error": "extract_owner_answer: no resolvable tenant context",
    }


# --- (3) record_answer -------------------------------------------------------------------------


def test_record_answer_business_name_from_model_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.onboarding.journey as journey_mod
    from orchestrator.agent.onboarding_conductor import record_answer

    seen: dict[str, Any] = {}

    def _fake_confirm_field_answer(tid: Any, field: str, value: str) -> dict[str, Any]:
        seen["tenant_id"] = tid
        return {"recorded": True, "promoted": True, "field": field}

    monkeypatch.setattr(journey_mod, "confirm_field_answer", _fake_confirm_field_answer)

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: record_answer.func(  # type: ignore[attr-defined]
                tenant_id="Sundaram Stores", field="city", value="Pune"
            ),
            tool_name="record_answer",
        )
    assert out == {"recorded": True, "promoted": True, "field": "city"}
    assert seen["tenant_id"] == tenant_id


def test_record_answer_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.onboarding_conductor import record_answer

    out = record_answer.func(  # type: ignore[attr-defined]
        tenant_id="not-a-uuid", field="city", value="Pune"
    )
    assert out == {"status": "error", "error": "record_answer: no resolvable tenant context"}


# --- (4) record_skip ---------------------------------------------------------------------------


def test_record_skip_business_name_from_model_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.onboarding.journey as journey_mod
    from orchestrator.agent.onboarding_conductor import record_skip

    seen: dict[str, Any] = {}

    def _fake_record_field_skip(tid: Any, field: str) -> dict[str, Any]:
        seen["tenant_id"] = tid
        return {"recorded": True, "field": field}

    monkeypatch.setattr(journey_mod, "record_field_skip", _fake_record_field_skip)

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: record_skip.func(tenant_id="Sundaram Stores", field="website"),  # type: ignore[attr-defined]
            tool_name="record_skip",
        )
    assert out == {"recorded": True, "field": "website"}
    assert seen["tenant_id"] == tenant_id


def test_record_skip_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.onboarding_conductor import record_skip

    out = record_skip.func(tenant_id="not-a-uuid", field="website")  # type: ignore[attr-defined]
    assert out == {"status": "error", "error": "record_skip: no resolvable tenant context"}


# --- (5) apply_correction ----------------------------------------------------------------------


def test_apply_correction_business_name_from_model_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.onboarding.journey as journey_mod
    from orchestrator.agent.onboarding_conductor import apply_correction

    seen: dict[str, Any] = {}

    def _fake_confirm_field_answer(tid: Any, field: str, value: str) -> dict[str, Any]:
        seen["tenant_id"] = tid
        return {"recorded": True, "promoted": True, "field": field}

    monkeypatch.setattr(journey_mod, "confirm_field_answer", _fake_confirm_field_answer)

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: apply_correction.func(  # type: ignore[attr-defined]
                tenant_id="Sundaram Stores", field="city", value="Pune"
            ),
            tool_name="apply_correction",
        )
    assert out == {"recorded": True, "promoted": True, "field": "city"}
    assert seen["tenant_id"] == tenant_id


def test_apply_correction_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.onboarding_conductor import apply_correction

    out = apply_correction.func(  # type: ignore[attr-defined]
        tenant_id="not-a-uuid", field="city", value="Pune"
    )
    assert out == {"status": "error", "error": "apply_correction: no resolvable tenant context"}


# --- (6) activation_check ------------------------------------------------------------------------


def test_activation_check_business_name_from_model_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.agents.onboarding_gate as gate_mod
    import orchestrator.db as db_mod
    from orchestrator.agent.onboarding_conductor import activation_check

    seen: dict[str, Any] = {}

    def _fake_is_agent_eligible(tid: Any, agent: str, *, conn: Any) -> bool:
        seen["tenant_id"] = tid
        seen["agent"] = agent
        return True

    monkeypatch.setattr(db_mod, "tenant_connection", _fake_tenant_connection)
    monkeypatch.setattr(gate_mod, "is_agent_eligible", _fake_is_agent_eligible)

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: activation_check.func(tenant_id="Sundaram Stores"),  # type: ignore[attr-defined]
            tool_name="activation_check",
        )
    assert out == {"agent": "sales_recovery", "eligible": True}
    assert seen["tenant_id"] == tenant_id
    assert seen["agent"] == "sales_recovery"


def test_activation_check_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.onboarding_conductor import activation_check

    out = activation_check.func(tenant_id="not-a-uuid")  # type: ignore[attr-defined]
    assert out == {"status": "error", "error": "activation_check: no resolvable tenant context"}


# --- (7) confirm_business_policy -----------------------------------------------------------------


def test_confirm_business_policy_business_name_from_model_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.agents.business_policy as policy_mod
    import orchestrator.db as db_mod
    from orchestrator.agent.onboarding_conductor import confirm_business_policy
    from orchestrator.agents.business_policy import BusinessPolicy

    seen: dict[str, Any] = {}

    def _fake_grant_business_policy(tid: Any, **kwargs: Any) -> BusinessPolicy:
        seen["tenant_id"] = tid
        return BusinessPolicy(
            allowed_action_types=frozenset({"customer_send"}),
            allowed_segments=frozenset({"lapsed"}),
            frequency_caps={"customer_send_per_month": 2},
            spend_ceiling_minor=50000,
        )

    monkeypatch.setattr(db_mod, "tenant_connection", _fake_tenant_connection)
    monkeypatch.setattr(policy_mod, "grant_business_policy", _fake_grant_business_policy)

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: confirm_business_policy.func(  # type: ignore[attr-defined]
                tenant_id="Sundaram Stores",
                allowed_action_types=["customer_send"],
                allowed_segments=["lapsed"],
                frequency_caps={"customer_send_per_month": 2},
                spend_ceiling_minor=50000,
            ),
            tool_name="confirm_business_policy",
        )
    assert out == {
        "granted": True,
        "allowed_action_types": ["customer_send"],
        "allowed_segments": ["lapsed"],
        "frequency_caps": {"customer_send_per_month": 2},
        "spend_ceiling_minor": 50000,
    }
    assert seen["tenant_id"] == tenant_id


def test_confirm_business_policy_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.onboarding_conductor import confirm_business_policy

    out = confirm_business_policy.func(  # type: ignore[attr-defined]
        tenant_id="not-a-uuid",
        allowed_action_types=["customer_send"],
        allowed_segments=["lapsed"],
        frequency_caps={},
        spend_ceiling_minor=0,
    )
    assert out == {
        "status": "error",
        "error": "confirm_business_policy: no resolvable tenant context",
    }
