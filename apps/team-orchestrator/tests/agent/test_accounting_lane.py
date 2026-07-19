"""VT-471 — the Accounting specialist lane (v1 PREPARE-ONLY) tests.

Pins the accounting lane WITHOUT a live Anthropic call. The lane is a DISJOINT module
(built concurrently); the coordinator registers ``SPECIALIST_SPEC`` into ``roster.ROSTER``
centrally, so these tests pin the MODULE's own contract (its exported spec + tool surface),
NOT the live ROSTER/supervisor graph (that is the coordinator's integration test).

Three things are locked:

  1. ``SPECIALIST_SPEC`` is a well-formed roster registration (accounting lane → END,
     compiled sub-graph, self-fetching) the coordinator can append.
  2. The HARD RAIL — v1 PREPARE-only: the tool surface holds NO file / submit / transact /
     write / send capability (every tool is a READ + SUMMARIZE/REPORT). Asserted by the
     VT-268 ``find_forbidden_tools`` guard AND by an explicit no-filing-substring scan AND by
     the build raising on a synthetic filing/submit tool.
  3. The tools PRODUCE summaries/reports (advisory output), and the DOCUMENTED future
     filing/submit seam is ABSENT as a capability (no such tool exists in v1).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")


# --- (1) SPECIALIST_SPEC is a well-formed roster registration ------------------------------


def test_specialist_spec_shape() -> None:
    from orchestrator.agent.accounting_lane import SPECIALIST_SPEC
    from orchestrator.agent.roster import SpecialistSpec

    spec = SPECIALIST_SPEC
    assert isinstance(spec, SpecialistSpec)
    assert spec.name == "accounting"
    assert spec.agent_name == "accounting_lane"
    assert spec.spawn_tool_name == "spawn_accounting"
    assert spec.route_key == "spawn_accounting"
    assert spec.wrap_node is False  # compiled sub-graph — never function-wrapped (VT-183/206)
    assert spec.edge_to is None  # -> END (advisory summary, not a campaign plan)
    assert spec.update_builder is None  # lane self-fetches via its tools (keyed on tenant_id)
    assert spec.prereq is None
    assert spec.default_outcome  # a non-empty desired-outcome default for the envelope


def test_specialist_spec_makes_a_spawn_tool() -> None:
    """The spec wires through the shared make_spawn_tool factory (no graph surgery here)."""
    from orchestrator.agent.accounting_lane import SPECIALIST_SPEC

    spawn = SPECIALIST_SPEC.make_spawn()
    assert spawn.name == "spawn_accounting"
    # The spec's node_builder produces a compiled sub-graph (the lane agent).
    node = SPECIALIST_SPEC.node_builder(None)
    assert node is not None


# --- (2) THE HARD RAIL: v1 PREPARE-only — NO file/submit/transact/write/send capability -----


ACCOUNTING_EXPECTED = {
    "accounting_categorize_books",
    "accounting_prepare_tax_summary",
    "accounting_organize_invoices_expenses",
    "accounting_reconcile_transactions",
    "accounting_escalate_to_fazal",
}


def test_tool_allowlist_pinned() -> None:
    """Exact match — a NEW tool (esp. a file/submit one) fails → forces VT-268 review."""
    from orchestrator.agent.accounting_lane import ACCOUNTING_LANE_TOOLS

    names = {t.name for t in ACCOUNTING_LANE_TOOLS}
    assert names == ACCOUNTING_EXPECTED


def test_lane_holds_no_send_or_write_tool() -> None:
    """VT-268 fail-closed guard: the lane surface exposes no forbidden write/send capability."""
    from orchestrator.agent.accounting_lane import ACCOUNTING_LANE_TOOLS
    from orchestrator.agent.tool_guardrail import find_forbidden_tools

    assert find_forbidden_tools(ACCOUNTING_LANE_TOOLS) == []


def test_no_filing_or_submit_capability_on_surface() -> None:
    """PREPARE-only: no tool name implies filing / submitting / transacting.

    The future filing/submit seam is documented but UNBUILT — there must be no tool whose
    name suggests it files a return, submits to a portal, or moves money. Independent of the
    VT-268 substring list (which is about send/write); this asserts the v1 PREPARE rail.

    Fragments are the ACTION verbs (file_/submit_/pay_/issue_/raise_) — NOT the noun
    "transactions" (the legitimate ``accounting_reconcile_transactions`` reads/reports them,
    it does not transact).
    """
    from orchestrator.agent.accounting_lane import ACCOUNTING_LANE_TOOLS

    forbidden_fragments = (
        "file_return",
        "file_gst",
        "submit_gst",
        "submit_return",
        "portal_submit",
        "balance_sheet",
        "make_payment",
        "pay_vendor",
        "settle_",
        "raise_invoice",
        "issue_invoice",
    )
    names = [t.name.lower() for t in ACCOUNTING_LANE_TOOLS]
    for name in names:
        for frag in forbidden_fragments:
            assert frag not in name, f"{name} implies a forbidden file/submit/transact capability"


def test_build_lane_rejects_a_filing_tool() -> None:
    """Runtime fail-closed: handing the builder a side-effecting tool raises at build.

    A would-be GST-submit tool matches a VT-268 forbidden substring (submit/spend family) and
    the build must RAISE rather than silently open the PREPARE-only boundary.
    """
    from langchain_core.tools import tool

    from orchestrator.agent.accounting_lane import _MODEL, build_accounting_lane_agent
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    @tool
    def make_payment_to_gst_portal(tenant_id: str) -> str:
        """A would-be money-moving tool that must never reach the accounting lane."""
        return tenant_id

    with pytest.raises(ToolGuardrailViolation):
        build_accounting_lane_agent(_MODEL, extra_tools=[make_payment_to_gst_portal])


def test_build_lane_rejects_a_ledger_write_tool() -> None:
    """The lane must never hold a ledger/accounts-book write tool (it PREPARES, not writes)."""
    from langchain_core.tools import tool

    from orchestrator.agent.accounting_lane import _MODEL, build_accounting_lane_agent
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    @tool
    def write_ledger_entry(tenant_id: str) -> str:
        """A would-be ledger writer that must never reach the accounting lane."""
        return tenant_id

    with pytest.raises(ToolGuardrailViolation):
        build_accounting_lane_agent(_MODEL, extra_tools=[write_ledger_entry])


def test_real_surface_passes_guard() -> None:
    from orchestrator.agent.accounting_lane import ACCOUNTING_LANE_TOOLS
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    # No raise — the v1 PREPARE-only surface is safe.
    assert_agent_tools_safe(ACCOUNTING_LANE_TOOLS, surface="accounting_lane")


# --- (3) the tools PRODUCE summaries/reports; advisory output, never a filed action ----------


def test_categorize_books_produces_a_prepared_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    """``accounting_categorize_books`` returns a categorized books SUMMARY (read-only).

    Patches the module-level ``_read_ledger_summary`` so no DB is touched — proving the tool
    composes a PREPARED view (counts/totals), never writes.
    """
    import orchestrator.agent.accounting_lane as lane
    from orchestrator.agent.accounting_lane import accounting_categorize_books

    monkeypatch.setattr(
        lane,
        "_read_ledger_summary",
        lambda tid: {
            "sale": {"count": 3, "total_paise": 30000, "total_inr": 300.0,
                     "first_date": "2026-06-01", "last_date": "2026-06-20"},
            "payment": {"count": 2, "total_paise": 20000, "total_inr": 200.0,
                        "first_date": "2026-06-02", "last_date": "2026-06-18"},
        },
    )
    from uuid import uuid4

    out = accounting_categorize_books.func(str(uuid4()))  # type: ignore[attr-defined]
    assert out["by_entry_type"]["sale"]["total_inr"] == 300.0
    assert "PREPARED" in out["note"]  # labelled advisory, not filed/finalized


def test_prepare_tax_summary_is_an_estimate_for_owner_to_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """``accounting_prepare_tax_summary`` PREPARES an estimate + tells the owner THEY file it."""
    import orchestrator.agent.accounting_lane as lane
    import orchestrator.knowledge.business_context as bctx
    from orchestrator.agent.accounting_lane import accounting_prepare_tax_summary

    fake_ctx = SimpleNamespace(
        identity={
            "gst_status": "gstin_verified",
            "gst_verified": True,
            "gstin_present": True,
            "business_name": "Sundaram Stores",
        },
    )
    monkeypatch.setattr(bctx, "read_business_context", lambda tid: fake_ctx)
    monkeypatch.setattr(
        lane,
        "_read_ledger_summary",
        lambda tid: {"sale": {"total_inr": 12500.0, "first_date": "2026-06-01",
                              "last_date": "2026-06-30"}},
    )
    from uuid import uuid4

    out = accounting_prepare_tax_summary.func(str(uuid4()))  # type: ignore[attr-defined]
    assert out["taxable_turnover_inr"] == 12500.0
    assert out["gst_verified"] is True
    # The rail: the lane PREPARES; the owner FILES. The output must say so + must NOT claim filed.
    assert "does NOT file" in out["note"] or "did not file" in out["next_step_for_owner"]
    assert "filed" not in out["note"].lower().replace("not file", "")


def test_reconcile_transactions_reports_matches_without_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``accounting_reconcile_transactions`` REPORTS matched/unmatched — never attributes/writes.

    Patches the RLS read + the deterministic matcher so no DB/pool is touched, proving the
    tool reuses ``match_transactions`` as a read-only scorer and does NOT call the WRITE
    counterpart ``attribute_imported_transactions``.
    """
    # The tool lazily imports tenant_connection / get_pool / match_transactions inside it;
    # patch on the SOURCE modules so the lazy resolution picks the fakes up. NOTE: the
    # `orchestrator.db` package __init__ re-exports `tenant_connection` (the function), so
    # `orchestrator.db.tenant_connection` resolves to the function, NOT the submodule — reach
    # the real submodule via sys.modules to patch its attribute (the lazy import targets it).
    import sys
    from contextlib import contextmanager
    from datetime import date

    import orchestrator.agent.tools.match_transactions as mt
    import orchestrator.graph as graph_mod

    tc_mod = sys.modules["orchestrator.db.tenant_connection"]

    class _FakeConn:
        def execute(self, *_a: Any, **_k: Any) -> "_FakeConn":
            return self

        def fetchall(self) -> list[dict[str, Any]]:
            return [{"id": "11111111-1111-1111-1111-111111111111",
                     "amount_paise": 5000, "txn_date": date(2026, 6, 10)}]

    @contextmanager
    def _fake_tc(_tid: Any, **_k: Any):  # type: ignore[no-untyped-def]
        yield _FakeConn()

    monkeypatch.setattr(tc_mod, "tenant_connection", _fake_tc)
    monkeypatch.setattr(graph_mod, "get_pool", lambda: object())

    out_result = SimpleNamespace(
        matches=[SimpleNamespace(txn_id="t1")],
        unmatched=[SimpleNamespace(txn_id="t2", reason="no_amount_match")],
    )
    monkeypatch.setattr(mt, "match_transactions", lambda payload, **k: out_result)

    from uuid import uuid4

    from orchestrator.agent.accounting_lane import accounting_reconcile_transactions

    out = accounting_reconcile_transactions.func(str(uuid4()), 90)  # type: ignore[attr-defined]
    assert out["matched_count"] == 1
    assert out["unmatched_count"] == 1
    assert out["unmatched_reasons"]["no_amount_match"] == 1
    assert "does not attribute, correct, or write" in out["note"]
