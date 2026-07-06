"""VT-206 Integration Agent — onboarding specialist (CL-420).

VT-608 (Loop Package 5) — the REAL tool surface. Replaces the VT-206 stub inventory with the ten
context-scoped tools the expert plan names: list_supported_connectors, read_integration_state,
start_oauth, check_oauth_status, pull_sample, propose_mapping, confirm_mapping, commit_ingestion,
schedule_recurring_pull, verify_connector (+ integration_escalate_to_fazal, kept — CL-420's own
"escalate if stuck" hard rule needs a tool regardless, and the Package 5 list's intent is the
CONNECTOR surface, not the safety valve).

Tenancy (VT-603, binding on every tool here): the AMBIENT dispatch context always wins —
``resolve_lane_tenant`` resolves it; a model-supplied ``tenant_id`` that disagrees is logged and
ignored, never trusted. Every tool returns a structured ``{"status": "error", ...}`` dict on an
unresolvable tenant (VT-484 invariant) — NEVER raises (these sub-graphs hold no tool-error
middleware of their own).

VT-268 fail-closed guardrail (unchanged): the agent holds NO write tool for the customer/ledger
substrate. ``commit_ingestion`` returns a TYPED PROPOSAL only (RULING 3) — the actual write runs
server-side, deterministically, via ``integrations.commit.execute_pending_ingestion_commit``,
called from a NON-agent code path (``runner.py`` post-dispatch for legacy/shadow;
``manager/workflow.py``'s ``_dispatch_specialist_step`` post-``graph.invoke`` for enforce) — never
from inside this agent's own tool-calling loop. ``schedule_recurring_pull`` writing
``tenant_connector_status`` (cadence CONFIG, not customer/ledger data) directly from a tool is
existing, accepted precedent (VT-210's original ``setup_recurring_ingestion_stub``).

Real connectors: Shopify (fixed canonical mapping, no reasoner) + Google Sheets (OAuth
zero-paste + a team-web picker link-out, CL-421/CL-443 + RULING 2 — the owner picks
spreadsheet/tab; ``propose_mapping``/``confirm_mapping`` wrap the REAL VT-209 reasoner,
``integrations/field_mapping.py``). Every other connector in the VT-205 registry is a
documented-but-unbuilt placeholder — reported honestly as unsupported, never a promised
walkthrough.
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
    # VT-608 (Package 5) — the two NEW machine-driven waypoints the ten context-scoped tools
    # advance through between "OAuth done" and "cadence chosen". Neither is an owner-facing
    # question (nothing asks the owner anything at these two points); they exist so
    # ``read_integration_state`` gives the agent (and ``execute_pending_ingestion_commit``) an
    # honest, resumable phase marker across the fresh-thread-per-message gap (VT-425's own
    # rationale, generalized past Shopify).
    "sample_pull_pending",  # RULING 2 — the picker's POST /select landed; pull_sample is next.
    "ingestion_commit_pending",  # RULING 3 — commit_ingestion proposed; server-side execute is next.
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
# Tools — VT-608 Package 5's ten context-scoped tools
# -----------------------------------------------------------------


@tool
def list_supported_connectors(category: str = "") -> str:
    """List the connectors the owner can ACTUALLY connect today (optionally filtered by category).

    Filtered to the OWNER-VISIBLE catalogue — Shopify + Google Sheets, the only two with a real
    implementation. If the owner names an unsupported platform by name, say plainly that it isn't
    supported yet; do not promise a walkthrough or a future connection for it.
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
def read_integration_state(tenant_id: str) -> dict[str, Any]:
    """Read this tenant's CURRENT onboarding phase + pending waypoint. Call this FIRST on every
    invocation (each inbound message is a fresh thread — this is how you resume where you left
    off). Returns ``{"phase": ..., "current_connector_id": ..., "pending_owner_input": {...} | None}``
    or ``{"phase": None, ...}`` when no onboarding has started yet.

    No PII: ``pending_owner_input.metadata`` only ever carries connector ids, spreadsheet/tab
    identifiers, and confirmed field-mapping labels — never a raw customer phone/email/name.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="read_integration_state")
    if resolved is None:
        return lane_tenant_error("read_integration_state")

    from orchestrator.onboarding.shopify_onboarding import (
        read_integration_state as _read_state,
    )

    state = _read_state(resolved)
    if state is None:
        return {"phase": None, "current_connector_id": None, "pending_owner_input": None}
    return state


@tool
def start_oauth(tenant_id: str, connector_id: str, shop: str = "") -> dict[str, Any]:
    """Begin the OAuth flow for ``connector_id``. Both Shopify and Google Sheets are zero-paste
    (CL-421) — this mints a REAL authorize link-out for the owner to tap in their WhatsApp
    in-app browser, approve, and return.

    ``shop`` is the owner's ``*.myshopify.com`` domain — REQUIRED for Shopify, ignored for
    google_sheet. Returns ``{"authorize_url": ...}`` on success (key is ``authorize_url``, NOT
    ``auth_url``); for Shopify with no ``shop`` yet, returns a ``next_action: prompt_shop_domain``
    envelope instead (ask the owner for their store address first).

    A ``connector_id`` outside the owner-visible catalogue (Shopify + Google Sheets — everything
    else in the registry is a documented-but-unbuilt placeholder, e.g. Amazon Seller Central)
    returns an honest ``status: unsupported`` envelope with NO promised follow-up.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="start_oauth")
    if resolved is None:
        return lane_tenant_error("start_oauth")
    tenant_id = str(resolved)

    if connector_id not in OWNER_VISIBLE_CONNECTOR_IDS:
        logger.info(
            "start_oauth connector=%s tenant=%s (not owner-visible — reporting unsupported)",
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

        try:
            result = start_shopify_setup(tenant_id, shop)
        except Exception as exc:  # noqa: BLE001 — a domain/config failure is a BLOCK, never needs_owner_input
            logger.warning("start_oauth shopify failed tenant=%s: %s", tenant_id, exc)
            return {"connector_id": connector_id, "status": "error", "error": str(exc)}
        logger.info("VT-608 start_oauth shopify tenant=%s (authorize_url minted)", tenant_id)
        return {
            "connector_id": connector_id,
            "next_action": "owner_completes_oauth_then_says_done",
            "authorize_url": result["authorize_url"],
        }

    # google_sheet — real OAuth kickoff (RULING 2). After the owner approves, they land on the
    # team-web picker page (api/sheet_picker.py) to choose a spreadsheet + tab; this tool's own
    # job ends at "link minted".
    from orchestrator.integrations.sheets_oauth import start_sheets_oauth

    try:
        result = start_sheets_oauth(tenant_id)
    except Exception as exc:  # noqa: BLE001 — config failure (e.g. unset OAuth client) is a BLOCK
        logger.warning("start_oauth google_sheet failed tenant=%s: %s", tenant_id, exc)
        return {"connector_id": connector_id, "status": "error", "error": str(exc)}
    logger.info("VT-608 start_oauth google_sheet tenant=%s (authorize_url minted)", tenant_id)
    return {
        "connector_id": connector_id,
        "next_action": "owner_completes_oauth_then_picks_sheet",
        "authorize_url": result["authorize_url"],
    }


@tool
def check_oauth_status(tenant_id: str, connector_id: str) -> dict[str, Any]:
    """Has the owner finished OAuth for ``connector_id``? Reads the DURABLE DB truth
    (``tenant_oauth_tokens`` — the callback persisted a row) rather than trusting the owner's
    say-so. Returns ``{"connector_id": ..., "connected": bool}``. NEVER fabricate a "connected"
    status the DB doesn't back."""
    resolved = resolve_lane_tenant(tenant_id, tool_name="check_oauth_status")
    if resolved is None:
        return lane_tenant_error("check_oauth_status")

    from orchestrator.integrations.commit import is_connector_connected

    connected = is_connector_connected(resolved, connector_id)
    return {"connector_id": connector_id, "connected": connected}


@tool
def pull_sample(tenant_id: str, connector_id: str) -> dict[str, Any]:
    """Fetch a sample from the connector, persisting the phase forward. Returns COUNTS (+ column
    NAMES for google_sheet — CL-104 sanctions field-name-only exposure for the mapping reasoner;
    row VALUES never reach you) — NEVER raw customer phone/email/name.

    For ``google_sheet``: requires the owner to have already picked a spreadsheet + tab via the
    team-web picker link-out (RULING 2) — if they haven't yet, returns
    ``{"status": "awaiting_picker_selection"}`` (an honest incomplete-input state, not a failure —
    remind the owner to finish picking a sheet).
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="pull_sample")
    if resolved is None:
        return lane_tenant_error("pull_sample")
    tenant_id = str(resolved)

    if connector_id == "shopify":
        from orchestrator.integrations.connectors.shopify import ShopifyConnector

        try:
            sample = ShopifyConnector().pull_sample(UUID(tenant_id))
        except Exception as exc:  # noqa: BLE001 — connector/API failure is a BLOCK, never needs_owner_input
            logger.warning("pull_sample shopify failed tenant=%s: %s", tenant_id, exc)
            return {"connector_id": connector_id, "status": "error", "error": str(exc)}
        logger.info("VT-425 pull_sample shopify tenant=%s rows=%d (counts only)", tenant_id, len(sample))
        return {"connector_id": connector_id, "row_count": len(sample)}  # COUNTS ONLY — no PII

    if connector_id == "google_sheet":
        from orchestrator.onboarding.shopify_onboarding import (
            PHASE_MAPPING,
            _validated_pending,
            _write_state,
            read_integration_state as _read_state,
        )

        state = _read_state(tenant_id)
        pending = (state or {}).get("pending_owner_input") or {}
        metadata = pending.get("metadata") or {}
        spreadsheet_id = str(metadata.get("spreadsheet_id") or "")
        tab_name = str(metadata.get("tab_name") or "")
        if not spreadsheet_id:
            return {"connector_id": connector_id, "status": "awaiting_picker_selection", "row_count": 0}

        from orchestrator.integrations.connectors.google_sheet import GoogleSheetConnector

        try:
            rows = GoogleSheetConnector().pull_sample(
                UUID(tenant_id), spreadsheet_id, tab_name=tab_name
            )
        except Exception as exc:  # noqa: BLE001 — connector/API failure is a BLOCK, never needs_owner_input
            logger.warning("pull_sample google_sheet failed tenant=%s: %s", tenant_id, exc)
            return {"connector_id": connector_id, "status": "error", "error": str(exc)}

        column_names = list(rows[0].keys()) if rows else []
        # Persist the sample's column names (no PII — field labels only, CL-104) so
        # propose_mapping/confirm_mapping can be called without re-pulling; carries
        # spreadsheet_id/tab_name forward for commit_ingestion.
        new_metadata = {**metadata, "column_names": column_names}
        new_pending = _validated_pending(
            awaiting="field_mapping_confirm",
            prompt_text="Reviewing your sheet's columns now.",
            connector_id=connector_id,
            metadata=new_metadata,
        )
        _write_state(tenant_id, phase=PHASE_MAPPING, connector_id=connector_id, pending=new_pending)
        logger.info(
            "VT-608 pull_sample google_sheet tenant=%s rows=%d columns=%d (counts only)",
            tenant_id, len(rows), len(column_names),
        )
        return {"connector_id": connector_id, "row_count": len(rows), "column_names": column_names}

    logger.info("pull_sample connector=%s tenant=%s (unsupported)", connector_id, tenant_id)
    return {"connector_id": connector_id, "status": "unsupported", "row_count": 0}


@tool
def propose_mapping(tenant_id: str, connector_id: str, source_fields: list[str]) -> dict[str, Any]:
    """Propose a canonical-field mapping for each of ``source_fields`` (the sheet's column
    names — from ``pull_sample``'s ``column_names``). Runs the REAL VT-209 reasoner
    (heuristic exact/fuzzy match, LLM-assisted fallback below 0.85 confidence).

    Each item's ``routing`` tells you what to do:
      - ``ask_owner`` (confidence < 0.7) — ask the owner to confirm/correct this column's meaning.
      - ``commit_with_notification`` (0.7-0.85) — proceed, but mention it to the owner.
      - ``commit_silently`` (>= 0.85) — proceed without mentioning it.
    Shopify never needs this (fixed canonical mapping) — call it only for google_sheet.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="propose_mapping")
    if resolved is None:
        return lane_tenant_error("propose_mapping")

    from orchestrator.integrations.field_mapping import propose_field_mapping

    proposals = [
        {
            "source_field": m.source_field,
            "canonical_field": m.canonical_field,
            "confidence": m.confidence,
            "decided_by": m.decided_by,
            "routing": m.routing,
        }
        for m in (propose_field_mapping(f, connector_id) for f in source_fields)
    ]
    return {"connector_id": connector_id, "proposals": proposals}


@tool
def confirm_mapping(tenant_id: str, connector_id: str, mapping: dict[str, str]) -> dict[str, Any]:
    """Persist the owner-confirmed (or auto-committed) ``{source_field: canonical_field}``
    mapping. Carries the sample's spreadsheet/tab identifiers forward so ``commit_ingestion``
    can find them. Does NOT ingest anything — the actual row transform reuses the same proven
    alias-based mapper the recurring-pull scheduler already uses; this mapping is the
    owner-facing confirmation + audit record, not the literal ingest transform.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="confirm_mapping")
    if resolved is None:
        return lane_tenant_error("confirm_mapping")
    tenant_id = str(resolved)

    from orchestrator.onboarding.shopify_onboarding import (
        PHASE_MAPPING,
        _validated_pending,
        _write_state,
        read_integration_state as _read_state,
    )

    state = _read_state(tenant_id)
    existing_metadata = ((state or {}).get("pending_owner_input") or {}).get("metadata") or {}
    new_metadata = {**existing_metadata, "confirmed_mapping": mapping}
    pending = _validated_pending(
        awaiting="field_mapping_confirm",
        prompt_text="Field mapping confirmed.",
        connector_id=connector_id,
        metadata=new_metadata,
    )
    _write_state(tenant_id, phase=PHASE_MAPPING, connector_id=connector_id, pending=pending)
    logger.info(
        "VT-608 confirm_mapping tenant=%s connector=%s fields=%d",
        tenant_id, connector_id, len(mapping),
    )
    return {"connector_id": connector_id, "confirmed": True, "field_count": len(mapping)}


@tool
def commit_ingestion(tenant_id: str, connector_id: str) -> dict[str, Any]:
    """Propose committing the pulled sample into Viabe's customer substrate. RETURNS A PROPOSAL
    ONLY — you hold no write/commit tool (VT-268: never fabricate that data has landed). The
    actual ingestion runs SERVER-SIDE, deterministically, right after this turn ends; a
    subsequent ``verify_connector`` call (on your NEXT turn) will show it as completed.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="commit_ingestion")
    if resolved is None:
        return lane_tenant_error("commit_ingestion")
    tenant_id = str(resolved)

    from orchestrator.onboarding.shopify_onboarding import (
        PHASE_MAPPING,
        _validated_pending,
        _write_state,
        read_integration_state as _read_state,
    )

    state = _read_state(tenant_id)
    existing_metadata = ((state or {}).get("pending_owner_input") or {}).get("metadata") or {}
    if connector_id == "google_sheet" and not existing_metadata.get("spreadsheet_id"):
        return {
            "connector_id": connector_id,
            "status": "error",
            "error": "no spreadsheet/tab on file — call pull_sample first",
        }
    pending = _validated_pending(
        awaiting="ingestion_commit_pending",
        prompt_text="Committing your data now.",
        connector_id=connector_id,
        metadata=existing_metadata,
    )
    _write_state(tenant_id, phase=PHASE_MAPPING, connector_id=connector_id, pending=pending)
    logger.info("VT-608 commit_ingestion PROPOSED tenant=%s connector=%s", tenant_id, connector_id)
    return {
        "connector_id": connector_id,
        "status": "proposal_recorded",
        "note": "Ingestion will complete shortly — verify_connector will confirm it next turn.",
    }


