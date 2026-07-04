"""VT-206 Integration Agent — onboarding specialist (CL-420).

The Integration Agent walks owners through 5 phases of onboarding:
discovery → auth → sample pull → field mapping → confirmed. Mirrors
the orchestrator-agent architecture (langchain `create_agent` + Opus
4.7 + `cache_control` per VT-194) so the Anthropic prompt cache
amortises the system prompt + tool inventory across dispatches.

Q1 Option A locked per Cowork plan-review 2026-05-28: full mirror of
`orchestrator_agent.py` shape.
Q2 Option A locked: 5-phase CHECK + JSONB state column with Pydantic
``PendingOwnerInput`` model gating writes.
Q5 Option A locked: `spawn_integration` handoff tool mirrors
`spawn_sales_recovery`.

Per CL-420: this is the agent itself; concrete connector tool
implementations land in VT-207+ (google_sheet) / VT-208 (shopify) /
etc. Most tools here are STUBS that log intent.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from langchain.agents import AgentState, create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.agent.lane_tenant import lane_tenant_error, resolve_lane_tenant
from orchestrator.integrations import (
    OWNER_VISIBLE_CONNECTOR_IDS,
    list_owner_visible_connectors,
    render_connector_listing_markdown,
)
from orchestrator.types.trigger_reason import TriggerReason

logger = logging.getLogger("orchestrator.agent.integration")

_PROMPT_PATH = (
    Path(__file__).parent.parent / "prompts" / "integration_agent_system.md"
)
INTEGRATION_AGENT_SYSTEM_PROMPT_BASE = _PROMPT_PATH.read_text(encoding="utf-8")

# The Integration Agent's prompt = base policy text + the
# deterministically-rendered connector registry listing (per VT-205
# AC-4 — no hard-coded connector names). Cached via VT-194 cache_control
# marker on the SystemMessage prefix.
INTEGRATION_AGENT_SYSTEM_PROMPT = (
    INTEGRATION_AGENT_SYSTEM_PROMPT_BASE
    + "\n\n"
    + render_connector_listing_markdown()
)

INTEGRATION_AGENT_SYSTEM_MESSAGE = SystemMessage(
    content=[
        {
            "type": "text",
            "text": INTEGRATION_AGENT_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
)

# mypy --strict needs the call-arg ignore for ChatAnthropic's pydantic
# kwargs — same pattern as orchestrator_agent.py.
_MODEL = ChatAnthropic(model="claude-opus-4-7", max_tokens=4096)  # type: ignore[call-arg]


# Q2 Option A locked — Pydantic model gates JSONB writes for the
# pending_owner_input column.
PendingOwnerInputKind = Literal[
    "connector_choice",
    "oauth_completion",
    "api_key_entry",
    "file_upload",
    "field_mapping_confirm",
    "cadence_choice",
]


class PendingOwnerInput(BaseModel):
    """Validated shape for ``tenant_integration_state.pending_owner_input``.

    Persisted as JSONB; loaded via ``model_validate(row['pending_owner_input'])``
    on each invocation. Bias the agent toward structured prompts that
    bind cleanly to this model (per Cowork Q2 flag).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    awaiting: PendingOwnerInputKind
    prompt_text: str
    valid_responses: list[str] | None = None
    connector_id: str | None = None
    walkthrough_url: str | None = None
    expires_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# -----------------------------------------------------------------
# Tools
# -----------------------------------------------------------------


@tool
def list_connectors_tool(category: str = "") -> str:
    """List the connectors the owner can ACTUALLY connect today (optionally filtered by category).

    VT-604 Package 1: filtered to the OWNER-VISIBLE catalogue — Shopify + Google Sheets, the only
    two with a real implementation. The full VT-205 registry carries additional placeholder
    entries (Amazon Seller Central, GA4, WooCommerce, the manual VT-6 family, …); none of them are
    listed here — they are unbuilt, so offering them as connectable would be dishonest. If the
    owner names an unsupported platform by name, say plainly that it isn't supported yet; do not
    promise a walkthrough or a future connection for it.
    """
    cat_arg = category if category in ("digital", "manual", "scrape") else None
    items = list_owner_visible_connectors(category=cat_arg)  # type: ignore[arg-type]
    if not items:
        return "(no connectors in this category)"
    lines = [
        f"- **{s.connector_id}** ({s.display_name}, {s.auth_flow}) — {s.summary}"
        for s in items
    ]
    return "\n".join(lines)


