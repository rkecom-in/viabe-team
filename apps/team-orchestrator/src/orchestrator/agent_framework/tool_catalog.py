"""VT-669 — the TOOL CATALOG: the canonical, drift-guarded inventory of every tool surface.

WHAT THIS IS
------------
The framework enforces tool SAFETY (``assert_agent_tools_safe`` deny-list + the positive-capability
manifest) but never had a single INVENTORY of "what tools exist, who holds them, what each is."
~90 ``@tool`` functions grew across 11 surfaces + 3 GateFacade doors with no canonical map. This
module is that map, AS DATA — a ``ToolCatalogEntry`` per tool surface — so the inventory is
version-controlled, diffable, and unit-testable at boot (the same "why code, not a DB table" call
as ``roster.py`` / ``registration.py``). The doc (``docs/agent-framework/TOOLS.md``) is GENERATED
from this registry (``render_catalog_markdown``) so it can never drift from the code.

CODE-FIRST, INTROSPECTION-BACKED (the "can't drift" property)
-------------------------------------------------------------
The catalog does NOT hand-type the tool NAMES. ``catalog_entries()`` IMPORTS the real tool-surface
tuples (lazily) and reads each tool's ``.name`` — the authoritative name is introspected. What IS
hand-authored is the metadata that is NOT introspectable: the ``ToolKind``, the ``Capability`` it
maps to, whether it is ``gated``, its CL-390 ``pii_safe`` posture, its ``tenant_scope``. Each such
annotation is keyed to a (home-surface, name) pair. A companion drift-guard test
(``tests/orchestrator/agent_framework/test_tool_catalog.py``) asserts that EVERY tool in EVERY
``*_TOOLS`` tuple has a catalog entry AND that every annotation maps to a real tool — so adding a
tool to a surface without annotating it FAILS the suite; it can never silently fall out of the map.

IMPORT-LIGHT (deliberate — mirrors the module files' lazy-import discipline)
---------------------------------------------------------------------------
Importing the tool surfaces pulls langchain (and, for the integration surface, a constructed chat
model). So this module's TOP-LEVEL imports stay dep-less (stdlib + ``agent_framework.capabilities``
only); every heavy surface import happens INSIDE ``catalog_entries()`` / ``_holder_labels`` at call
time. ``import orchestrator.agent_framework.tool_catalog`` pulls NO langchain — only calling
``catalog_entries()`` does. This keeps ``import orchestrator.agent_framework`` inert + dep-less-smoke
safe (this module is NOT imported by ``agent_framework/__init__``).

ADDITIVE / INERT (the VT-669 guardrail)
---------------------------------------
The catalog + the ``required_tools`` manifest field + the ``required_tools_reachable`` conformance
check add NO live routing change. The effect DOORS (``GateFacade``) stay the SOLE path to a
consequential action (ARCHITECTURE §2) — the catalog DOCUMENTS them, it never widens them. A
"required tool" for a specialist is NEVER a raw effect; the strongest a catalog entry records is a
``REQUEST_*`` capability (gated), serviced only by the facade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from orchestrator.agent_framework.capabilities import Capability

logger = logging.getLogger("orchestrator.agent_framework.tool_catalog")

#: Sentinel for a metadata value that genuinely cannot be determined without deep analysis. Per the
#: VT-669 brief: mark it UNKNOWN + note, never guess. Used for ``pii_safe`` / ``tenant_scope``.
UNKNOWN = "UNKNOWN"


class ToolKind(str, Enum):
    """The kind of a tool surface. A ``str`` enum so a catalog value serializes to a stable token.

    Classified by the tool's NATURE (what it does), not merely by which surface holds it:

      - ``READ``          — a pure read: counts / statuses / aggregates / the owner's own fields.
      - ``GATED_EFFECT``  — a ``GateFacade`` door that PERFORMS a consequential effect through the
                            deterministic gate (customer send / business action). Gated.
      - ``INTEGRATION``   — a connector tool on the Integration surface (list/read/OAuth/pull-sample/
                            mapping/commit-proposal/schedule/verify) — data-source setup + maintenance.
      - ``ADVISORY``      — a Manager/specialist non-effectful capability: a drafted proposal / a
                            recommendation / a Manager-scoped context write / a parse / an escalate /
                            a pushback. No customer send, no money action, no external mutation.
      - ``EVAL``          — an LLM evaluation/classification tool (self-evaluate / classify).
      - ``SPAWN``         — a handoff tool that spawns a specialist sub-graph (returns a Command).
      - ``DECISION``      — a decision-ONLY door: reports a deterministic gate's decision (or a
                            rail-facing intent probe) WITHOUT executing any effect.
    """

    READ = "read"
    GATED_EFFECT = "gated_effect"
    INTEGRATION = "integration"
    ADVISORY = "advisory"
    EVAL = "eval"
    SPAWN = "spawn"
    DECISION = "decision"


@dataclass(frozen=True)
class ToolCatalogEntry:
    """One tool surface, as data. Frozen value object (an inventory row, like a registry entry).

    Fields:
      - ``name``          — the tool's ``.name`` (introspected from the real tool object).
      - ``surface``       — the module/file the tool is DEFINED in (e.g. ``agent/sales_lane.py``).
      - ``kind``          — the ``ToolKind`` (by nature; see the enum).
      - ``capability``    — the ``agent_framework.Capability`` this tool maps to, or ``None`` for a
                            tool with no positively-declarable capability (advisory / eval / spawn /
                            escalate / a Manager-memory read-write with no capability analogue).
      - ``gated``         — True iff reaching this tool requires a gated (``REQUEST_*``) capability
                            serviced by the ``GateFacade`` (the two effect doors + the decision door).
      - ``pii_safe``      — CL-390: True iff the tool's RETURN carries NO customer PII (counts / ids /
                            statuses / aggregates / the OWNER's own business fields / drafted copy).
                            ``UNKNOWN`` where it genuinely cannot be determined without deep analysis.
      - ``tenant_scope``  — ``"resolved"`` (resolves/scopes to a tenant + opens an RLS-scoped read),
                            ``"n/a"`` (no tenant DB — a registry / pure-reasoning / handoff tool), or
                            ``UNKNOWN``.
      - ``holders``       — the surface label(s) that DECLARE or REACH this tool (e.g.
                            ``("sales_lane", "manager_advisory")`` for a lane tool the Manager also
                            holds in its advisory set). Introspected from the holder surfaces.
      - ``note``          — a short clarifier (why UNKNOWN, a mapping caveat, an escalate/write note).
    """

    name: str
    surface: str
    kind: ToolKind
    capability: Capability | None
    gated: bool
    pii_safe: bool | str
    tenant_scope: str
    holders: tuple[str, ...] = ()
    note: str = ""


# =================================================================================================
# METADATA ANNOTATIONS — the NON-introspectable half, keyed by (home-surface, tool-name).
# =================================================================================================
#
# The tool NAME is introspected (``catalog_entries`` reads ``.name`` off the real object). Everything
# here is what introspection CANNOT tell us: the kind, the capability mapping, the gated flag, the
# CL-390 PII posture, the tenant scope. Grouped by the tool's HOME surface so the ``read_integration_
# state`` name collision (a common-read tool AND an integration-agent tool — two DISTINCT objects
# that share a name) is disambiguated by which surface defines it.


@dataclass(frozen=True)
class _Ann:
    """A per-tool metadata annotation (everything the ``ToolCatalogEntry`` carries except the
    introspected ``name`` / ``surface`` / ``holders``)."""

    kind: ToolKind
    capability: Capability | None = None
    gated: bool = False
    pii_safe: bool | str = True
    tenant_scope: str = "resolved"
    note: str = ""


# Common shorthands.
_C = Capability

# --- home: common_read (agent_framework/tools_common.py) — the Manager-scoped common READ tools ---
_COMMON_READ_ANN: dict[str, _Ann] = {
    "read_customer_ledger_summary": _Ann(ToolKind.READ, _C.READ_CUSTOMER_LEDGER),
    "read_business_context": _Ann(ToolKind.READ, _C.READ_BUSINESS_CONTEXT),
    "read_integration_state": _Ann(ToolKind.READ, _C.READ_INTEGRATION_STATE),
    "read_active_plan": _Ann(
        ToolKind.READ, None,
        note="VT-673: first-class plan/roadmap read (delegates to business_plan store/seams; "
        "owner's own plan data, no customer PII)",
    ),
    "read_agent_memory": _Ann(
        ToolKind.READ, None, tenant_scope="n/a",
        note="VT-674: on-demand L3-prior read (delegates to knowledge.l3_query.lookup_pattern; "
        "180d quarantine + k>=10 anonymized aggregates structural — cross-tenant global table, "
        "resolved tenant used ONLY for the quarantine check)",
    ),
    # VT-675 promotions — resolve-first @tool WRAPPERS over the agent/tools payload functions (the
    # raw functions take a model-supplied payload.tenant_id — promoting them verbatim would be the
    # VT-293/294/599 IDOR class; the wrappers resolve the ambient tenant then delegate).
    # get_recent_campaigns gains its FIRST catalog entry here — the raw fn was on no swept surface.
    "get_recent_campaigns": _Ann(
        ToolKind.READ, None,
        note="VT-675 promoted (resolve-first wrapper): recent-campaign rollup (counts/statuses "
        "only, CL-390)",
    ),
    "get_attribution_data": _Ann(
        ToolKind.READ, None,
        note="VT-675 promoted (resolve-first wrapper): attribution rollup (counts/aggregates)",
    ),
    "query_customer_ledger": _Ann(
        ToolKind.READ, None,
        note="VT-675 promoted (resolve-first wrapper): operator-role ledger read (phone-token "
        "input, customer_id UUIDs + amounts out — never name/phone/email; scope unchanged, "
        "CL-82/CL-390)",
    ),
}

# --- home: common_advisory (agent_framework/tools_common.py) — the common hand-back tools ---------
_COMMON_ADVISORY_ANN: dict[str, _Ann] = {
    "escalate": _Ann(
        ToolKind.ADVISORY, None, tenant_scope="n/a",
        note="VT-672: the ONE common escalate — a specialist hands a decision back to the Manager "
        "(§1.2 owner-comms stays Manager-only); no external effect, no DB write. The Manager's own "
        "escalate_to_fazal terminal signal is separate and untouched.",
    ),
}

# --- home: integration_agent (agent/integration_agent.py) — the 11 VT-608 connector tools ---------
_INTEGRATION_ANN: dict[str, _Ann] = {
    "list_supported_connectors": _Ann(
        ToolKind.INTEGRATION, _C.READ_INTEGRATION_STATE, tenant_scope="n/a",
        note="static supported-connector registry read; no tenant DB",
    ),
    "read_integration_state": _Ann(ToolKind.INTEGRATION, _C.READ_INTEGRATION_STATE),
    "start_oauth": _Ann(
        ToolKind.INTEGRATION, _C.PROPOSE_CONFIG_CHANGE,
        note="OAuth link-out (a proposal/hand-off, not a config write)",
    ),
    "check_oauth_status": _Ann(ToolKind.INTEGRATION, _C.READ_INTEGRATION_STATE),
    "pull_sample": _Ann(
        ToolKind.INTEGRATION, _C.READ_INTEGRATION_STATE,
        note="VT-268: counts-only sample summary (no raw customer rows returned)",
    ),
    "propose_mapping": _Ann(ToolKind.INTEGRATION, _C.PROPOSE_CONFIG_CHANGE),
    "confirm_mapping": _Ann(ToolKind.INTEGRATION, _C.PROPOSE_CONFIG_CHANGE),
    "commit_ingestion": _Ann(
        ToolKind.INTEGRATION, _C.PROPOSE_CONFIG_CHANGE,
        note="VT-268: PROPOSAL only — the ingest WRITE is the module-external deterministic executor",
    ),
    "schedule_recurring_pull": _Ann(
        ToolKind.INTEGRATION, _C.PROPOSE_CONFIG_CHANGE,
        note="cadence CONFIG staging (VT-210 accepted precedent); non-gated",
    ),
    "verify_connector": _Ann(ToolKind.INTEGRATION, _C.READ_INTEGRATION_STATE),
    "integration_escalate_to_fazal": _Ann(
        ToolKind.ADVISORY, None, tenant_scope="n/a",
        note="ops escalation to Fazal (integration surface); no external effect",
    ),
}

# --- home: onboarding_conductor (agent/onboarding_conductor.py) — the 10 conductor tools ----------
_ONBOARDING_ANN: dict[str, _Ann] = {
    "read_onboarding_state": _Ann(ToolKind.READ, None),
    "extract_owner_answer": _Ann(
        ToolKind.ADVISORY, None, tenant_scope="n/a",
        note="parses a structured answer from the owner's message; no DB",
    ),
    "record_answer": _Ann(
        ToolKind.ADVISORY, None,
        note="onboarding-profile WRITE (owner's own data) — non-gated, no customer send",
    ),
    "record_skip": _Ann(
        ToolKind.ADVISORY, None, note="onboarding-profile WRITE (skip marker) — non-gated",
    ),
    "apply_correction": _Ann(
        ToolKind.ADVISORY, None, note="onboarding-profile WRITE (correction) — non-gated",
    ),
    "next_required_question": _Ann(ToolKind.READ, None),
    "profile_completion_check": _Ann(ToolKind.READ, None),
    "activation_check": _Ann(ToolKind.READ, None),
    "propose_business_policy": _Ann(
        ToolKind.ADVISORY, None,
        note="drafts a business-policy PROPOSAL (owner confirms the bound); no effect",
    ),
    "conductor_escalate_to_fazal": _Ann(
        ToolKind.ADVISORY, None, tenant_scope="n/a",
        note="ops escalation to Fazal (onboarding surface); no external effect",
    ),
}

# --- home: orchestrator_agent (agent/orchestrator_agent.py) — the Manager's OWN base tool set -----
_ORCHESTRATOR_ANN: dict[str, _Ann] = {
    "escalate_to_fazal": _Ann(
        ToolKind.ADVISORY, None, tenant_scope="n/a",
        note="ops escalation to Fazal; no external effect",
    ),
    "write_l0_fragment": _Ann(
        ToolKind.ADVISORY, None,
        note="Manager-scoped context WRITE (L0 memory) — non-gated, no external effect (VT-268 benign)",
    ),
    "query_l0": _Ann(ToolKind.READ, None, note="Manager L0-memory read"),
    "record_business_objective": _Ann(
        ToolKind.ADVISORY, None,
        note="Manager-scoped context WRITE (business_objective, VT-466) — non-gated",
    ),
    "set_language_preference": _Ann(
        ToolKind.ADVISORY, None,
        note="VT-677: the owner's EXPLICIT language choice (preferred_language write, D3 verbal "
        "override) — non-gated owner-own-preference write; never affects live-turn mirroring (D2)",
    ),
    "export_customer_list": _Ann(
        ToolKind.ADVISORY, None,
        note="VT-676 F3: delivers the owner's OWN customer list as a WhatsApp CSV to the VERIFIED "
        "owner (send_customer_list_to_owner: server-derived recipient, private bucket, 300s URL, "
        "audit) — OWNER-comms delivery, not a customer send; Manager-only holder (§1.2)",
    ),
    "search_conversation_history": _Ann(
        ToolKind.READ, None,
        note="owner<->assistant conversation-log retrieval (owner-authored text; not customer rows)",
    ),
}

# --- home: sales_lane (agent/sales_lane.py) -------------------------------------------------------
_SALES_LANE_ANN: dict[str, _Ann] = {
    "recommend_sales_play": _Ann(
        ToolKind.ADVISORY, None, note="drafts a sales-play recommendation (intent only; no send)",
    ),
    "identify_repeat_upsell_opportunity": _Ann(
        ToolKind.READ, None, tenant_scope="n/a", note="pure reasoning-grounding read; no DB, no effect",
    ),
    "push_back_to_manager": _Ann(
        ToolKind.ADVISORY, None, tenant_scope="n/a",
        note="specialist->manager pushback protocol; no effect (excluded from ADVISORY_TOOLS)",
    ),
    "sales_lane_escalate_to_fazal": _Ann(
        ToolKind.ADVISORY, None, tenant_scope="n/a",
        note="ops escalation to Fazal (excluded from ADVISORY_TOOLS as redundant); no effect",
    ),
}

# --- home: marketing_lane (agent/marketing_lane.py) -----------------------------------------------
_MARKETING_LANE_ANN: dict[str, _Ann] = {
    "list_recent_campaigns": _Ann(ToolKind.READ, None, note="read-only rollup (counts only, CL-390)"),
    "draft_campaign_plan": _Ann(ToolKind.ADVISORY, None, note="drafts a campaign/offer intent; no send"),
    "draft_content": _Ann(ToolKind.ADVISORY, None, note="drafts content copy; no send"),
    "check_send_intent": _Ann(
        ToolKind.DECISION, None,
        note="rail-facing probe: reports the CUSTOMER_SEND policy bound; sends nothing (non-gated)",
    ),
    "check_ad_spend_intent": _Ann(
        ToolKind.DECISION, None,
        note="rail-facing probe: reports the SPEND business-impact gate; spends nothing (non-gated)",
    ),
    "marketing_escalate_to_fazal": _Ann(
        ToolKind.ADVISORY, None, tenant_scope="n/a",
        note="ops escalation to Fazal (excluded from ADVISORY_TOOLS as redundant); no effect",
    ),
}

# --- home: finance_lane (agent/finance_lane.py) ---------------------------------------------------
_FINANCE_LANE_ANN: dict[str, _Ann] = {
    "analyze_cash_flow": _Ann(ToolKind.READ, None, note="read-only aggregate"),
    "analyze_receivables": _Ann(ToolKind.READ, None, note="read-only aggregate"),
    "pricing_margin_input": _Ann(ToolKind.READ, None, note="read-only aggregate"),
    "propose_payment_reminder": _Ann(
        ToolKind.ADVISORY, None, note="drafts a reminder PROPOSAL; no send/persist",
    ),
    "finance_pushback": _Ann(
        ToolKind.ADVISORY, None, tenant_scope="n/a",
        note="specialist->manager pushback protocol; no effect (excluded from ADVISORY_TOOLS)",
    ),
    "finance_escalate_to_fazal": _Ann(
        ToolKind.ADVISORY, None, tenant_scope="n/a",
        note="ops escalation to Fazal (excluded from ADVISORY_TOOLS as redundant); no effect",
    ),
}

# --- home: accounting_lane (agent/accounting_lane.py) — v1 PREPARE-only by charter -----------------
_ACCOUNTING_LANE_ANN: dict[str, _Ann] = {
    "accounting_categorize_books": _Ann(ToolKind.ADVISORY, None, note="prepares categorization; no write"),
    "accounting_prepare_tax_summary": _Ann(ToolKind.ADVISORY, None, note="prepares a tax summary; no write"),
    "accounting_organize_invoices_expenses": _Ann(
        ToolKind.ADVISORY, None, note="prepares an organization; no write",
    ),
    "accounting_reconcile_transactions": _Ann(
        ToolKind.ADVISORY, None, note="prepares a reconciliation; no write",
    ),
    "accounting_escalate_to_fazal": _Ann(
        ToolKind.ADVISORY, None, tenant_scope="n/a",
        note="ops escalation to Fazal (excluded from ADVISORY_TOOLS as redundant); no effect",
    ),
}

# --- home: tech_lane (agent/tech_lane.py) ---------------------------------------------------------
_TECH_LANE_ANN: dict[str, _Ann] = {
    "read_integration_health": _Ann(ToolKind.READ, None, note="read-only (tenant_connector_status)"),
    "read_listing_health": _Ann(ToolKind.READ, None, note="read-only (platform_listings)"),
    "advise_integration_setup": _Ann(
        ToolKind.ADVISORY, None, tenant_scope="n/a",
        note="read-only registry advice (owner-visible connector catalogue only)",
    ),
    "read_tech_context": _Ann(ToolKind.READ, None, note="read-only (business_context slice)"),
    "propose_config_change": _Ann(ToolKind.ADVISORY, _C.PROPOSE_CONFIG_CHANGE, note="drafts a config intent; no write"),
    "check_config_change_intent": _Ann(
        ToolKind.DECISION, None,
        note="rail-facing probe: reports the CONFIG business-impact gate; writes nothing (non-gated)",
    ),
    "tech_escalate_to_fazal": _Ann(
        ToolKind.ADVISORY, None, tenant_scope="n/a",
        note="ops escalation to Fazal (excluded from ADVISORY_TOOLS as redundant); no effect",
    ),
}

# --- home: cost_opt_lane (agent/cost_opt_lane.py) — v1 ADVISE-only by charter ----------------------
_COST_OPT_LANE_ANN: dict[str, _Ann] = {
    "analyze_tenant_spend": _Ann(ToolKind.READ, None, note="read-only aggregate"),
    "analyze_unit_economics": _Ann(ToolKind.READ, None, note="read-only aggregate"),
    "identify_spend_anomaly": _Ann(ToolKind.READ, None, note="read-only aggregate"),
    "analyze_marketing_roi": _Ann(ToolKind.READ, None, note="read-only aggregate"),
    "read_cost_context": _Ann(ToolKind.READ, None, note="read-only (business_context slice)"),
}

# --- home: agent_tools (agent/tools/*) — the standalone tool functions -----------------------------
_AGENT_TOOLS_ANN: dict[str, _Ann] = {
    "query_customer_ledger": _Ann(
        ToolKind.READ, _C.READ_CUSTOMER_LEDGER,
        note="returns customer_id + amounts/dates/notes — no name/phone column (CL-390)",
    ),
    "get_attribution_data": _Ann(ToolKind.READ, None, note="attribution rollup (counts/aggregates)"),
    "get_business_profile": _Ann(
        ToolKind.READ, _C.READ_BUSINESS_CONTEXT, note="owner's own business profile",
    ),
    "schedule_followup": _Ann(
        ToolKind.ADVISORY, None,
        note="schedules an internal followup (non-gated write; no customer send)",
    ),
    "classify_owner_message": _Ann(
        ToolKind.EVAL, None, tenant_scope="n/a",
        note="LLM classification of the owner's message; no DB, no effect",
    ),
    "self_evaluate": _Ann(
        ToolKind.EVAL, None, tenant_scope="n/a",
        note="LLM self-evaluation of a proposal (VT-36); returns a verdict, no effect",
    ),
}

# --- home: roster (agent/roster.py) — the specialist spawn (handoff) tools -------------------------
_SPAWN_ANN: dict[str, _Ann] = {
    "spawn_sales_recovery": _Ann(
        ToolKind.SPAWN, None, tenant_scope="n/a",
        note="handoff to the Sales-Recovery specialist sub-graph; returns a Command",
    ),
    "spawn_integration": _Ann(
        ToolKind.SPAWN, None, tenant_scope="n/a",
        note="handoff to the Integration specialist sub-graph; returns a Command",
    ),
    "spawn_onboarding_conductor": _Ann(
        ToolKind.SPAWN, None, tenant_scope="n/a",
        note="handoff to the Onboarding-Conductor specialist sub-graph; returns a Command",
    ),
}

# --- home: gate_facade (agent_framework/gate_facade.py) — the effect + decision DOORS --------------
_GATE_FACADE_ANN: dict[str, _Ann] = {
    "request_customer_send": _Ann(
        ToolKind.GATED_EFFECT, _C.REQUEST_CUSTOMER_SEND, gated=True,
        note="the SOLE customer-send door; routes to customer_send.agent_send_draft (Gate 0..5)",
    ),
    "perform_business_action": _Ann(
        ToolKind.GATED_EFFECT, _C.REQUEST_BUSINESS_ACTION, gated=True,
        note="whole-round-trip business-action door (classify + issue-inside-choke, ARCHITECTURE §2)",
    ),
    "gate_business_action": _Ann(
        ToolKind.DECISION, _C.REQUEST_BUSINESS_ACTION, gated=True,
        note="decision-ONLY door: returns the gate decision (issues no effect)",
    ),
}


# =================================================================================================
# SURFACE DESCRIPTORS — the DEFINING surfaces (introspected for names) + HOLDER surfaces (for
# computing which module(s) declare/reach a tool). Every loader is LAZY (imports happen at call).
# =================================================================================================


@dataclass(frozen=True)
class _DefiningSurface:
    """A surface that DEFINES tools. ``loader`` lazily imports and returns the tool tuple; ``home``
    is the annotation group; ``annotations`` is the (name -> _Ann) map for that surface."""

    home: str
    annotations: dict[str, _Ann]
    loader: Any  # () -> Iterable[tool objects]


def _load(modpath: str, sym: str) -> Any:
    """Lazily import ``sym`` from ``modpath`` and return it (a tuple/list of tool objects)."""

    def _loader() -> Any:
        module = __import__(modpath, fromlist=[sym])
        return getattr(module, sym)

    return _loader


def _load_agent_tools() -> list[Any]:
    """The standalone ``agent/tools/*`` tool functions (+ the self_evaluate MCPTool). Lazy — each
    pulls its own deps (pydantic IO / the MCP framework)."""
    from orchestrator.agent.tools.classify_owner_message import classify_owner_message
    from orchestrator.agent.tools.get_attribution_data import get_attribution_data
    from orchestrator.agent.tools.get_business_profile import get_business_profile
    from orchestrator.agent.tools.query_customer_ledger import query_customer_ledger
    from orchestrator.agent.tools.schedule_followup import schedule_followup
    from orchestrator.agent.tools.self_evaluate import SelfEvaluateTool

    return [
        query_customer_ledger,
        get_attribution_data,
        get_business_profile,
        schedule_followup,
        classify_owner_message,
        SelfEvaluateTool,  # class carries ``name = "self_evaluate"`` (MCPTool, not a langchain tool)
    ]


def _spawn_tool_stubs() -> list[Any]:
    """The three specialist spawn tools, as name-only stubs. Unlike the tuple surfaces these are
    BUILT per-graph (``supervisor.build_supervisor_graph`` passes them as ``extra_tools``), so there
    is no importable tuple to introspect. The stable spawn-tool NAMES live on ``roster.ROSTER``
    (``SpecialistSpec.spawn_tool_name``) — we read them from there so this stays drift-guarded
    against the roster rather than hand-typed."""
    from orchestrator.agent.roster import ROSTER

    return [
        _NamedStub(spec.spawn_tool_name, "orchestrator.agent.roster")
        for spec in ROSTER
        if spec.spawn_tool_name
    ]


class _NamedStub:
    """A minimal ``.name``-carrying stand-in for a tool with no importable object (spawn / facade
    doors). Lets the introspection path treat every surface uniformly (read ``.name``)."""

    __slots__ = ("name", "_module")

    def __init__(self, name: str, module: str = "") -> None:
        self.name = name
        self._module = module


def _gate_facade_door_stubs() -> list[Any]:
    """The three GateFacade doors, as name-only stubs (they are METHODS, not tools in a tuple)."""
    return [
        _NamedStub("request_customer_send", "orchestrator.agent_framework.gate_facade"),
        _NamedStub("perform_business_action", "orchestrator.agent_framework.gate_facade"),
        _NamedStub("gate_business_action", "orchestrator.agent_framework.gate_facade"),
    ]


#: The DEFINING surfaces, in a stable order (each tool is defined in exactly ONE of these — the
#: surfaces are disjoint by object identity; the ``read_integration_state`` NAME appears in two but
#: they are two DISTINCT objects, one per home).
def _defining_surfaces() -> list[_DefiningSurface]:
    return [
        _DefiningSurface(
            "common_read", _COMMON_READ_ANN,
            _load("orchestrator.agent_framework.tools_common", "COMMON_READ_TOOLS"),
        ),
        _DefiningSurface(
            "common_advisory", _COMMON_ADVISORY_ANN,
            _load("orchestrator.agent_framework.tools_common", "COMMON_ADVISORY_TOOLS"),
        ),
        _DefiningSurface(
            "integration_agent", _INTEGRATION_ANN,
            _load("orchestrator.agent.integration_agent", "INTEGRATION_AGENT_TOOLS"),
        ),
        _DefiningSurface(
            "onboarding_conductor", _ONBOARDING_ANN,
            _load("orchestrator.agent.onboarding_conductor", "ONBOARDING_CONDUCTOR_TOOLS"),
        ),
        _DefiningSurface(
            "orchestrator_agent", _ORCHESTRATOR_ANN,
            _load("orchestrator.agent.orchestrator_agent", "ORCHESTRATOR_AGENT_TOOLS"),
        ),
        _DefiningSurface(
            "sales_lane", _SALES_LANE_ANN,
            _load("orchestrator.agent.sales_lane", "SALES_LANE_TOOLS"),
        ),
        _DefiningSurface(
            "marketing_lane", _MARKETING_LANE_ANN,
            _load("orchestrator.agent.marketing_lane", "MARKETING_LANE_TOOLS"),
        ),
        _DefiningSurface(
            "finance_lane", _FINANCE_LANE_ANN,
            _load("orchestrator.agent.finance_lane", "FINANCE_LANE_TOOLS"),
        ),
        _DefiningSurface(
            "accounting_lane", _ACCOUNTING_LANE_ANN,
            _load("orchestrator.agent.accounting_lane", "ACCOUNTING_LANE_TOOLS"),
        ),
        _DefiningSurface(
            "tech_lane", _TECH_LANE_ANN,
            _load("orchestrator.agent.tech_lane", "TECH_LANE_TOOLS"),
        ),
        _DefiningSurface(
            "cost_opt_lane", _COST_OPT_LANE_ANN,
            _load("orchestrator.agent.cost_opt_lane", "COST_OPT_LANE_TOOLS"),
        ),
        _DefiningSurface("agent_tools", _AGENT_TOOLS_ANN, _load_agent_tools),
        _DefiningSurface("roster", _SPAWN_ANN, _spawn_tool_stubs),
        _DefiningSurface("gate_facade", _GATE_FACADE_ANN, _gate_facade_door_stubs),
    ]


#: The HOLDER surfaces — who DECLARES or REACHES a tool at runtime. A tool's ``holders`` is every
#: label here whose collection contains that tool object (by identity), so a lane tool the Manager
#: also carries in ADVISORY_TOOLS shows BOTH holders. Lazy loaders (imports at call).
def _holder_surfaces() -> list[tuple[str, Any]]:
    return [
        ("manager_common_read", _load("orchestrator.agent_framework.tools_common", "COMMON_READ_TOOLS")),
        ("integration_specialist", _load("orchestrator.agent.integration_agent", "INTEGRATION_AGENT_TOOLS")),
        ("onboarding_specialist", _load("orchestrator.agent.onboarding_conductor", "ONBOARDING_CONDUCTOR_TOOLS")),
        ("manager_core", _load("orchestrator.agent.orchestrator_agent", "ORCHESTRATOR_AGENT_TOOLS")),
        ("manager_advisory", _load("orchestrator.agent.advisory_registry", "ADVISORY_TOOLS")),
        ("sales_lane", _load("orchestrator.agent.sales_lane", "SALES_LANE_TOOLS")),
        ("marketing_lane", _load("orchestrator.agent.marketing_lane", "MARKETING_LANE_TOOLS")),
        ("finance_lane", _load("orchestrator.agent.finance_lane", "FINANCE_LANE_TOOLS")),
        ("accounting_lane", _load("orchestrator.agent.accounting_lane", "ACCOUNTING_LANE_TOOLS")),
        ("tech_lane", _load("orchestrator.agent.tech_lane", "TECH_LANE_TOOLS")),
        ("cost_opt_lane", _load("orchestrator.agent.cost_opt_lane", "COST_OPT_LANE_TOOLS")),
    ]


# =================================================================================================
# INTROSPECTION ENGINE
# =================================================================================================


def _tool_name(tool: Any) -> str:
    """The tool's ``.name`` (langchain BaseTool / MCPTool / our stubs), else the callable's
    ``__name__`` (the standalone ``agent/tools/*`` functions), else ``repr``."""
    name = getattr(tool, "name", None)
    if isinstance(name, str) and name:
        return name
    fn_name = getattr(tool, "__name__", None)
    if isinstance(fn_name, str) and fn_name:
        return fn_name
    return repr(tool)


def _surface_file(tool: Any, home: str) -> str:
    """The ``agent/foo.py``-style source file a tool is defined in. Derived from the tool's
    ``__module__`` (unwrapping a langchain ``@tool``'s ``.func``); falls back to the home label."""
    stub_module = getattr(tool, "_module", None)  # a name-only stub carries its home module explicitly
    obj = getattr(tool, "func", tool)  # langchain @tool wraps the fn at ``.func``
    module = stub_module or getattr(obj, "__module__", None) or getattr(getattr(obj, "__class__", None), "__module__", "")
    if isinstance(module, str) and module.startswith("orchestrator."):
        return module[len("orchestrator."):].replace(".", "/") + ".py"
    return f"<{home}>"


def _holder_labels(tool: Any) -> tuple[str, ...]:
    """The holder-surface labels whose collection contains ``tool`` (by object identity)."""
    labels: list[str] = []
    for label, loader in _holder_surfaces():
        try:
            surface_tools = loader()
        except Exception as exc:  # noqa: BLE001 — a holder surface that fails to import is skipped.
            logger.warning("tool_catalog: holder surface %r failed to load: %s", label, exc)
            continue
        if any(tool is candidate for candidate in surface_tools):
            labels.append(label)
    return tuple(labels)


def catalog_entries() -> tuple[ToolCatalogEntry, ...]:
    """Build the full catalog by INTROSPECTING every tool surface (lazily) and joining each tool's
    introspected ``.name`` with its hand-authored ``_Ann`` metadata.

    A tool present on a surface but MISSING an annotation is still emitted — as an ``UNKNOWN`` entry
    with a ``note`` — so the catalog never silently drops a tool; the drift-guard test asserts there
    are ZERO such entries (adding a tool without annotating it fails the suite).
    """
    entries: list[ToolCatalogEntry] = []
    for surface in _defining_surfaces():
        try:
            tools = list(surface.loader())
        except Exception as exc:  # noqa: BLE001 — a surface that fails to import is recorded, not fatal.
            logger.warning("tool_catalog: defining surface %r failed to load: %s", surface.home, exc)
            continue
        # Holders only make sense for real objects; the name-only stubs (spawn / facade doors) carry
        # their holder semantics in the annotation ``note`` (they are not in any holder tuple).
        real_objects = surface.home not in {"roster", "gate_facade"}
        for tool in tools:
            name = _tool_name(tool)
            ann = surface.annotations.get(name)
            holders = _holder_labels(tool) if real_objects else ()
            if ann is None:
                entries.append(
                    ToolCatalogEntry(
                        name=name,
                        surface=_surface_file(tool, surface.home),
                        kind=ToolKind.READ,  # placeholder; the note flags it as unannotated.
                        capability=None,
                        gated=False,
                        pii_safe=UNKNOWN,
                        tenant_scope=UNKNOWN,
                        holders=holders,
                        note=f"NO ANNOTATION for {surface.home}:{name} — add one (drift-guard should fail)",
                    )
                )
                continue
            entries.append(
                ToolCatalogEntry(
                    name=name,
                    surface=_surface_file(tool, surface.home),
                    kind=ann.kind,
                    capability=ann.capability,
                    gated=ann.gated,
                    pii_safe=ann.pii_safe,
                    tenant_scope=ann.tenant_scope,
                    holders=holders,
                    note=ann.note,
                )
            )
    return tuple(entries)


def catalog_by_name() -> dict[str, list[ToolCatalogEntry]]:
    """The catalog grouped by tool name (a list per name — a name may resolve to >1 surface, e.g.
    ``read_integration_state`` on both the common-read and the integration surfaces)."""
    out: dict[str, list[ToolCatalogEntry]] = {}
    for e in catalog_entries():
        out.setdefault(e.name, []).append(e)
    return out


def catalog_tool_names() -> frozenset[str]:
    """The set of every tool NAME in the catalog (across all surfaces)."""
    return frozenset(e.name for e in catalog_entries())


def catalog_coverage_gaps() -> dict[str, list[str]]:
    """The DRIFT GUARD: compare each defining surface's INTROSPECTED tool names against its
    hand-authored annotation keys, BOTH directions. Returns ``{"unannotated": [...], "stale": [...]}``:

      - ``unannotated`` — a tool is present on a surface but has NO ``_Ann`` (adding a tool without
        annotating it → it lands here). The drift-guard test asserts this is EMPTY.
      - ``stale``       — an annotation exists for a name no longer on its surface (removing/renaming
        a tool → its old annotation lands here). The test asserts this is EMPTY too.

    This is what makes the catalog "can't drift": the doc is generated from the catalog, and the
    catalog's annotations are pinned to the real tool surfaces in both directions.
    """
    unannotated: list[str] = []
    stale: list[str] = []
    for surface in _defining_surfaces():
        try:
            introspected = {_tool_name(t) for t in surface.loader()}
        except Exception as exc:  # noqa: BLE001 — a surface that fails to load is reported, not fatal.
            logger.warning("tool_catalog: coverage surface %r failed to load: %s", surface.home, exc)
            continue
        annotated = set(surface.annotations)
        unannotated.extend(f"{surface.home}:{n}" for n in sorted(introspected - annotated))
        stale.extend(f"{surface.home}:{n}" for n in sorted(annotated - introspected))
    return {"unannotated": unannotated, "stale": stale}


# =================================================================================================
# CAPABILITY GAPS — the SUFFICIENCY frontier (VT-669, "fail loudly", Fazal 2026-07-18).
# =================================================================================================
#
# ``catalog_coverage_gaps`` (above) is the DRIFT guard: does every tool that EXISTS have an
# annotation. This is a DIFFERENT axis: the common-tool ACTION surface a sub-agent depends on is not
# yet COMPLETE. Fazal's ruling: don't paper the holes over by giving each specialist a required set
# trimmed to only-what's-reachable (that makes every check green and HIDES the missing capability).
# Name the holes, make them machine-visible, and fail a dedicated gate LOUD until each is built.
#
# These are CAPABILITY holes, not drift: the tool does not exist yet (escalate/plan/memory reads) OR
# an existing tool is not yet promoted into the Manager-scoped COMMON set (the richer reads). Each is
# tracked to a board row (VT-672..675) and surfaced by ``scripts/check_capability_gaps.py`` (exit 1
# while any is open) + rendered into TOOLS.md. A gap AUTO-CLOSES: once its tool is cataloged (or, for
# a promotion, appears on the common-read surface), ``open_capability_gaps`` stops returning it — and
# the registry-honesty test then fails RED until the now-inert entry is deleted, so a closed gap can
# never silently linger. NONE of this is on a live routing path; it is a spec + a report.


class GapKind(str, Enum):
    """How a capability gap is DETECTED as closed.

    - ``ABSENT_FROM_CATALOG`` — the gap tool does not exist on ANY surface yet. Closed once ANY of
      its ``probe_names`` appears in the catalog (the tool got built + annotated).
    - ``ABSENT_FROM_COMMON``  — the tool(s) EXIST (already cataloged as lane tools) but are not in
      the Manager-scoped COMMON read set. Closed once EVERY ``probe_name`` is a common-read tool.
    """

    ABSENT_FROM_CATALOG = "absent_from_catalog"
    ABSENT_FROM_COMMON = "absent_from_common"


@dataclass(frozen=True)
class CapabilityGap:
    """A named hole in the common-tool ACTION surface a sub-agent's job depends on.

    Fields:
      - ``key``          — short stable id (for the gate/report + the honesty test).
      - ``title``        — one-line human name.
      - ``kind``         — the ``GapKind`` (how closure is detected).
      - ``probe_names``  — the tool NAME(s) whose presence marks the gap CLOSED (per ``kind``).
      - ``needed_by``    — the specialist(s) whose job this capability serves.
      - ``reason``       — why it is a gap today (what's missing / mis-shaped).
      - ``followon_vt``  — the board row that builds it (the "onto the board" anchor).
    """

    key: str
    title: str
    kind: GapKind
    probe_names: tuple[str, ...]
    needed_by: tuple[str, ...]
    reason: str
    followon_vt: str


#: The known holes in the common-tool surface, as of VT-669 (Fazal 2026-07-18). Each is on the board.
#: The 4 gaps registered 2026-07-18 (Fazal "fail loudly") were ALL BUILT the same day — the registry
#: is empty until the next capability hole is named. Register future holes here (each with a board
#: row); the gate + honesty test re-arm automatically on the first entry.
KNOWN_CAPABILITY_GAPS: tuple[CapabilityGap, ...] = (
    # unified_escalate (VT-672): CLOSED 2026-07-18 — common `escalate` built into
    # COMMON_ADVISORY_TOOLS (the Manager's escalate_to_fazal terminal stays separate); entry
    # deleted per the registry-honesty test.
    # plan_roadmap_read (VT-673): CLOSED 2026-07-18 — `read_active_plan` built into
    # COMMON_READ_TOOLS (delegates to business_plan store/seams); entry deleted per the
    # registry-honesty test.
    # on_demand_memory_read (VT-674): CLOSED 2026-07-18 — `read_agent_memory` built into
    # COMMON_READ_TOOLS (delegates to knowledge.l3_query.lookup_pattern: quarantine + k-anon
    # structural); entry deleted per the registry-honesty test.
    # richer_reads_into_common (VT-675): CLOSED 2026-07-18 — get_recent_campaigns /
    # get_attribution_data / query_customer_ledger promoted onto COMMON_READ_TOOLS (same objects,
    # scope unchanged); entry deleted per the registry-honesty test.
)


def _common_read_names() -> frozenset[str]:
    """The tool names on the Manager-scoped common READ surface (lazy — pulls langchain)."""
    from orchestrator.agent_framework.tools_common import COMMON_READ_TOOLS

    return frozenset(_tool_name(t) for t in COMMON_READ_TOOLS)


def _gap_is_open(gap: CapabilityGap, catalog_names: frozenset[str], common_names: frozenset[str]) -> bool:
    """True iff the gap is UNRESOLVED given the current catalog + common-read surface."""
    if gap.kind is GapKind.ABSENT_FROM_CATALOG:
        # Closed once the tool has been BUILT (any probe name now cataloged).
        return not any(n in catalog_names for n in gap.probe_names)
    if gap.kind is GapKind.ABSENT_FROM_COMMON:
        # Closed once EVERY probe tool is promoted onto the common-read surface.
        return not all(n in common_names for n in gap.probe_names)
    return True  # unknown kind → treat as open (fail loud, never silently closed)


def open_capability_gaps() -> tuple[CapabilityGap, ...]:
    """The subset of ``KNOWN_CAPABILITY_GAPS`` still UNRESOLVED against the live catalog + common set.

    A gap auto-drops here the moment its tool is built/promoted — so this is the honest, self-updating
    'what's still missing' list. ``scripts/check_capability_gaps.py`` exits non-zero while this is
    non-empty; the registry-honesty test fails if a gap has closed but its entry was not deleted.
    """
    catalog_names = catalog_tool_names()
    common_names = _common_read_names()
    return tuple(g for g in KNOWN_CAPABILITY_GAPS if _gap_is_open(g, catalog_names, common_names))


# =================================================================================================
# DOC GENERATION — TOOLS.md is produced FROM the catalog (never hand-maintained).
# =================================================================================================


def render_catalog_markdown() -> str:
    """Render ``docs/agent-framework/TOOLS.md`` content from the catalog. GENERATED — do not
    hand-edit the doc; edit the catalog annotations + regenerate."""
    entries = sorted(catalog_entries(), key=lambda e: (e.kind.value, e.surface, e.name))
    total = len(entries)
    gated = sum(1 for e in entries if e.gated)
    by_kind: dict[str, int] = {}
    for e in entries:
        by_kind[e.kind.value] = by_kind.get(e.kind.value, 0) + 1

    lines: list[str] = [
        "# Tool Catalog (`agent_framework.tool_catalog`)",
        "",
        "> GENERATED from `orchestrator.agent_framework.tool_catalog` by `render_catalog_markdown()`.",
        "> Do NOT hand-edit — edit the catalog annotations and regenerate. ARCHITECTURE §1.3.",
        "",
        f"**{total} tool surfaces** across the roster — {gated} gated (GateFacade doors). "
        + ", ".join(f"{k}: {n}" for k, n in sorted(by_kind.items())),
        "",
        "| Tool | Surface | Kind | Capability | Gated | PII-safe | Tenant | Holders | Note |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for e in entries:
        cap = e.capability.value if e.capability is not None else "—"
        holders = ", ".join(e.holders) if e.holders else "—"
        pii = e.pii_safe if isinstance(e.pii_safe, str) else ("yes" if e.pii_safe else "NO")
        lines.append(
            f"| `{e.name}` | `{e.surface}` | {e.kind.value} | {cap} | "
            f"{'yes' if e.gated else 'no'} | {pii} | {e.tenant_scope} | {holders} | {e.note} |"
        )

    # SUFFICIENCY frontier — the OPEN capability gaps (VT-669, Fazal "fail loudly"). Rendered so the
    # doc always shows what the common-tool surface still LACKS, tracked to a board row.
    open_gaps = open_capability_gaps()
    lines += ["", "## Open capability gaps (sufficiency frontier)", ""]
    if open_gaps:
        lines += [
            f"**{len(open_gaps)} OPEN** — the common-tool ACTION surface is not yet complete. "
            "`scripts/check_capability_gaps.py` exits non-zero while any is open.",
            "",
            "| Gap | Needed by | Missing | Follow-on |",
            "| --- | --- | --- | --- |",
        ]
        for g in open_gaps:
            lines.append(
                f"| {g.title} | {', '.join(g.needed_by)} | {g.reason} | {g.followon_vt} |"
            )
    else:
        lines.append("None open — every tracked capability gap has been built/promoted.")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "KNOWN_CAPABILITY_GAPS",
    "UNKNOWN",
    "CapabilityGap",
    "GapKind",
    "ToolCatalogEntry",
    "ToolKind",
    "catalog_by_name",
    "catalog_coverage_gaps",
    "catalog_entries",
    "catalog_tool_names",
    "open_capability_gaps",
    "render_catalog_markdown",
]