@tool
def schedule_recurring_pull(tenant_id: str, connector_id: str, cadence: str) -> dict[str, str]:
    """Schedule (or change) the recurring pull cadence. Inserts/updates
    ``tenant_connector_status``. ``cadence`` is a Phase-1 daily cron expression
    (``"M H * * *"``); the scheduler scans every 5 minutes and fires due rows.

    Note: a successful ``commit_ingestion`` already auto-schedules a sensible default daily
    cadence server-side — call this only when the owner wants a DIFFERENT cadence (it overwrites
    idempotently, never duplicates).
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="schedule_recurring_pull")
    if resolved is None:
        return lane_tenant_error("schedule_recurring_pull")
    tenant_id = str(resolved)

    from datetime import UTC, datetime

    from orchestrator.db import tenant_connection
    from orchestrator.integrations.scheduler import _compute_next_run

    try:
        next_run = _compute_next_run(cadence, datetime.now(UTC))
    except ValueError as exc:
        return {"connector_id": connector_id, "status": "error", "error": str(exc)}
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
        "schedule_recurring_pull tenant=%s connector=%s cadence=%s next=%s",
        tenant_id, connector_id, cadence, next_run.isoformat(),
    )
    return {"scheduled": "true", "next_run": next_run.isoformat()}


@tool
def verify_connector(tenant_id: str, connector_id: str) -> dict[str, Any]:
    """Truthful current status for ``connector_id`` — the evidence for a
    completed/blocked/needs_owner_input report. Reads REAL DB state, never asserts.
    Returns ``{"connector_id", "connected", "phase", "pending_awaiting", "cadence", "last_status",
    "consecutive_fails", "rows_ingested_today"}`` — every field ``None``/``False`` when absent
    (e.g. no recurring pull scheduled yet), never fabricated.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="verify_connector")
    if resolved is None:
        return lane_tenant_error("verify_connector")

    from orchestrator.db import tenant_connection
    from orchestrator.integrations.commit import is_connector_connected
    from orchestrator.onboarding.shopify_onboarding import (
        read_integration_state as _read_state,
    )

    connected = is_connector_connected(resolved, connector_id)
    state = _read_state(resolved) or {}
    pending = state.get("pending_owner_input") or {}

    with tenant_connection(resolved) as conn:
        row = conn.execute(
            "SELECT pull_cadence, last_status, consecutive_fails, rows_ingested_today "
            "FROM tenant_connector_status WHERE tenant_id = %s AND connector_id = %s",
            (str(resolved), connector_id),
        ).fetchone()
    status_row = dict(row) if row is not None else {}

    return {
        "connector_id": connector_id,
        "connected": connected,
        "phase": state.get("phase"),
        "pending_awaiting": pending.get("awaiting"),
        "cadence": status_row.get("pull_cadence"),
        "last_status": status_row.get("last_status"),
        "consecutive_fails": status_row.get("consecutive_fails"),
        "rows_ingested_today": status_row.get("rows_ingested_today"),
    }


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
    list_supported_connectors,
    read_integration_state,
    start_oauth,
    check_oauth_status,
    pull_sample,
    propose_mapping,
    confirm_mapping,
    commit_ingestion,  # VT-268: proposal only — see docstring; never writes the ledger itself.
    schedule_recurring_pull,
    verify_connector,
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