@tool
def start_connector_setup(connector_id: str, tenant_id: str, shop: str = "") -> dict[str, str]:
    """Begin the auth flow for ``connector_id``. VT-425 Phase A — Shopify is REAL: mints the
    Shopify ``authorize_url`` link-out (the owner taps it in the WA in-app browser, approves,
    returns) and writes the oauth_completion pending-state for the VT-267 resume.

    ``shop`` is the owner's ``*.myshopify.com`` domain (required for Shopify). Returns the
    ``authorize_url`` (key is ``authorize_url``, NOT ``auth_url``).

    VT-604 Package 1: a ``connector_id`` outside the OWNER-VISIBLE catalogue (Shopify + Google
    Sheets — everything else in the VT-205 registry is a documented-but-unbuilt placeholder, e.g.
    Amazon Seller Central) returns an honest ``status: unsupported`` envelope with NO promised
    follow-up — never a "we'll show you a walkthrough" / "not wired yet" implication that a
    connection is coming.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="start_connector_setup")
    if resolved is None:
        return lane_tenant_error("start_connector_setup")
    tenant_id = str(resolved)

    if connector_id not in OWNER_VISIBLE_CONNECTOR_IDS:
        logger.info(
            "start_connector_setup connector=%s tenant=%s (not owner-visible — reporting unsupported)",
            connector_id, tenant_id,
        )
        return {
            "connector_id": connector_id,
            "status": "unsupported",
            "message": (
                "This connector isn't supported yet — Viabe Team currently connects "
                "Shopify and Google Sheets."
            ),
        }

    if connector_id == "shopify":
        if not shop:
            return {
                "connector_id": connector_id,
                "next_action": "prompt_shop_domain",
                "prompt": "Ask the owner for their Shopify store address (yourstore.myshopify.com).",
            }
        from orchestrator.onboarding.shopify_onboarding import start_shopify_setup

        result = start_shopify_setup(tenant_id, shop)
        logger.info("VT-425 start_connector_setup shopify tenant=%s (authorize_url minted)", tenant_id)
        return {
            "connector_id": connector_id,
            "next_action": "owner_completes_oauth_then_says_done",
            "authorize_url": result["authorize_url"],
        }
    # google_sheet — owner-visible (real catalogue entry) but its OAuth flow is not yet wired
    # onto this tool (Package 5 territory). Distinct from "unsupported": this connector IS one
    # the owner can eventually connect; it just isn't live on this seam today.
    logger.info("start_connector_setup connector=%s tenant=%s (not wired in Phase A)", connector_id, tenant_id)
    return {
        "connector_id": connector_id,
        "next_action": "show_walkthrough_or_prompt_credential",
        "not_wired_phase_a": "true",
    }


@tool
def pull_sample(tenant_id: str, connector_id: str) -> dict[str, Any]:
    """Fetch a sample from the connector. VT-425 Phase A — Shopify is REAL.

    PII (CL-104 / CL-390): returns COUNTS ONLY — NEVER raw rows. Raw customer phone/email/name
    must never reach the LLM prompt, so the sample row CONTENT stays server-side; the agent only
    sees how many were found.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="pull_sample")
    if resolved is None:
        return lane_tenant_error("pull_sample")
    tenant_id = str(resolved)

    if connector_id == "shopify":
        from orchestrator.integrations.connectors.shopify import ShopifyConnector

        sample = ShopifyConnector().pull_sample(UUID(tenant_id))
        logger.info("VT-425 pull_sample shopify tenant=%s rows=%d (counts only)", tenant_id, len(sample))
        return {"connector_id": connector_id, "row_count": len(sample)}  # COUNTS ONLY — no PII
    logger.info("pull_sample connector=%s tenant=%s (not wired in Phase A)", connector_id, tenant_id)
    return {"row_count": 0, "not_wired_phase_a": "true"}


@tool
def propose_field_mapping_stub(
    tenant_id: str, connector_id: str, source_fields: list[str]
) -> dict[str, str]:
    """STUB — propose source→canonical field mapping. TODO(VT-209) reasoner."""
    resolved = resolve_lane_tenant(tenant_id, tool_name="propose_field_mapping_stub")
    if resolved is None:
        return lane_tenant_error("propose_field_mapping_stub")
    tenant_id = str(resolved)

    logger.info(
        "[VT-209 STUB] propose_field_mapping tenant=%s connector=%s",
        tenant_id, connector_id,
    )
    return {"proposed_mapping": "{}", "stub": "true"}


@tool
def confirm_field_mapping_stub(
    tenant_id: str, connector_id: str, mapping: dict[str, str]
) -> dict[str, str]:
    """STUB — persist owner-confirmed mapping. TODO(VT-209)."""
    resolved = resolve_lane_tenant(tenant_id, tool_name="confirm_field_mapping_stub")
    if resolved is None:
        return lane_tenant_error("confirm_field_mapping_stub")
    tenant_id = str(resolved)

    logger.info(
        "[VT-209 STUB] confirm_field_mapping tenant=%s connector=%s",
        tenant_id, connector_id,
    )
    return {"confirmed": "true", "stub": "true"}


