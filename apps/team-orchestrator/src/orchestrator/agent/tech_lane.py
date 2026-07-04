"""VT-472 — the TECH specialist lane (Team-Manager rebuild, design §8 charter).

VT-604 Package 1 UPDATE (2026-07-05): this lane is NOT a roster specialist. The
verified Phase-1 runtime scope is exactly three specialists (sales_recovery /
integration / onboarding_conductor); this module's ``SPECIALIST_SPEC`` (bottom of
file) is no longer appended to ``agent/roster.py``'s ``ROSTER`` — there is no spawn
tool, no graph node, no route for ``tech_lane``. A curated subset of its ``@tool``
functions is instead exposed DIRECTLY to the Manager as an advisory capability — see
``agent/advisory_registry.py`` for the exact subset. ``advise_integration_setup``
ALSO gained the VT-604 connector-catalogue filter (owner-visible = Shopify + Google
Sheets only) — see that tool's own docstring.

The Tech lane is one of the six manager specialists (design §7 "Division of intelligence",
211500Z). The MANAGER reads the business situation + decides the desired OUTCOME ("keep the
storefront + listings healthy", "the Shopify sync stopped — fix it", "their Google listing
shows wrong hours") + hands off to THIS specialist; the SPECIALIST takes {situation, outcome,
context_slice, data} and decides the ACTION using its DOMAIN EXPERTISE — store/website/listing
HEALTH (GBP / Shopify / delivery-platform listings), integration setup help, connection
diagnosis (§8 VT-472 charter). It is ACTION-accountable, lane-scoped, and holds NO
cross-functional strategy.

SHAPE — mirrors ``integration_agent.build_integration_agent`` / ``onboarding_conductor`` /
``cost_opt_lane`` byte-for-byte (langchain ``create_agent`` sub-graph + Opus + ``cache_control``
per VT-194), registered as a ``SpecialistSpec`` (``SPECIALIST_SPEC``) the coordinator appends to
``agent/roster.py``'s ``ROSTER``. This module is DISJOINT — it owns NO edit to roster.py /
supervisor.py / routing.py / the rail files; the coordinator registers it centrally (design §7
"adding a lane = a sub-graph + a registry entry", not graph surgery). VT-474 owns the rail
internals; this lane CALLS the rail, it does not modify it.

REUSE (no duplication, Fazal standing) — the lane's tools delegate to the EXISTING integration /
listing substrate; they build NO parallel connector or aggregation machinery:
  - the EXISTING Integration Agent (``agent.integration_agent``) — the connector specialist that
    already does discovery → auth → sample → mapping. The Tech lane ADVISES on setup and defers
    the actual connect/auth flow to it (``list_connectors`` / ``get_connector`` registry reads),
    never rebuilding the connector path.
  - ``tenant_connector_status`` (VT-210 substrate) — the recurring-ingestion operational state
    (enabled / last_status / last_sync_at / consecutive_fails / last_error_message /
    next_scheduled_run): the integration-HEALTH read.
  - ``platform_listings`` (VT-325) — per (tenant, platform, external_listing_id) listing rows
    (rating + structured non-PII attributes incl. permanently_closed / hours / category): the
    listing-HEALTH read. Read via the same RLS-scoped ``tenant_connection`` the writer uses.
  - ``knowledge.business_context.read_business_context`` (VT-466) — the manager-held objective +
    the tech-relevant profile slice (context).

THE CONTRACT — INTENTS through the RAILS, never a direct effect (design §4/§7, VT-268)
---------------------------------------------------------------------------
"Nothing hardcoded" = dynamic BEHAVIOUR; the safety/correctness RAILS stay DETERMINISTIC. The
specialist REASONS about technical health (the dynamic part, read-only) and produces config /
integration changes as INTENTS. Every consequential CONFIG change routes through the EXISTING
business-impact rail — the specialist has NO code path around it and HOLDS NO tool that performs
one:

  * CONFIG CHANGE (a listing edit, a connector re-wire, a store/website setting, toggling a
    sync) → the BUSINESS-IMPACT rail (VT-467, owner-gated, ``CONFIG`` class). The specialist
    NEVER writes a config (VT-268: no ``write_config`` / ``apply_config_change`` /
    ``update_integration_config`` tool on its surface — graph build RAISES if one is added). It
    PROPOSES the change as an INTENT, then routes it through ``assert_or_gate_business_action``
    for ``BusinessImpactClass.CONFIG`` — DETERMINISTICALLY autonomous-vs-owner-approval from {the
    owner's policy, the tenant's CONFIG autonomy tier}. A fail-closed (no-grant) tenant ⇒
    REQUIRES_OWNER_APPROVAL (the owner-gated charter default). The ACTUAL config push is a
    non-agent path inside ``business_action_context`` AFTER an AUTONOMOUS gate or the owner's
    approval — never from a tool here.

So this lane's tools are: tech HEALTH DIAGNOSIS (read-only — integration + listing status) +
integration SETUP advice (defer to the Integration Agent) + a RAIL-FACING CONFIG-change INTENT
check (report the deterministic gate decision) + escalate. It carries NO config-write /
integration-mutate capability; the VT-268 ``assert_agent_tools_safe`` at build is the fail-CLOSED
backstop.

v1 = ADVISE / ACT-WITHIN-POLICY (design §8). NO future-autonomy is built here — the specialist
diagnoses + proposes + the rails gate; it does not self-grant policy or self-loosen the gate.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain.agents import AgentState, create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.agent.lane_tenant import lane_tenant_error, resolve_lane_tenant
from orchestrator.types.trigger_reason import TriggerReason

logger = logging.getLogger("orchestrator.agent.tech_lane")

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "tech_lane_system.md"
TECH_LANE_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# VT-194 prompt caching — the cached prefix amortises the system prompt + tool inventory across
# dispatches (parity with orchestrator_agent / integration_agent / onboarding_conductor).
TECH_LANE_SYSTEM_MESSAGE = SystemMessage(
    content=[
        {
            "type": "text",
            "text": TECH_LANE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
)

# mypy --strict needs the call-arg ignore for ChatAnthropic's pydantic kwargs (parity with the
# orchestrator / integration / onboarding / cost-opt agents).
_MODEL = ChatAnthropic(model="claude-opus-4-7", max_tokens=4096)  # type: ignore[call-arg]

# A listing is "stale" if it has not been refreshed in this many days — a health flag the
# specialist surfaces (the listing data the team reads is going out of date).
_LISTING_STALE_DAYS = 30


# ===========================================================================
# The DIAGNOSIS output shapes — what the lane reasons over + can hand BACK to the manager.
# These carry NO side-effect; they describe the read state.
# ===========================================================================


class TechHealthFinding(BaseModel):
    """One technical-health finding (a diagnosis — NOT an action). PII-safe: ids / status codes /
    ratings / counts only (CL-390)."""

    model_config = ConfigDict(frozen=True)

    area: str = Field(
        ...,
        description="the health area: 'integration' | 'listing' | 'store' | 'website'",
    )
    subject: str = Field(
        ..., description="WHAT this is about (a connector_id or a platform listing key) — no PII"
    )
    status: str = Field(
        ..., description="the read status: 'healthy' | 'degraded' | 'broken' | 'stale' | 'unknown'"
    )
    detail: str = Field(..., description="the diagnosis in plain terms, grounded in the read data")


# ===========================================================================
# Tools — READ-ONLY health diagnosis + integration-setup advice + a RAIL-FACING CONFIG INTENT
# check. NONE writes a config or mutates an integration: every consequential CONFIG change routes
# through the EXISTING business-impact gate (the specialist reports the gate decision; the
# non-agent path runs the effect after the gate). REUSE the integration substrate; this module
# owns ZERO side-effect machinery of its own. Names are deliberately read-ish (read_/advise_/
# check_) so the VT-268 guard never false-flags them.
# ===========================================================================


@tool
def read_integration_health(tenant_id: str) -> dict[str, Any]:
    """Read the HEALTH of the tenant's data integrations / connectors — is each sync alive, when
    did it last run, is it erroring.

    REUSE: reads ``tenant_connector_status`` (the VT-210 recurring-ingestion operational state)
    under the RLS-scoped ``tenant_connection`` — no parallel store. For each connected source it
    returns ``{connector_id, enabled, last_status, last_sync_at, consecutive_fails,
    last_error_message, next_scheduled_run}``. Use it to diagnose a broken/stale sync (a non-'ok'
    last_status, a rising consecutive_fails, a long-ago last_sync_at, or a disabled connector).
    Returns ``{connectors: [...], count}`` — status codes + counts ONLY, no customer PII.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="read_integration_health")
    if resolved is None:
        return lane_tenant_error("read_integration_health")
    tenant_id = str(resolved)

    from orchestrator.db.tenant_connection import tenant_connection

    connectors: list[dict[str, Any]] = []
    with tenant_connection(tenant_id) as conn:
        rows = conn.execute(
            """
            SELECT connector_id, enabled, last_status, last_sync_at,
                   consecutive_fails, last_error_message, next_scheduled_run
            FROM tenant_connector_status
            WHERE tenant_id = %s
            ORDER BY connector_id
            """,
            (str(UUID(tenant_id)),),
        ).fetchall()
    for r in rows:
        rec = dict(r) if isinstance(r, dict) else dict(
            zip(
                (
                    "connector_id", "enabled", "last_status", "last_sync_at",
                    "consecutive_fails", "last_error_message", "next_scheduled_run",
                ),
                r,
                strict=False,
            )
        )
        connectors.append(
            {
                "connector_id": rec.get("connector_id"),
                "enabled": bool(rec.get("enabled")),
                "last_status": rec.get("last_status"),
                "last_sync_at": _iso(rec.get("last_sync_at")),
                "consecutive_fails": int(rec.get("consecutive_fails") or 0),
                # last_error_message is an internal error string (vendor SDK repr), NOT owner/customer
                # PII — surfaced truncated so the specialist can name the failure cause.
                "last_error_message": (
                    str(rec["last_error_message"])[:200] if rec.get("last_error_message") else None
                ),
                "next_scheduled_run": _iso(rec.get("next_scheduled_run")),
            }
        )
    logger.info(
        "tech_lane: read_integration_health tenant=%s connectors=%d", tenant_id, len(connectors)
    )
    return {"connectors": connectors, "count": len(connectors)}


