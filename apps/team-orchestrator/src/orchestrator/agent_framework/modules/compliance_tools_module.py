"""VT-685 — the Compliance specialist SKELETON (Codex-onboarding target #1).

WHAT THIS IS
------------
The FIRST Codex-built specialist target named in ``docs/agent-framework/CODEX-ONBOARDING.md``: an
independent, framework-conforming module for GST return-filing READINESS (Phase 1) and, later,
ROC/AOC/balance-sheet readiness (Phase 2, MCA-gated — see the hard boundary below). This file is a
SKELETON: it registers, it conforms (``assert_conforms`` passes all 10 checks, VT-686's
``brief_complete`` included — see the manifest's ``category``/``tags``/``brief`` below), and it carries ONE
real, working, read-only example tool — ``gstr_filing_readiness_snapshot`` — so Codex has a proven
seam to extend rather than a blank page. Every extension point is marked ``TODO(Codex)``.

PHASE-1 POSTURE — ADVISORY / PREPARE-ONLY (the hard rail, non-negotiable)
--------------------------------------------------------------------------
This module READS + ANALYSES + PREPARES. It NEVER files, sends, spends, or mutates business state.
It declares NO gated (``REQUEST_*``) capability and is a pure ``{PROPOSER}`` — structurally, its
``GateFacade`` is empty and raises ``CapabilityNotDeclared`` on every gated method (proven by the
``proposer_gate_readonly`` conformance check). Real return FILING is a LATER graduation through
``orchestrator.capability.registry`` (see ``compliance.return_filing``, declared TODAY as a
``mode="disabled"`` honesty entry — available in NO environment — so the Manager can decline a
"file my GST return" ask truthfully instead of silently failing or fabricating an outcome). Do not
add a filing/submit/portal tool to this module without that registry entry graduating AND an
explicit Fazal grant — mirrors the accounting lane's identical v1 PREPARE-only rail
(``agent/accounting_lane.py``).

MCA / ROC BOUNDARY (hard, Fazal-standing — do not cross without an explicit un-park)
--------------------------------------------------------------------------------------
ROC/AOC/balance-sheet readiness is PARKED: there is no MCA/ROC integration in this codebase today,
and building one is explicitly OUT OF SCOPE until Fazal un-parks it. Any ROC/AOC readiness work
Codex adds here MUST source its facts from OWNER-PROVIDED documents only (the owner uploads/pastes
figures; the Manager is the owner-comms surface, ARCHITECTURE.md §1.2) — never an automated
MCA/ROC scrape or portal integration. TODO(Codex): when Fazal un-parks MCA, that graduation gets
its OWN capability registry entry (mirroring ``compliance.gstr_readiness`` below) and its own
canary-gated integration — do not fold it into the GSTR reads silently.

THE ONE WORKING EXAMPLE TOOL — ``gstr_filing_readiness_snapshot``
--------------------------------------------------------------------
A read-only READINESS check (NOT a filing, NOT an estimate of tax owed): it reports whether the
data a GSTR-1/3B filing would need is PRESENT, never whether a return IS filed. It composes over
TWO existing reads — never re-authors either:

  - ``knowledge.business_context.read_business_context`` → ``identity.gst_verified`` (the SAME
    verified-tiers boundary ``onboarding_gate``/``activation_registry`` use — this module never
    re-derives what "verified" means).
  - a direct RLS-scoped read of ``customer_ledger_entries`` (mirrors
    ``agent.accounting_lane._read_ledger_summary``'s pattern EXACTLY: that table is NOT one of the
    ``check_no_direct_tenant_db_access.py``-gated hot tables, so direct SQL through
    ``tenant_connection`` is the sanctioned shape here — same as the accounting lane, not a
    deviation) for the months with >=1 recorded sale + the trailing-90-day sale count.

Returns ``{gstin_verified, ledger_months_present, sales_entries_90d, readiness_notes}`` — every
field traces to a real read; nothing is fabricated (an empty ledger reads as an honest empty, never
a guessed readiness verdict).

IMPORT-LIGHT (mirrors ``integration_tools_module`` / ``onboarding_conductor_module`` EXACTLY)
------------------------------------------------------------------------------------------------
``gstr_filing_readiness_snapshot`` is a plain, directly-callable, directly-unit-testable function
(no decorator at definition). Wrapping it as a real langchain ``@tool`` object happens ONLY inside
``_compliance_tools()``, called LAZILY from ``ComplianceToolsModule.__init__`` — so
``import orchestrator.agent_framework.modules.compliance_tools_module`` (or referencing
``ComplianceToolsModule`` without instantiating it) pulls NO ``langchain_core`` / DB deps; only
constructing ``ComplianceToolsModule()`` does. This is the SAME import-light discipline every
sibling framework module follows (never regress it when extending this file). The DB/knowledge
readers are likewise LAZY-imported INSIDE the read functions. The reader is INJECTABLE
(``reader=``, the repo's transport-injection convention, cf. ``reference_plugin``'s ``reader=`` /
``integration_tools_module``'s ``state_reader=``) so the module unit-tests with no DB.

CAPABILITY MODELING (truthful, minimal, NON-GATED)
---------------------------------------------------
The manifest declares exactly the two NON-GATED read capabilities the snapshot exercises —
``{READ_BUSINESS_CONTEXT, READ_CUSTOMER_LEDGER}`` — and NO gated (``REQUEST_*``) capability,
consistent with the PROPOSER-only role. There is no customer send and no money action anywhere on
this surface.

INERT / ADDITIVE
-----------------
Importing this module wires NOTHING: it is NOT imported by ``agent_framework/__init__`` and it does
NOT register itself. Registration (``register_agent``) and activation-registry wiring
(``register_activation_prereqs``) are deliberate, later, Fazal-reviewed steps — see
``docs/agent-framework/CODEX-ONBOARDING.md`` §6 for the workflow Codex follows to get there.

TODO(Codex) — the extension points, in the order the onboarding doc's roadmap expects
----------------------------------------------------------------------------------------
  1. GSTR-1 (outward supplies) needs invoice-level detail this snapshot does NOT read yet (HSN/SAC
     codes, per-invoice tax rate, B2B vs B2C split). GSTR-3B needs the aggregate turnover + a
     tax-liability breakup (not just a sale count). Add a SIBLING read (do not overload
     ``gstr_filing_readiness_snapshot`` past its honest, structural scope) once those fields exist
     in the ledger schema.
  2. ``prerequisites=None`` today (a read is always safe, like the reference plugin). If this
     module graduates to something with a real activation bar (e.g. requires GST verification +
     journey complete before it can run), declare an ``AgentPrerequisites`` here — see
     ``agents/activation_registry.py`` for the shape; reuse, do not fork.
  3. ``entitlement_key="compliance_agent"`` is declared but UN-enforced (soft-open, D-ENT — see
     ``agent_framework/entitlement.py``). No action needed until billing wires; do not hand-roll a
     price check here.
  4. If this module's tool surface grows enough to need VT-669 ``required_tools`` sufficiency
     modeling, it must ALSO gain a defining-surface entry in
     ``agent_framework/tool_catalog.py`` (its tools are not on any existing catalog surface today) —
     add both together, never one without the other (the ``required_tools_reachable`` conformance
     check will fail loud if you forget).
  5. ROC/AOC/balance-sheet readiness — see the MCA/ROC boundary above. A NEW registry entry
     (``compliance.roc_readiness`` or similar), a NEW read function, and an explicit Fazal un-park
     — never silently widened out of the GSTR reads.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from orchestrator.agent_framework.capabilities import AgentRole, Capability
from orchestrator.agent_framework.context import ModuleContext, ModuleResult
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.manifest import AgentBrief, AgentManifest

logger = logging.getLogger("orchestrator.agent_framework.modules.compliance_tools")

#: The module's stable registry key.
MODULE_NAME = "compliance_tools"

#: Injectable reader signature: ``(tenant_id: UUID) -> readiness dict`` (ALREADY-resolved tenant —
#: mirrors ``integration_tools_module.StateReaderFn`` / the transport-injection convention). Default
#: ``None`` -> ``gstr_filing_readiness_snapshot``'s own compute path.
ReaderFn = Callable[[UUID], dict[str, Any]]


def _readiness_notes(
    *, gstin_verified: bool, ledger_months_present: list[str], sales_entries_90d: int
) -> list[str]:
    """Honest, structural notes ONLY — never a filing verdict, never a tax-liability estimate.

    TODO(Codex): once GSTR-1 invoice-level fields (HSN/SAC, tax rate, B2B/B2C) and GSTR-3B turnover
    breakups exist, extend the note set to distinguish "GSTR-1 ready" from "GSTR-3B ready" — today
    this is a SINGLE combined readiness check (see the module docstring, extension point #1).
    """
    notes: list[str] = []
    if not gstin_verified:
        notes.append(
            "GSTIN is not verified — verify GST registration before return-filing readiness can "
            "be meaningfully assessed."
        )
    if not ledger_months_present:
        notes.append(
            "No sales recorded in the ledger yet — connect a sales data source (Google Sheets / "
            "Shopify) to build the filing history this check reads."
        )
    elif sales_entries_90d == 0:
        notes.append(
            "No sales recorded in the trailing 90 days — the current filing period may have "
            "nothing to report."
        )
    if gstin_verified and ledger_months_present:
        notes.append(
            f"{len(ledger_months_present)} month(s) of sales-ledger history available to prepare "
            "a return summary from."
        )
    return notes


def _compute_gstr_readiness(tenant_id: UUID) -> dict[str, Any]:
    """The CORE read — takes an ALREADY-RESOLVED tenant (never re-runs ``resolve_lane_tenant``;
    mirrors ``integration_tools_module._read_state``'s "already-resolved authoritative tenant"
    pattern). Composes over the two existing reads named in the module docstring; a miss on either
    degrades to the honest empty/false shape (this is a readiness CHECK — enrichment, never a hard
    failure that would orphan a caller).
    """
    # Lazy: pulls the KG-backed business-context read chain — kept off this module's import surface.
    from orchestrator.knowledge.business_context import read_business_context

    try:
        bc = read_business_context(tenant_id)
        gstin_verified = bool(bc.identity.get("gst_verified"))
    except Exception:  # noqa: BLE001 — a miss degrades to unverified, never a raise
        logger.warning(
            "compliance_tools: business-context read failed (tenant=%s)", tenant_id
        )
        gstin_verified = False

    # Lazy: pulls psycopg — kept off this module's import surface. ``customer_ledger_entries`` is
    # NOT one of the ``check_no_direct_tenant_db_access.py``-gated hot tables (see
    # ``scripts/check_no_direct_tenant_db_access.py``'s ``_TABLES`` list) — direct SQL through
    # ``tenant_connection`` is the SAME sanctioned pattern ``agent.accounting_lane._read_ledger_
    # summary`` uses, not a deviation from it.
    from orchestrator.db.tenant_connection import tenant_connection

    ledger_months_present: list[str] = []
    sales_entries_90d = 0
    try:
        with tenant_connection(tenant_id) as conn:
            month_rows = conn.execute(
                "SELECT DISTINCT to_char(entry_date, 'YYYY-MM') AS ym "
                "FROM customer_ledger_entries WHERE tenant_id = %s AND entry_type = 'sale' "
                "ORDER BY ym",
                (str(tenant_id),),
            ).fetchall()
            count_row = conn.execute(
                "SELECT COUNT(*) AS n FROM customer_ledger_entries "
                "WHERE tenant_id = %s AND entry_type = 'sale' "
                "AND entry_date >= CURRENT_DATE - INTERVAL '90 days'",
                (str(tenant_id),),
            ).fetchone()
        ledger_months_present = [
            r["ym"] if isinstance(r, dict) else r[0] for r in (month_rows or [])
        ]
        if count_row is not None:
            sales_entries_90d = int(
                count_row["n"] if isinstance(count_row, dict) else count_row[0]
            )
    except Exception:  # noqa: BLE001 — ledger read is enrichment; a miss degrades to empty
        logger.warning("compliance_tools: ledger read failed (tenant=%s)", tenant_id)

    return {
        "gstin_verified": gstin_verified,
        "ledger_months_present": ledger_months_present,
        "sales_entries_90d": sales_entries_90d,
        "readiness_notes": _readiness_notes(
            gstin_verified=gstin_verified,
            ledger_months_present=ledger_months_present,
            sales_entries_90d=sales_entries_90d,
        ),
    }


def gstr_filing_readiness_snapshot(tenant_id: str) -> dict[str, Any]:
    """RESOLVE-FIRST public entry (ambient-tenant, model-input UNTRUSTED): the GSTR-1/3B
    return-filing READINESS snapshot for ``tenant_id``.

    Mirrors ``agent_framework.tools_common`` invariant #1 EXACTLY: the first line resolves the
    tenant through ``resolve_lane_tenant`` (the ambient dispatch context wins; a model-supplied
    value that disagrees is logged + ignored — the VT-293/294/599 IDOR guard) and an unresolvable
    tenant returns the structured ``lane_tenant_error`` dict, NEVER a raise (a raise here would
    orphan the caller's tool_use). This is the plain, directly-callable, directly-unit-testable
    function; ``_compliance_tools()`` wraps it as a real langchain ``@tool`` object LAZILY (see the
    module docstring, "IMPORT-LIGHT").

    Returns (Phase-1 advisory/prepare-only — a READINESS check, never a filing, never a tax
    estimate):
      - ``gstin_verified``       — the tenant's GST verification status (``tenants.
        verification_status``, via the SAME verified-tiers boundary ``onboarding_gate`` uses).
      - ``ledger_months_present`` — ``"YYYY-MM"`` strings, ascending, for every month with >=1
        recorded 'sale' ledger entry.
      - ``sales_entries_90d``    — count of 'sale' ledger entries in the trailing 90 days.
      - ``readiness_notes``      — honest, structural notes (never a filing verdict).
    """
    # Lazy: pulls the lane-tenant IDOR-guard seam — kept off this module's import surface.
    from orchestrator.agent.lane_tenant import lane_tenant_error, resolve_lane_tenant

    resolved = resolve_lane_tenant(tenant_id, tool_name="gstr_filing_readiness_snapshot")
    if resolved is None:
        return lane_tenant_error("gstr_filing_readiness_snapshot")
    return _compute_gstr_readiness(resolved)


def _compliance_tools() -> tuple[Any, ...]:
    """The one working example tool, LAZY-wrapped as a real langchain ``@tool`` object (defining
    ``gstr_filing_readiness_snapshot`` as a plain function above and wrapping it ONLY here — not at
    module top — keeps ``import compliance_tools_module`` langchain-free; see the module docstring,
    "IMPORT-LIGHT"). TODO(Codex): append future tools to this tuple as the surface grows (see
    extension point #1/#5 in the module docstring)."""
    from langchain_core.tools import tool

    return (tool(gstr_filing_readiness_snapshot),)


class ComplianceToolsModule:
    """The Compliance specialist SKELETON: a pure ``{PROPOSER}`` framework module carrying ONE
    working read-only tool (``gstr_filing_readiness_snapshot``) on ``manifest.tools``.

    ADVISORY/PREPARE-ONLY by contract (see the module docstring): declares NO gated capability, so
    its proposer-lane ``GateFacade`` is structurally empty — it cannot file, send, spend, or mutate
    business state no matter what Codex adds to ``propose``. The manifest (with its tool surface) is
    built at INSTANCE construction so the FILE stays import-light (mirrors
    ``integration_tools_module`` / ``onboarding_conductor_module`` exactly).
    """

    def __init__(self, *, reader: ReaderFn | None = None) -> None:
        self._reader = reader
        #: Instance-level (not class-level) so importing this file pulls no langchain — only
        #: constructing the module resolves the compliance tool surface (see the module docstring).
        self.manifest = AgentManifest(
            name=MODULE_NAME,
            version="1.0.0",
            roles=frozenset({AgentRole.PROPOSER}),
            description=(
                "Compliance specialist (Phase 1: GST return-filing readiness; later: ROC/AOC/"
                "balance-sheet readiness). PROPOSER: reads the tenant's GST verification status + "
                "sales ledger and reports a GSTR-1/3B filing-READINESS snapshot. ADVISORY/PREPARE-"
                "ONLY — it NEVER files, sends, spends, or mutates business state; real filing is a "
                "later graduation through the capability registry (see compliance.return_filing, "
                "declared disabled)."
            ),
            capabilities=frozenset(
                {Capability.READ_BUSINESS_CONTEXT, Capability.READ_CUSTOMER_LEDGER}
            ),
            # TODO(Codex) extension point #2: a real activation bar (AgentPrerequisites), if this
            # graduates beyond a read-only advisory check. A read is always safe today — no bar.
            prerequisites=None,
            # The one working example tool — the whole point of this skeleton. Deny-list clean
            # (its name holds no forbidden send/write/spend/config-write substring, VT-268); the
            # conformance ``tool_surface_safe`` check re-proves it.
            tools=_compliance_tools(),
            # TODO(Codex) extension point #4: if required_tools sufficiency modeling is wanted,
            # this module's tools must ALSO gain a tool_catalog.py defining-surface entry first.
            required_tools=(),
            # ₹5000/agent SKU declaration (D-ENT, soft-open today — see agent_framework/
            # entitlement.py). TODO(Codex) extension point #3: no action needed until billing wires.
            entitlement_key="compliance_agent",
            # VT-686 — the agent taxonomy: category/tags/brief, written from this module's own
            # docstring above (accurate, no invention; limits mirror the PHASE-1 POSTURE +
            # MCA/ROC BOUNDARY sections verbatim).
            category="Compliance",
            tags=frozenset({"gst", "gstr1", "gstr3b", "returns", "filing-readiness"}),
            brief=AgentBrief(
                what_it_does=(
                    "Reads the tenant's GST verification status and sales ledger and reports a "
                    "GSTR-1/3B filing-READINESS snapshot — whether the data a filing would need "
                    "is present, never whether a return IS filed."
                ),
                actions=("gstr_filing_readiness_snapshot",),
                business_activities=(
                    "check whether a business is ready to file its GST return",
                    "prepare a GST filing-readiness summary",
                ),
                when_to_use=(
                    "Route here when the owner asks about GST return filing, GSTR-1/3B readiness, "
                    "or whether their sales/ledger data is sufficient to file."
                ),
                limits=(
                    "does NOT file GST returns — readiness/prepare-only (Phase 1); real filing is "
                    "a later graduation, declared TODAY as a disabled capability "
                    "(compliance.return_filing)",
                    "no ROC/AOC/balance-sheet/MCA work — there is no MCA integration in this "
                    "codebase; that boundary stays parked until Fazal explicitly un-parks it",
                    "never estimates tax owed or gives a filing verdict — reports data presence "
                    "only (an honest empty, never a guessed readiness verdict)",
                ),
            ),
        )

    def _read(self, tenant_id: UUID) -> dict[str, Any]:
        if self._reader is not None:
            return self._reader(tenant_id)
        return _compute_gstr_readiness(tenant_id)

    def propose(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        """Return the GSTR-1/3B filing-readiness snapshot for ``ctx.tenant_id`` as a proposal.

        ``gate`` is intentionally UNUSED — Phase-1 compliance is ADVISORY/PREPARE-ONLY (see the
        module docstring): this module reads + analyses + prepares, it never reaches an effect.
        The proposer-lane facade is empty and would raise ``CapabilityNotDeclared`` on any gated
        call. Uses ``ctx.tenant_id`` directly (the ALREADY-resolved, IDOR-guarded tenant) — never
        re-runs the tool's own ``resolve_lane_tenant`` (mirrors the sibling modules' pattern).
        """
        snapshot = self._read(ctx.tenant_id)
        return ModuleResult(role=AgentRole.PROPOSER, status="completed", proposal=snapshot)


__all__ = [
    "MODULE_NAME",
    "ComplianceToolsModule",
    "ReaderFn",
    "gstr_filing_readiness_snapshot",
]
