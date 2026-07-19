"""VT-609 — onboarding_conductor's REAL tool surface derives tenant from the run context, never
the model. Same defect class VT-603 closed for the original two tools (``next_required_question``
/ ``profile_completion_check`` — tested in ``test_onboarding_conductor_tenant_scope.py``), extended
here to the NEW read/write/policy tools this row adds: ``read_onboarding_state``,
``extract_owner_answer``, ``record_answer``, ``record_skip``, ``apply_correction``,
``activation_check``, ``propose_business_policy``. (The original ``resolve_business_policy_proposal``
tool was DELETED in the VT-609 fix round 2 CRITICAL redesign — it was never reliably re-dispatched
on the owner's clear yes; the grant is now applied by the deterministic approval-glue, tested in
``test_autonomy_rails_vt474.py``'s Section E, not here.)

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


class _FakeTransactionConn:
    """A fake conn supporting ``.transaction()`` — for the propose/resolve business-policy tools,
    which wrap their (mocked) grant/arm calls in an explicit ``conn.transaction()`` block (atomicity
    across the pipeline_runs + pending_approvals inserts)."""

    @contextmanager
    def transaction(self) -> Any:
        yield


@contextmanager
def _fake_tenant_connection_with_txn(tenant_id: Any):
    yield _FakeTransactionConn()


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


def test_read_onboarding_state_populate_delta_triggers_completion_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VT-609 gap fix (mapping-table audit): unlike the legacy walker (which only ever runs
    populate-first ONCE, at journey-start, and completes inline right there when nothing else
    remains), read_onboarding_state runs populate-first on EVERY call — so populate-first can land
    the tenant's LAST remaining necessities with no write-tool call following it in the same turn.
    ``populate_profile_from_draft`` itself never transitions completion (the caller always has),
    so this tool must re-check + transition it itself when ``populated`` is non-empty — otherwise
    the journey row stays 'active' forever and ``activation_check`` can never admit the tenant."""
    import orchestrator.onboarding.journey as journey_mod
    from orchestrator.agent.onboarding_conductor import read_onboarding_state

    seen: dict[str, Any] = {}

    monkeypatch.setattr(
        journey_mod, "populate_profile_from_draft", lambda tid: {"business_type": "restaurant"}
    )
    monkeypatch.setattr(
        journey_mod,
        "get_journey",
        lambda tid: {"status": "active", "answers": {"business_type": "restaurant"}, "skipped": []},
    )

    def _fake_complete(tid: Any) -> bool:
        seen["completed_tenant_id"] = tid
        return True

    monkeypatch.setattr(journey_mod, "maybe_complete_from_populate", _fake_complete)

    tenant_id = uuid4()
    with observability_context(run_id=uuid4(), tenant_id=tenant_id):
        read_onboarding_state.func(tenant_id=str(tenant_id))  # type: ignore[attr-defined]

    assert seen["completed_tenant_id"] == tenant_id


def test_read_onboarding_state_empty_populate_delta_skips_completion_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The completion re-check only needs to run when populate-first actually changed something —
    a no-op populate (the common case, every turn after the first) must not pay for an extra
    completion-derivation round trip."""
    import orchestrator.onboarding.journey as journey_mod
    from orchestrator.agent.onboarding_conductor import read_onboarding_state

    called = {"count": 0}

    monkeypatch.setattr(journey_mod, "populate_profile_from_draft", lambda tid: {})
    monkeypatch.setattr(
        journey_mod, "get_journey", lambda tid: {"status": "active", "answers": {}, "skipped": []}
    )

    def _fake_complete(tid: Any) -> bool:
        called["count"] += 1
        return False

    monkeypatch.setattr(journey_mod, "maybe_complete_from_populate", _fake_complete)

    with observability_context(run_id=uuid4(), tenant_id=uuid4()):
        read_onboarding_state.func(tenant_id="whatever")  # type: ignore[attr-defined]

    assert called["count"] == 0


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


def test_read_onboarding_state_completion_recheck_failure_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure in the post-populate completion re-check (e.g. a transient DB error deriving
    ``profile_collection_complete``) must NEVER raise out of the tool — the just-committed
    ``populated`` delta is still surfaced to the caller."""
    import orchestrator.onboarding.journey as journey_mod
    from orchestrator.agent.onboarding_conductor import read_onboarding_state

    monkeypatch.setattr(
        journey_mod, "populate_profile_from_draft", lambda tid: {"business_type": "restaurant"}
    )
    monkeypatch.setattr(
        journey_mod,
        "get_journey",
        lambda tid: {"status": "active", "answers": {"business_type": "restaurant"}, "skipped": []},
    )

    def _boom(tid: Any) -> bool:
        raise RuntimeError("simulated completion-check failure")

    monkeypatch.setattr(journey_mod, "maybe_complete_from_populate", _boom)

    with observability_context(run_id=uuid4(), tenant_id=uuid4()):
        out = read_onboarding_state.func(tenant_id="whatever")  # type: ignore[attr-defined]

    assert out["status"] == "active"
    assert out["populated"] == {"business_type": "restaurant"}


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


# --- (7) propose_business_policy ----------------------------------------------------------------