@tool
def read_listing_health(tenant_id: str) -> dict[str, Any]:
    """Read the HEALTH of the tenant's platform LISTINGS — GBP / Swiggy / Zomato etc.: rating,
    freshness, and whether a listing is showing the business permanently-closed.

    REUSE: reads ``platform_listings`` (the VT-325 per (tenant, platform, external_listing_id)
    rows) under the RLS-scoped ``tenant_connection`` — the same table the GBP/food ingest writes.
    For each listing it returns ``{platform, external_listing_id, rating, fetched_at, stale,
    permanently_closed, name, category}``. ``stale`` flags a listing not refreshed in 30 days (going
    out of date — the ``_LISTING_STALE_DAYS`` threshold). ``permanently_closed`` (read from the
    structured attributes) is a strong health flag — the listing tells customers the business is closed.

    CL-390: ``attributes`` carries ONLY structured non-PII facts (name/category/hours/closed) — the
    ingest allowlist never copies review text or reviewer identity, so nothing here is customer PII.
    Returns ``{listings: [...], count}``.

    VT-465: reads through the SANCTIONED ``PlatformListingsWrapper.list_for_tenant`` seam (the
    `no-direct-tenant-db-access` lint's wrapper layer) — `platform_listings` is a VT-325 watched hot
    table, so this lane reads it through the typed wrapper (RLS-scoped + Pillar-8 tenant-validated),
    NOT a direct SELECT. Same pattern every migrated hot-table read uses.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="read_listing_health")
    if resolved is None:
        return lane_tenant_error("read_listing_health")
    tenant_id = str(resolved)

    from orchestrator.db.wrappers import PlatformListingsWrapper

    now = datetime.now(timezone.utc)
    listings: list[dict[str, Any]] = []
    rows = PlatformListingsWrapper().list_for_tenant(tenant_id)
    # Stable order (the wrapper's generic list does not ORDER BY) — by (platform,
    # external_listing_id), matching the prior direct-read contract.
    rows = sorted(
        rows,
        key=lambda r: (str(r.get("platform") or ""), str(r.get("external_listing_id") or "")),
    )
    for rec in rows:
        attrs = rec.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}
        fetched_at = rec.get("fetched_at")
        stale = bool(
            isinstance(fetched_at, datetime)
            and (now - _aware(fetched_at)).days >= _LISTING_STALE_DAYS
        )
        listings.append(
            {
                "platform": rec.get("platform"),
                "external_listing_id": rec.get("external_listing_id"),
                "rating": float(rec["rating"]) if rec.get("rating") is not None else None,
                "fetched_at": _iso(fetched_at),
                "stale": stale,
                "permanently_closed": bool(attrs.get("permanently_closed", False)),
                "name": attrs.get("gbp_title") or attrs.get("name"),
                "category": attrs.get("category"),
            }
        )
    logger.info("tech_lane: read_listing_health tenant=%s listings=%d", tenant_id, len(listings))
    return {"listings": listings, "count": len(listings)}


@tool
def advise_integration_setup(category: str = "") -> dict[str, Any]:
    """Advise WHICH connector fits + the next setup step — the integration-setup help charter.

    REUSE (no rebuild): reads the OWNER-VISIBLE connector catalogue (``integrations.
    list_owner_visible_connectors`` — VT-604 Package 1: Shopify + Google Sheets, the only two with
    a real implementation) — optionally filtered by category ('digital' | 'manual' | 'scrape'). Use
    it to recommend the right data source for the owner's tools and explain the connect path. This
    is ADVICE: the ACTUAL auth/connect flow runs on the Integration Agent + the deterministic
    connector path (this lane holds no connector auth/write tool). Anything outside this catalogue
    (Amazon Seller Central, GA4, WooCommerce, …) is an unbuilt placeholder — never recommend it as
    connectable; say plainly it isn't supported yet. Returns ``{connectors: [{connector_id,
    display_name, auth_flow, category, summary, auth_walkthrough_url}], count}`` — registry facts,
    no PII.
    """
    from orchestrator.integrations import list_owner_visible_connectors

    cat = category if category in ("digital", "manual", "scrape") else None
    specs = list_owner_visible_connectors(category=cat)  # type: ignore[arg-type]
    items = [
        {
            "connector_id": s.connector_id,
            "display_name": s.display_name,
            "auth_flow": s.auth_flow,
            "category": s.category,
            "summary": s.summary,
            "auth_walkthrough_url": getattr(s, "auth_walkthrough_url", None),
        }
        for s in specs
    ]
    logger.info("tech_lane: advise_integration_setup category=%s count=%d", category or "*", len(items))
    return {"connectors": items, "count": len(items)}


@tool
def read_tech_context(tenant_id: str) -> dict[str, Any]:
    """Read the manager-held business objective + the tech-relevant profile slice (context).

    REUSE: delegates to ``knowledge.business_context.read_business_context`` (VT-466) — the ONE
    per-tenant context record (verified identity + the objective the manager holds). Use it to
    frame a health finding against the owner's goal (e.g. "you're trying to grow online orders —
    the Shopify sync has been broken 3 days"). Best-effort: a read miss yields ``{}``. No
    cross-tenant data.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="read_tech_context")
    if resolved is None:
        return lane_tenant_error("read_tech_context")
    tenant_id = str(resolved)

    try:
        from orchestrator.knowledge.business_context import read_business_context

        ctx = read_business_context(tenant_id)
        return {
            "objective": dict(ctx.objective),
            "identity": dict(getattr(ctx, "identity", {}) or {}),
        }
    except Exception:  # noqa: BLE001 — context is enrichment; a miss yields {}
        logger.warning("tech_lane: read_tech_context miss tenant=%s", tenant_id)
        return {}


