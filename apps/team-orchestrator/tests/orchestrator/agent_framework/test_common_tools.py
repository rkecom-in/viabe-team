"""Unit tests for the COMMON READ tools (``agent_framework/tools_common.py``).

Proves the three invariants every common read tool holds (ARCHITECTURE.md §1.1/§1.3/§3):
  1. RESOLVE-FIRST, MODEL-UNTRUSTED — the ambient dispatch ``ObservabilityContext`` wins; a
     disagreeing model-supplied ``tenant_id`` is observed + logged but never trusted; an
     unresolvable tenant returns the structured ``lane_tenant_error`` dict, NEVER a raise.
  2. OWN RLS SCOPE — a DB read opens its own ``tenant_connection`` (mocked here) / delegates to a
     reader that does; a read failure returns a structured error dict, never a raise.
  3. CL-390 PII-SAFE — a return carries counts / owner-business-fields / integration phase only,
     never a customer name/phone/email key.

Plus: ``read_integration_state`` DELEGATES to the SAME onboarding seam the integration agent uses.

Dep discipline (mirrors ``test_onboarding_conductor_write_tools_tenant_scope.py``): the ``@tool``
objects pull langchain, and invoking a tool transitively imports the DB/knowledge/onboarding read
modules (whose readers are MOCKED here — no live DB). We ``importorskip('langchain')`` so the
dep-less smoke skips the module; the full suite runs all of it.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("langchain")

from orchestrator.observability.decorators import observability_context  # noqa: E402

_LANE_LOGGER = "orchestrator.agent.lane_tenant"


@contextmanager
def _fake_tenant_connection(tenant_id: Any):
    """A no-op tenant_connection (the wrapper counts are mocked, so the conn is never touched)."""
    yield object()


def _assert_context_wins_no_raise(
    caplog: pytest.LogCaptureFixture, *, call: Any, tool_name: str
) -> Any:
    """Run ``call`` inside a caplog scope; assert exactly one mismatch WARNING named ``tool_name``
    was logged (the ambient tenant won over a disagreeing model value). Returns the tool result."""
    with caplog.at_level(logging.WARNING, logger=_LANE_LOGGER):
        result = call()
    mismatches = [r for r in caplog.records if tool_name in r.getMessage()]
    assert len(mismatches) == 1, caplog.text
    assert "mismatch" in mismatches[0].getMessage().lower()
    return result


def _assert_no_customer_pii_keys(payload: dict[str, Any]) -> None:
    """CL-390: a read-tool return must carry NO customer name/phone/email key (counts/fields only)."""
    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = str(k).lower()
                assert "phone" not in lk, f"customer-PII key leaked: {k!r}"
                assert "email" not in lk, f"customer-PII key leaked: {k!r}"
                assert "display_name" not in lk, f"customer-PII key leaked: {k!r}"
                _walk(v)

    _walk(payload)


# =============================================================================================
#  read_customer_ledger_summary — counts only, resolve-first, PII-safe, own RLS scope
# =============================================================================================


class _FakeCustomers:
    """A ``CustomersWrapper`` stand-in returning fixed counts + recording its call args."""

    def __init__(self, captured: dict[str, Any], *, boom: bool = False) -> None:
        self._captured = captured
        self._boom = boom

    def count_all(self, tenant_id: Any, *, conn: Any = None) -> int:
        if self._boom:
            raise RuntimeError("db unavailable")
        self._captured["total_tid"] = tenant_id
        return 10

    def count_with_sales(self, tenant_id: Any, *, conn: Any = None) -> int:
        return 7

    def count_lapsed(self, tenant_id: Any, *, days: int, conn: Any = None) -> int:
        self._captured["lapsed_tid"] = tenant_id
        self._captured["lapsed_days"] = days
        return 3


def _patch_ledger(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any], *, boom: bool = False):
    import orchestrator.db as db_pkg
    import orchestrator.db.wrappers as wrappers_mod

    monkeypatch.setattr(db_pkg, "tenant_connection", _fake_tenant_connection)
    monkeypatch.setattr(
        wrappers_mod, "CustomersWrapper", lambda: _FakeCustomers(captured, boom=boom)
    )


def test_ledger_summary_counts_only_and_pii_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns the counts summary (total / with-sales / lapsed / window) and NO customer PII, using
    the CL-2026-07-10 45-day ``LAPSED_WINDOW_DAYS`` for the lapsed count."""
    from orchestrator.agent_framework.tools_common import read_customer_ledger_summary
    from orchestrator.db.wrappers import LAPSED_WINDOW_DAYS

    captured: dict[str, Any] = {}
    _patch_ledger(monkeypatch, captured)

    tenant = uuid4()
    with observability_context(run_id=uuid4(), tenant_id=tenant):
        out = read_customer_ledger_summary.func(tenant_id=str(tenant))  # type: ignore[attr-defined]

    assert out == {
        "total_customers": 10,
        "customers_with_sales": 7,
        "lapsed_count": 3,
        "lapsed_window_days": LAPSED_WINDOW_DAYS,
    }
    # the lapsed count used the canonical 45-day window (the SAME the owner-facing metric uses).
    assert captured["lapsed_days"] == LAPSED_WINDOW_DAYS == 45
    _assert_no_customer_pii_keys(out)