def test_propose_business_policy_business_name_from_model_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.agent.onboarding_conductor as conductor_mod
    import orchestrator.agents.business_policy as policy_mod
    import orchestrator.db as db_mod
    from orchestrator.agent.onboarding_conductor import propose_business_policy

    seen: dict[str, Any] = {}

    # The profile-completeness gate is a SEPARATE concern (see the fix-round tests below) — bypass
    # it here so this test isolates the tenant-scope property.
    monkeypatch.setattr(conductor_mod, "_profile_is_complete", lambda tid: True)

    def _fake_propose(tid: Any, **kwargs: Any) -> dict[str, Any]:
        seen["tenant_id"] = tid
        return {
            "status": "pending_owner_approval",
            "approval_id": "approval-1",
            "allowed_action_types": ["customer_send"],
            "allowed_segments": ["lapsed"],
            "frequency_caps": {"customer_send_per_month": 2},
            "spend_ceiling_minor": 50000,
        }

    monkeypatch.setattr(db_mod, "tenant_connection", _fake_tenant_connection_with_txn)
    monkeypatch.setattr(policy_mod, "propose_business_policy_grant", _fake_propose)

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: propose_business_policy.func(  # type: ignore[attr-defined]
                tenant_id="Sundaram Stores",
                allowed_action_types=["customer_send"],
                allowed_segments=["lapsed"],
                frequency_caps={"customer_send_per_month": 2},
                spend_ceiling_minor=50000,
            ),
            tool_name="propose_business_policy",
        )
    assert out["status"] == "pending_owner_approval"
    assert out["allowed_action_types"] == ["customer_send"]
    assert seen["tenant_id"] == tenant_id


def test_propose_business_policy_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.onboarding_conductor import propose_business_policy

    out = propose_business_policy.func(  # type: ignore[attr-defined]
        tenant_id="not-a-uuid",
        allowed_action_types=["customer_send"],
        allowed_segments=["lapsed"],
        frequency_caps={},
        spend_ceiling_minor=0,
    )
    assert out == {
        "status": "error",
        "error": "propose_business_policy: no resolvable tenant context",
    }


def test_propose_business_policy_refuses_when_profile_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VT-609 fix round (CRITICAL — the ordering MAJOR folded in): the profile-completeness gate
    is enforced IN CODE, not left to the model's own sense of the conversation."""
    import orchestrator.agent.onboarding_conductor as conductor_mod
    from orchestrator.agent.onboarding_conductor import propose_business_policy

    monkeypatch.setattr(conductor_mod, "_profile_is_complete", lambda tid: False)

    with observability_context(run_id=uuid4(), tenant_id=uuid4()):
        out = propose_business_policy.func(  # type: ignore[attr-defined]
            tenant_id="whatever",
            allowed_action_types=["customer_send"],
            allowed_segments=["lapsed"],
            frequency_caps={},
            spend_ceiling_minor=0,
        )
    assert out == {"status": "error", "error": "profile_setup_incomplete"}


def test_propose_business_policy_drops_unrecognized_action_types_and_clamps_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VT-609 fix round (CRITICAL) — bounds validation: an unrecognized action type is dropped
    (never silently passed through to a money-bearing proposal), and the spend ceiling is clamped
    to the defensive sanity ceiling rather than accepting an absurd value verbatim."""
    import orchestrator.agent.onboarding_conductor as conductor_mod
    import orchestrator.agents.business_policy as policy_mod
    import orchestrator.db as db_mod
    from orchestrator.agent.onboarding_conductor import propose_business_policy
    from orchestrator.agents.business_policy import MAX_SANE_SPEND_CEILING_MINOR

    monkeypatch.setattr(conductor_mod, "_profile_is_complete", lambda tid: True)
    monkeypatch.setattr(db_mod, "tenant_connection", _fake_tenant_connection_with_txn)

    captured: dict[str, Any] = {}

    def _fake_propose(tid: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "pending_owner_approval", "approval_id": "approval-1", **kwargs}

    monkeypatch.setattr(policy_mod, "propose_business_policy_grant", _fake_propose)

    with observability_context(run_id=uuid4(), tenant_id=uuid4()):
        out = propose_business_policy.func(  # type: ignore[attr-defined]
            tenant_id="whatever",
            allowed_action_types=["customer_send", "not_a_real_type"],
            allowed_segments=["lapsed"],
            frequency_caps={"customer_send_per_month": 2},
            spend_ceiling_minor=999_999_999,
        )
    assert captured["allowed_action_types"] == ["customer_send"]
    assert captured["spend_ceiling_minor"] == MAX_SANE_SPEND_CEILING_MINOR
    assert out["status"] == "pending_owner_approval"


def test_propose_business_policy_refuses_when_no_valid_action_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing valid survives validation -> refuse rather than arm an empty/meaningless proposal."""
    import orchestrator.agent.onboarding_conductor as conductor_mod
    from orchestrator.agent.onboarding_conductor import propose_business_policy

    monkeypatch.setattr(conductor_mod, "_profile_is_complete", lambda tid: True)

    with observability_context(run_id=uuid4(), tenant_id=uuid4()):
        out = propose_business_policy.func(  # type: ignore[attr-defined]
            tenant_id="whatever",
            allowed_action_types=["not_a_real_type"],
            allowed_segments=["lapsed"],
            frequency_caps={},
            spend_ceiling_minor=0,
        )
    assert out == {"status": "error", "error": "no_valid_action_types"}

# NOTE (VT-609 fix round 2, CRITICAL): section (8) used to test ``resolve_business_policy_proposal``
# here — that tool was DELETED. An inbound owner reply is consumed by
# ``runner.try_resume_pending_approval`` BEFORE the conductor is ever re-dispatched, so a specialist
# resolve tool built to fire on the owner's clear yes was, structurally, only ever reachable on an
# AMBIGUOUS reply (the one case it must NOT grant) — a resolved-but-never-granted row, permanently
# stuck deny-all. The grant now runs through the deterministic approval-glue
# (``business_policy.apply_business_policy_decision``, dispatched from
# ``approval_resume._apply_agent_glue``) on the SAME choke point every other approval type resolves
# through. See ``test_autonomy_rails_vt474.py``'s Section E for the DB-backed inbound-path proof.