@tool
def propose_config_change(
    tenant_id: str, target: str, change_summary: str, current_value: str = "", desired_value: str = ""
) -> dict[str, Any]:
    """Propose a config / integration CHANGE as an INTENT — the specialist's diagnosis turned into a
    PROPOSAL, NO effect. This produces the structured intent (WHAT to change + WHY); it does NOT
    write the config and does NOT mutate the integration. The intent is handed back to the manager /
    the deterministic config path, which routes the change through the owner-gated business-impact
    gate (see ``check_config_change_intent``) — the change only takes effect AFTER an autonomous gate
    or the owner's approval.

    Args:
      target — WHAT is being changed (a connector_id, a listing key, a store/website setting key) — no PII.
      change_summary — the change in plain owner terms (e.g. "re-connect the Shopify sync",
        "update the GBP listing hours", "disable the broken Sheets pull").
      current_value / desired_value — optional human-readable before/after (owner-reviewed copy, NOT
        a secret or credential — never a token/key/password).

    Returns the structured intent (``{kind: 'config_change', ...}``). No PII / no secret (CL-390).
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="propose_config_change")
    if resolved is None:
        return lane_tenant_error("propose_config_change")
    tenant_id = str(resolved)

    intent = {
        "kind": "config_change",
        "tenant_id": tenant_id,
        "target": target,
        "change_summary": change_summary,
        "current_value": current_value,
        "desired_value": desired_value,
    }
    logger.info(
        "tech_lane: propose_config_change tenant=%s target=%s (intent only, no write)",
        tenant_id, target,
    )
    return intent


@tool
def check_config_change_intent(tenant_id: str, target: str) -> dict[str, Any]:
    """Check a CONFIG-change intent against the DETERMINISTIC business-impact rail — the rail decides
    autonomous-vs-owner-approval, NOT the specialist. The specialist NEVER writes a config (VT-268:
    it holds no config-write tool); this consults the gate so it knows whether a proposed change is
    autonomous or needs owner approval — BEFORE proposing it.

    REUSE (no rebuild): routes the intent through ``business_impact_choke.assert_or_gate_business_
    action`` for ``BusinessImpactClass.CONFIG`` (VT-467) — the SAME deterministic gate every
    consequential config change uses:
      1. The OUTER policy bound (``assert_within_policy`` for ``CONFIG``, VT-474 A2) — is a config
         change an allowed action TYPE for this tenant at all. Out-of-policy ⇒ owner approval
         regardless of tier (the brain can't reason past the owner's policy).
      2. The per-class autonomy tier (the VT-467 decaying-HITL): a permitting tier ⇒ AUTONOMOUS;
         a low-autonomy / frozen / no-grant tenant ⇒ REQUIRES_OWNER_APPROVAL (the owner-gated
         charter default — a config change is owner-gated by charter, so a fresh tenant fails closed
         to owner approval).

    A config change carries no money magnitude, so ``magnitude_minor=0``; the gate decides on
    {policy, tier}. The ACTUAL config push is a non-agent effect inside ``business_action_context``
    AFTER an AUTONOMOUS gate or the owner's approval — never this tool.

    Returns ``{decision, reason, action_class, requires_owner_approval}`` — IDs + class + a reason
    CODE only (CL-390); ``target`` is the specialist's own framing, not an owner secret.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="check_config_change_intent")
    if resolved is None:
        return lane_tenant_error("check_config_change_intent")
    tenant_id = str(resolved)

    from orchestrator.agents.business_impact_choke import (
        BusinessActionDecision,
        BusinessImpactClass,
        assert_or_gate_business_action,
    )

    gate = assert_or_gate_business_action(
        UUID(tenant_id),
        BusinessImpactClass.CONFIG,
        0,  # a config change has no money magnitude; the gate decides on {policy, tier}.
        action_attrs={"target": target},
    )
    logger.info(
        "tech_lane: check_config_change_intent tenant=%s target=%s decision=%s reason=%s",
        tenant_id, target, gate.decision.value, gate.reason,
    )
    return {
        "decision": gate.decision.value,
        "reason": gate.reason,
        "action_class": gate.action_class,
        "requires_owner_approval": gate.decision is BusinessActionDecision.REQUIRES_OWNER_APPROVAL,
    }