def test_ledger_summary_resolves_context_over_model_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A model-supplied tenant_id that DISAGREES with the ambient context is ignored: the read runs
    for the ambient (authoritative) tenant, and a mismatch warning is logged (VT-599 IDOR guard)."""
    from orchestrator.agent_framework.tools_common import read_customer_ledger_summary

    captured: dict[str, Any] = {}
    _patch_ledger(monkeypatch, captured)

    ambient, model_value = uuid4(), uuid4()
    with observability_context(run_id=uuid4(), tenant_id=ambient):
        _assert_context_wins_no_raise(
            caplog,
            call=lambda: read_customer_ledger_summary.func(tenant_id=str(model_value)),  # type: ignore[attr-defined]
            tool_name="read_customer_ledger_summary",
        )
    # the wrapper read for the AMBIENT tenant, never the model-supplied one.
    assert captured["lapsed_tid"] == ambient
    assert captured["total_tid"] == ambient


def test_ledger_summary_unresolvable_returns_error_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient context + an unparseable model value -> the structured lane_tenant_error dict
    (never a raise, never a DB read)."""
    from orchestrator.agent_framework.tools_common import read_customer_ledger_summary

    captured: dict[str, Any] = {}
    _patch_ledger(monkeypatch, captured)

    out = read_customer_ledger_summary.func(tenant_id="not-a-uuid")  # type: ignore[attr-defined]
    assert out == {
        "status": "error",
        "error": "read_customer_ledger_summary: no resolvable tenant context",
    }
    assert captured == {}  # short-circuited before any DB read


def test_ledger_summary_db_failure_returns_error_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB read failure returns a structured error dict — a lane tool must NEVER raise (would orphan
    the tool_use / hang the run)."""
    from orchestrator.agent_framework.tools_common import read_customer_ledger_summary

    captured: dict[str, Any] = {}
    _patch_ledger(monkeypatch, captured, boom=True)

    tenant = uuid4()
    with observability_context(run_id=uuid4(), tenant_id=tenant):
        out = read_customer_ledger_summary.func(tenant_id=str(tenant))  # type: ignore[attr-defined]
    assert out == {"status": "error", "error": "read_customer_ledger_summary: ledger read failed"}


# =============================================================================================
#  read_business_context — owner-business summary, no customer PII, delegates to the §7 reader
# =============================================================================================


def test_business_context_summary_shape_and_delegation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delegates to the manager's §7 ``read_business_context`` seam and returns a summary of its
    structured fields (identity / profile / objective + an L1-presence boolean); business fields are
    the owner's own data (CL-390 OK) and no customer PII key appears."""
    import orchestrator.knowledge.business_context as bc_mod
    from orchestrator.agent_framework.tools_common import read_business_context

    captured: dict[str, Any] = {}
    fake_bc = SimpleNamespace(
        identity={"business_name": "Sundaram Stores", "gst_status": "gstin_verified", "gst_verified": True},
        profile={"business_archetype": "kirana", "working_hours": "9-9"},
        objective={"goal": "recover lapsed customers"},
        l1_block="<<rendered L1 block>>",
    )

    def _fake_reader(tenant_id: Any) -> Any:
        captured["tenant_id"] = tenant_id
        return fake_bc

    monkeypatch.setattr(bc_mod, "read_business_context", _fake_reader)

    tenant = uuid4()
    with observability_context(run_id=uuid4(), tenant_id=tenant):
        out = read_business_context.func(tenant_id=str(tenant))  # type: ignore[attr-defined]

    assert out == {
        "identity": fake_bc.identity,
        "profile": fake_bc.profile,
        "objective": fake_bc.objective,
        "has_l1_context": True,  # the boolean summary, NOT the rendered block itself
    }
    assert "<<rendered L1 block>>" not in str(out)  # the L1 block is not dumped
    assert captured["tenant_id"] == tenant
    _assert_no_customer_pii_keys(out)