@tool
def setup_recurring_ingestion_stub(
    tenant_id: str, connector_id: str, cadence: str
) -> dict[str, str]:
    """Schedule recurring pulls (VT-210). Inserts/updates ``tenant_connector_status``.

    Cadence is a Phase-1 daily cron expression (``"M H * * *"``). The
    scheduler (``orchestrator.integrations.scheduler``) scans this table
    every 5 minutes and fires per-(tenant, connector) workflows on due
    rows. ``next_scheduled_run`` is computed from ``cadence`` at insert
    time; subsequent runs update it via the same parser.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="setup_recurring_ingestion_stub")
    if resolved is None:
        return lane_tenant_error("setup_recurring_ingestion_stub")
    tenant_id = str(resolved)

    from datetime import UTC, datetime

    from orchestrator.db import tenant_connection
    from orchestrator.integrations.scheduler import _compute_next_run

    next_run = _compute_next_run(cadence, datetime.now(UTC))
    # VT-603: RLS-scoped write keyed on the RESOLVED tenant — never the raw BYPASSRLS pool keyed
    # on a model-supplied string (the live cross-tenant-write defect this closes).
    with tenant_connection(resolved) as conn:
        conn.execute(
            """
            INSERT INTO tenant_connector_status (
                tenant_id, connector_id, pull_cadence,
                next_scheduled_run, enabled
            ) VALUES (%s, %s, %s, %s, TRUE)
            ON CONFLICT (tenant_id, connector_id) DO UPDATE SET
                pull_cadence = EXCLUDED.pull_cadence,
                next_scheduled_run = EXCLUDED.next_scheduled_run,
                enabled = TRUE,
                updated_at = now()
            """,
            (tenant_id, connector_id, cadence, next_run),
        )
    logger.info(
        "setup_recurring_ingestion tenant=%s connector=%s cadence=%s next=%s",
        tenant_id, connector_id, cadence, next_run.isoformat(),
    )
    return {"scheduled": "true", "next_run": next_run.isoformat()}


# VT-425 Phase A — the `dedupe_against_existing_stub` is DELETED (plan §3 "delete the concept").
# A connector commit must NEVER be an agent tool: the integration agent is fail-CLOSED against
# ledger/customer-writes (VT-268 assert_agent_tools_safe). The Shopify sample commit (fixed-schema
# auto-map → ingest_customer_rows + dedup_and_merge) runs SERVER-SIDE in
# orchestrator.onboarding.shopify_onboarding.pull_and_ingest_shopify, invoked by the deterministic
# resume hook — never from inside the agent's tool list.


@tool
def integration_escalate_to_fazal(
    run_id: str, reason: str, owner_stuck_at: str
) -> str:
    """Escalate to Fazal when owner is stuck. Log + return ack."""
    logger.warning(
        "INTEGRATION_ESCALATE run_id=%s reason=%s stuck_at=%s",
        run_id, reason, owner_stuck_at,
    )
    return f"[escalated] reason={reason}"


INTEGRATION_AGENT_TOOLS: list[BaseTool] = [
    list_connectors_tool,
    start_connector_setup,  # VT-425 — de-stubbed (real Shopify authorize_url link-out)
    pull_sample,  # VT-425 — de-stubbed (real Shopify pull; COUNTS-ONLY return, no PII to LLM)
    # VT-425 Phase A uses a FIXED-SCHEMA auto-map for Shopify (no mapping form, no reasoner), so
    # the field-mapping stubs are NOT in the active launch path — kept for Phase C (Sheets/CSV).
    propose_field_mapping_stub,
    confirm_field_mapping_stub,
    setup_recurring_ingestion_stub,
    # dedupe_against_existing_stub DELETED (plan §3) — commit is server-side, never an agent tool.
    integration_escalate_to_fazal,
]


class IntegrationAgentState(AgentState, total=False):
    """State schema for the integration_agent subgraph (mirrors VT-3.4 PR 2/3
    pattern). Carries tenant_id / run_id / trigger_reason into the subgraph.
    """

    run_id: UUID | None
    tenant_id: UUID | None
    trigger_reason: TriggerReason | None


def build_integration_agent(
    model: ChatAnthropic = _MODEL,
    *,
    extra_tools: Sequence[BaseTool] = (),
) -> Any:
    """Build the Integration Agent with base tools + extras.

    Caller wraps invocation under ``observability_context(run_id,
    tenant_id)`` (VT-181) so VT-125's ``OrchestratorReasoningCallback``
    can attach + emit ``agent_reasoning_step`` rows. The handoff seam
    is the supervisor graph's ``spawn_integration`` node (VT-27
    pattern; see ``handoffs.py``).
    """
    tools = [*INTEGRATION_AGENT_TOOLS, *extra_tools]
    # VT-268: fail-CLOSED guardrail — the integration agent must never hold a Sheets-write /
    # ledger-write / direct-send tool (raises at build if it does). The accounts book (owner's
    # Google Sheet) is read-only; ingestion writes go through the non-agent service path.
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    assert_agent_tools_safe(tools, surface="integration_agent")
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=INTEGRATION_AGENT_SYSTEM_MESSAGE,
        name="integration_agent",
        state_schema=IntegrationAgentState,
    )


integration_agent = build_integration_agent(_MODEL)


__all__ = [
    "INTEGRATION_AGENT_SYSTEM_MESSAGE",
    "INTEGRATION_AGENT_SYSTEM_PROMPT",
    "INTEGRATION_AGENT_TOOLS",
    "IntegrationAgentState",
    "PendingOwnerInput",
    "PendingOwnerInputKind",
    "build_integration_agent",
    "integration_agent",
]