@tool
def tech_escalate_to_fazal(run_id: str, reason: str, owner_stuck_at: str) -> str:
    """Escalate to the owner (WhatsApp-only, design §6) when a technical decision is outside policy or
    a high-stakes config change / connection failure the specialist cannot resolve in-lane. Log +
    return ack (last-resort)."""
    logger.warning(
        "TECH_ESCALATE run_id=%s reason=%s stuck_at=%s",
        run_id, reason, owner_stuck_at,
    )
    return f"[escalated] reason={reason}"


TECH_LANE_TOOLS: list[BaseTool] = [
    read_integration_health,    # read-only: connector/sync HEALTH (tenant_connector_status)
    read_listing_health,        # read-only: GBP/platform listing HEALTH (platform_listings)
    advise_integration_setup,   # advise: which connector + next setup step (registry, no connect)
    read_tech_context,          # read-only: manager objective + tech-relevant context (VT-466)
    propose_config_change,      # advise: config/integration change INTENT (no write)
    check_config_change_intent, # rail-facing: business-impact CONFIG gate for a config-change intent
    tech_escalate_to_fazal,
]


class TechLaneState(AgentState, total=False):
    """State schema for the tech_lane sub-graph (mirrors IntegrationAgentState).

    Carries the run-identity fields into the sub-graph so a future handoff tool's ``InjectedState``
    can read them (parity with the integration / onboarding / cost-opt agents; the current tool set
    keys on ``tenant_id`` passed as a tool arg).
    """

    run_id: UUID | None
    tenant_id: UUID | None
    trigger_reason: TriggerReason | None