def test_business_context_unresolvable_returns_error_dict() -> None:
    from orchestrator.agent_framework.tools_common import read_business_context

    out = read_business_context.func(tenant_id="not-a-uuid")  # type: ignore[attr-defined]
    assert out == {"status": "error", "error": "read_business_context: no resolvable tenant context"}


# =============================================================================================
#  read_integration_state — delegates to the SAME onboarding seam the integration agent uses
# =============================================================================================


def test_integration_state_delegates_to_shared_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delegates to ``onboarding.shopify_onboarding.read_integration_state`` (the SAME reader the
    integration agent's tool uses) and returns its result verbatim, for the resolved tenant."""
    import orchestrator.onboarding.shopify_onboarding as onb_mod
    from orchestrator.agent_framework.tools_common import read_integration_state

    captured: dict[str, Any] = {}
    state = {
        "phase": "mapping",
        "current_connector_id": "google_sheet",
        "pending_owner_input": {"awaiting": "field_mapping_confirm"},
    }

    def _fake_reader(tenant_id: Any) -> Any:
        captured["tenant_id"] = tenant_id
        return state

    monkeypatch.setattr(onb_mod, "read_integration_state", _fake_reader)

    tenant = uuid4()
    with observability_context(run_id=uuid4(), tenant_id=tenant):
        out = read_integration_state.func(tenant_id=str(tenant))  # type: ignore[attr-defined]

    assert out == state
    assert captured["tenant_id"] == tenant
    _assert_no_customer_pii_keys(out)


def test_integration_state_none_returns_all_none_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tenant with no onboarding started (reader returns None) -> the all-None shape (mirrors the
    integration agent's own tool)."""
    import orchestrator.onboarding.shopify_onboarding as onb_mod
    from orchestrator.agent_framework.tools_common import read_integration_state

    monkeypatch.setattr(onb_mod, "read_integration_state", lambda _t: None)

    tenant = uuid4()
    with observability_context(run_id=uuid4(), tenant_id=tenant):
        out = read_integration_state.func(tenant_id=str(tenant))  # type: ignore[attr-defined]
    assert out == {"phase": None, "current_connector_id": None, "pending_owner_input": None}


def test_integration_state_unresolvable_returns_error_dict() -> None:
    from orchestrator.agent_framework.tools_common import read_integration_state

    out = read_integration_state.func(tenant_id="not-a-uuid")  # type: ignore[attr-defined]
    assert out == {
        "status": "error",
        "error": "read_integration_state: no resolvable tenant context",
    }


# =============================================================================================
#  the surface itself — deny-list clean, the exact three tools
# =============================================================================================


def test_common_read_tools_surface_is_the_three_reads() -> None:
    from orchestrator.agent_framework.tools_common import COMMON_READ_TOOLS

    assert [t.name for t in COMMON_READ_TOOLS] == [
        "read_customer_ledger_summary",
        "read_business_context",
        "read_integration_state",
    ]


def test_common_read_tools_pass_the_deny_list() -> None:
    """The import-time ``assert_agent_tools_safe`` already ran; re-assert explicitly that the read
    surface holds no forbidden send/ledger-write/config-write capability."""
    from orchestrator.agent.tool_guardrail import find_forbidden_tools
    from orchestrator.agent_framework.tools_common import COMMON_READ_TOOLS

    assert find_forbidden_tools(COMMON_READ_TOOLS) == []
