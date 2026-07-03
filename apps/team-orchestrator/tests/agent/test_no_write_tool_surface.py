"""VT-268 — agent tool-surface guardrail tests (prove-and-lock the owner boundary).

Locks the two owner guardrails at the agent's capability boundary:
  - "never update the accounts book" → the agent holds NO Sheets-write / ledger-write tool.
  - "no discount without my confirmation" → the agent holds NO direct customer-send tool;
    every send is forced through the campaign approval gate (collapse → request_owner_approval,
    Pillar-7). The agent cannot send 1:1 at all, so it cannot send an un-gated concession.

Two layers:
  1. ALLOWLIST pin — the exact agent tool surface. ANY new tool fails this test → forces review
     (catches a future PR that wires a send/write tool, even cleverly named).
  2. FAIL-CLOSED guard — `assert_agent_tools_safe` (wired at graph build) raises on a
     forbidden-capability tool. Proven to trip on synthetic send/sheets/ledger tools and to pass
     on the real surfaces; and `build_orchestrator_agent` raises if handed one.

Ground truth (2026-06-03): send_whatsapp_message has no production caller + is not an agent tool;
send_whatsapp_template is called only by execute_approved_campaign (already approval-gated). This
test LOCKS that safe state.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("langchain")


def _names(tools):
    return {getattr(t, "name", type(t).__name__) for t in tools}


# --- allowlist pins --------------------------------------------------------

ORCHESTRATOR_EXPECTED = {
    "escalate_to_fazal",
    # VT-590: compose_owner_output_tool removed from the manager inventory (see
    # orchestrator_agent.py) — the manager writes its owner reply as message text.
    "write_l0_fragment",
    "query_l0",
    # VT-466 — the manager's WRITE seam: tenant-scoped business objective record.
    # Composes over the L1 business_profile entity (MERGE-not-clobber); NOT a
    # send/ledger/accounts write (passes the VT-268 forbidden-capability guard).
    "record_business_objective",
    # VT-579 — the manager's RETRIEVAL over the lifetime conversation log.
    "search_conversation_history",
}
INTEGRATION_EXPECTED = {
    "list_connectors_tool",
    # VT-425 Phase A — de-stubbed (real Shopify): renamed out of `_stub`. pull_sample returns
    # COUNTS ONLY (no raw PII to the LLM). The connector COMMIT is NOT here — it runs server-side
    # (shopify_onboarding.pull_and_ingest_shopify), never as an agent tool (VT-268 fail-closed).
    "start_connector_setup",
    "pull_sample",
    # Field-mapping stubs kept for Phase C (Sheets/CSV); Phase A Shopify uses fixed-schema auto-map.
    "propose_field_mapping_stub",
    "confirm_field_mapping_stub",
    "setup_recurring_ingestion_stub",
    # dedupe_against_existing_stub DELETED (plan §3 "delete the concept") — commit is server-side.
    "integration_escalate_to_fazal",
}
HANDOFF_EXPECTED = {
    "spawn_sales_recovery",
    "spawn_integration",
    "spawn_onboarding_conductor",
    # VT-465 central integration — the six specialist lanes (VT-468..473) now
    # registered on ROSTER. Each spawn tool is a pure Command(goto, graph=PARENT)
    # handoff (no send/write capability — the lanes hold no forbidden tool, pinned
    # by their own per-lane allowlist tests), so they belong on this handoff pin.
    "spawn_sales_lane",
    "spawn_marketing",
    "spawn_finance_lane",
    "spawn_accounting",
    "spawn_tech",
    "spawn_cost_opt",
}

# VT-462 — the onboarding-conductor specialist's tool surface (parity allowlist pin with the
# orchestrator + integration surfaces). No send/write tool — it reasons about WHAT to ask; the
# deterministic journey reply path owns the side-effects.
ONBOARDING_CONDUCTOR_EXPECTED = {
    "onboarding_next_question",
    "onboarding_profile_complete",
    "conductor_escalate_to_fazal",
}


def test_orchestrator_tool_allowlist_pinned():
    from orchestrator.agent.orchestrator_agent import ORCHESTRATOR_AGENT_TOOLS

    # Exact match: a NEW tool (additions OR removals) fails → forces VT-268 review that the
    # new capability is not a send/write boundary breach.
    assert _names(ORCHESTRATOR_AGENT_TOOLS) == ORCHESTRATOR_EXPECTED


def test_integration_tool_allowlist_pinned():
    from orchestrator.agent.integration_agent import INTEGRATION_AGENT_TOOLS

    assert _names(INTEGRATION_AGENT_TOOLS) == INTEGRATION_EXPECTED


def test_handoff_tools_pinned():
    # VT-465 — the manager's handoff surface is the ROSTER's spawn tools (the
    # registry drives it; ``roster_spawn_tools`` is what build_supervisor_graph
    # binds as the manager's extra_tools). Pin the EXACT set — a NEW spawn tool
    # (a new lane) fails this → forces a VT-268 review that the new handoff
    # carries no send/write boundary breach.
    from orchestrator.agent.roster import roster_spawn_tools

    assert _names(roster_spawn_tools()) == HANDOFF_EXPECTED

    # The three pre-roster handoffs are still standalone exports in handoffs.py
    # (their identity/wiring unchanged) — assert they remain a subset of the pin.
    from orchestrator.handoffs import (
        spawn_integration,
        spawn_onboarding_conductor,
        spawn_sales_recovery,
    )

    assert _names(
        [spawn_sales_recovery, spawn_integration, spawn_onboarding_conductor]
    ) <= HANDOFF_EXPECTED


def test_onboarding_conductor_tool_allowlist_pinned():
    from orchestrator.agent.onboarding_conductor import ONBOARDING_CONDUCTOR_TOOLS

    # VT-462 — exact match: a NEW tool fails → forces VT-268 review that the new capability is not a
    # send/write boundary breach. The conductor reasons; it holds no send/write tool.
    assert _names(ONBOARDING_CONDUCTOR_TOOLS) == ONBOARDING_CONDUCTOR_EXPECTED


def test_dangerous_standalone_functions_are_not_agent_tools():
    """The 1:1 send tools + the ledger writer must NOT appear on any agent surface."""
    from orchestrator.agent.integration_agent import INTEGRATION_AGENT_TOOLS
    from orchestrator.agent.orchestrator_agent import ORCHESTRATOR_AGENT_TOOLS
    from orchestrator.handoffs import spawn_integration, spawn_sales_recovery

    all_names = _names(
        [*ORCHESTRATOR_AGENT_TOOLS, *INTEGRATION_AGENT_TOOLS, spawn_sales_recovery, spawn_integration]
    )
    for forbidden in ("send_whatsapp_message", "send_whatsapp_template", "record_ledger_entries"):
        assert forbidden not in all_names


# --- fail-closed guard -----------------------------------------------------

def test_guard_passes_real_surfaces():
    from orchestrator.agent.integration_agent import INTEGRATION_AGENT_TOOLS
    from orchestrator.agent.orchestrator_agent import ORCHESTRATOR_AGENT_TOOLS
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe
    from orchestrator.handoffs import spawn_integration, spawn_sales_recovery

    # No raise.
    assert_agent_tools_safe(
        [*ORCHESTRATOR_AGENT_TOOLS, spawn_sales_recovery, spawn_integration],
        surface="orchestrator_agent",
    )
    assert_agent_tools_safe(INTEGRATION_AGENT_TOOLS, surface="integration_agent")

    # VT-462 — the onboarding-conductor surface is also safe (no send/write tool).
    from orchestrator.agent.onboarding_conductor import ONBOARDING_CONDUCTOR_TOOLS

    assert_agent_tools_safe(ONBOARDING_CONDUCTOR_TOOLS, surface="onboarding_conductor")


@pytest.mark.parametrize(
    "bad_name",
    [
        "send_whatsapp_message",
        "send_whatsapp_template",
        "send_template_message",
        "append_to_sheet",
        "sheet_update",
        "values_append",
        "write_accounts_book",
        "record_ledger_entries",
        "write_ledger_entry",
    ],
)
def test_guard_trips_on_forbidden_capability(bad_name):
    from orchestrator.agent.tool_guardrail import (
        ToolGuardrailViolation,
        assert_agent_tools_safe,
    )

    bad = SimpleNamespace(name=bad_name)
    with pytest.raises(ToolGuardrailViolation):
        assert_agent_tools_safe([bad], surface="test")


def test_guard_does_not_false_flag_benign_write_tools():
    """write_l0_fragment / compose_owner_output_tool must NOT trip (specific patterns, not bare 'write')."""
    from orchestrator.agent.tool_guardrail import find_forbidden_tools

    benign = [
        SimpleNamespace(name="write_l0_fragment"),
        SimpleNamespace(name="compose_owner_output_tool"),
        SimpleNamespace(name="query_l0"),
        SimpleNamespace(name="setup_recurring_ingestion_stub"),
    ]
    assert find_forbidden_tools(benign) == []


def test_build_orchestrator_agent_rejects_send_tool():
    """Runtime fail-closed: handing the builder a send tool raises at build, not silently wires it."""
    from langchain_core.tools import tool

    from orchestrator.agent.orchestrator_agent import _MODEL, build_orchestrator_agent
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    @tool
    def send_whatsapp_message_evil(customer_id: str) -> str:
        """A would-be direct customer-send tool that must never reach the agent."""
        return customer_id

    with pytest.raises(ToolGuardrailViolation):
        build_orchestrator_agent(_MODEL, extra_tools=[send_whatsapp_message_evil])


def test_mcptool_registry_has_no_forbidden_tool():
    """The @register MCPTool registry must expose no send/write tool (empty on main)."""
    from orchestrator.agent.tool_guardrail import FORBIDDEN_CAPABILITY_SUBSTRINGS
    from orchestrator.agent.tool_registry import _REGISTRY

    for name in _REGISTRY:
        low = name.lower()
        assert not any(sub in low for sub in FORBIDDEN_CAPABILITY_SUBSTRINGS), name


# --- VT-471 — the Accounting specialist lane (v1 PREPARE-only) -------------------------------
# The lane PREPARES/SUMMARIZES; it holds NO file/submit/transact/ledger-write/send tool. The
# guard must pass on its real surface (no forbidden capability) — the v1 PREPARE-only rail.

ACCOUNTING_LANE_EXPECTED = {
    "accounting_categorize_books",
    "accounting_prepare_tax_summary",
    "accounting_organize_invoices_expenses",
    "accounting_reconcile_transactions",
    "accounting_escalate_to_fazal",
}


def test_accounting_lane_tool_allowlist_pinned():
    pytest.importorskip("langchain_anthropic")
    from orchestrator.agent.accounting_lane import ACCOUNTING_LANE_TOOLS

    # Exact match — a NEW tool (esp. a file/submit one) fails → forces VT-268 review that the
    # new capability is not a send/write/file boundary breach.
    assert _names(ACCOUNTING_LANE_TOOLS) == ACCOUNTING_LANE_EXPECTED


def test_accounting_lane_guard_passes_real_surface():
    pytest.importorskip("langchain_anthropic")
    from orchestrator.agent.accounting_lane import ACCOUNTING_LANE_TOOLS
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    assert_agent_tools_safe(ACCOUNTING_LANE_TOOLS, surface="accounting_lane")