def build_tech_lane_agent(
    model: ChatAnthropic = _MODEL,
    *,
    extra_tools: Sequence[BaseTool] = (),
) -> Any:
    """Build the Tech specialist sub-graph (mirrors ``build_integration_agent`` /
    ``build_cost_opt_lane_agent``).

    VT-268 fail-CLOSED guardrail: the tech specialist must NEVER hold a direct config-write /
    integration-mutate / send / ledger-write / spend tool (raises at build if it does) — it
    DIAGNOSES tech health (read-only) + produces config-change INTENTS; the deterministic
    business-impact rail owns every config side-effect (the CONFIG gate). The config-change INTENT
    check here is a READ of the deterministic gate (no effect), not the effect itself.
    """
    tools = [*TECH_LANE_TOOLS, *extra_tools]
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    assert_agent_tools_safe(tools, surface="tech_lane")
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=TECH_LANE_SYSTEM_MESSAGE,
        name="tech_lane",
        state_schema=TechLaneState,
    )


def _build_tech_lane_node(model: Any) -> Any:
    """Return the tech_lane sub-graph (a CompiledStateGraph) — the roster ``node_builder``.

    REUSE: ``build_tech_lane_agent`` unchanged. ``wrap_node=False`` in ``SPECIALIST_SPEC`` — a
    compiled sub-graph must NOT be function-wrapped (VT-183 / VT-206), same as integration /
    onboarding / cost-opt. Imported by ``agent/roster.py`` when the coordinator appends
    ``SPECIALIST_SPEC``.
    """
    return build_tech_lane_agent(model=model)


# ===========================================================================
# Small read helpers (timestamp normalisation) — module-private, no side effect.
# ===========================================================================


def _aware(dt: datetime) -> datetime:
    """Treat a naive timestamp as UTC so the stale-day diff never raises on tz mismatch."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Any) -> str | None:
    """ISO-format a timestamp column; None passes through. Non-datetime stringifies (driver edge)."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


# ===========================================================================
# The roster registration — ONE SpecialistSpec the coordinator appends to ROSTER.
# This module owns NO edit to roster.py; it EXPORTS the spec, the coordinator
# registers it centrally (design §7 "adding a lane = a sub-graph + a registry
# entry", not graph surgery).
# ===========================================================================

# The ``SpecialistSpec`` TYPE is imported from roster.py (a one-way edge: roster.py does NOT import
# this module — it imports the node builders lazily inside its own spec entries — so no import
# cycle). The ``node_builder`` stays a module-level callable (``_build_tech_lane_node``) the
# coordinator-iterated graph build invokes; the langchain/Anthropic deps load only when that fires.
from orchestrator.agent.roster import SpecialistSpec  # noqa: E402 — placed after the node builder

SPECIALIST_SPEC = SpecialistSpec(
    name="tech",
    agent_name="tech_lane",
    spawn_tool_name="spawn_tech",
    route_key="spawn_tech",
    node_builder=_build_tech_lane_node,
    description=(
        "Hand off to the Tech specialist to diagnose and improve the technical HEALTH of the "
        "business's store/website, Google Business Profile + delivery-platform listings "
        "(GBP/Shopify/Swiggy/Zomato), and data integrations — a broken/stale sync, a listing "
        "showing wrong info or permanently-closed, integration setup help, a connection that "
        "stopped working. Use when the desired outcome is keeping the storefront / listings / "
        "integrations healthy and connected. It DIAGNOSES (read-only) and proposes config / "
        "integration changes as INTENTS; it never writes a config directly — config / integration "
        "changes are owner-gated and route through the business-impact gate."
    ),
    update_builder=None,  # the lane self-fetches via tenant_id; no pre-built data bundle.
    prereq=None,
    edge_to=None,  # END — the lane emits a diagnosis/intent, not a campaign plan to collapse.
    wrap_node=False,  # CompiledStateGraph sub-graph — not function-wrapped (VT-183).
    default_outcome="keep the store/website/listings/integrations healthy (advise)",
)


tech_lane = build_tech_lane_agent(_MODEL)


__all__ = [
    "TECH_LANE_SYSTEM_MESSAGE",
    "TECH_LANE_SYSTEM_PROMPT",
    "TECH_LANE_TOOLS",
    "TechHealthFinding",
    "TechLaneState",
    "SPECIALIST_SPEC",
    "build_tech_lane_agent",
    "tech_lane",
]
