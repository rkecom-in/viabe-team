"""VT-425 Phase A — conversational Shopify onboarding (the journey→agent seam + resume).

CL-443 (Fazal STANDING 2026-06-25): the conversational agent is the PRIMARY tenant surface;
complex I/O link-outs into the WA in-app browser RETURN to chat (the VT-267 handoff+resume).

This module is the LAUNCH-CRITICAL minimum that onboards a Shopify tenant entirely in chat:

    journey completes  →  [SEAM]  →  start_setup (mint authorize_url link-out)
                                       ↓  owner taps the link, approves in WA browser, returns
    inbound "done"     →  [RESUME] →  re-check connector status (tenant_oauth_tokens)
                                       ↓  status connected → pull_sample (mock-injectable)
                                       →  fixed-schema auto-map → ingest_customer_rows (server-side)
                                       →  phase_5_confirmed (recurring ingestion scheduled)

It MIRRORS the VT-367 deterministic journey discipline (paced, idempotent on message_sid,
fail-OPEN, RLS-scoped via ``tenant_connection``) rather than relying on the LLM integration
agent to orchestrate tool calls across distinct per-message threads (each inbound gets a fresh
thread_id, so the checkpointer carries nothing — resume is DB-state, never thread-state).

DE-STUB MAP (the 3 launch tools, REAL counterparts cited):
  * ``start_connector_setup``  → ``ShopifyConnector.build_oauth_install_url`` + the VT-289
                                  ``mint_install_state`` nonce → a real ``authorize_url`` link-out.
  * ``pull_sample``            → ``ShopifyConnector.pull_sample`` (injectable for the Phase-A canary;
                                  the LIVE real-merchant OAuth+pull is DEFERRED to VT-422 Partner app).
  * commit/ingest              → ``shopify_sample_row_to_canonical`` (fixed schema, NO mapping form)
                                  → ``ingest_customer_rows(acquired_via='shopify')`` SERVER-SIDE
                                  (NOT an agent tool — the agent is fail-CLOSED against write tools).

PII (BINDING — CL-104 / CL-390 / CL-426 / CL-422):
  * Names-only to any LLM: Phase A's fixed-schema map has NO LLM call, so no phone/email reaches
    a prompt by construction. The owner-facing chat prompts are COUNTS only (e.g. "found N
    customers") — never a raw phone/email/name.
  * Counts-only logging: every log line here is counts/phase, never raw PII.
  * Never fabricate a number: the canary injects a MOCK connector returning a small clearly-marked
    sample; no DB-persisted fake numbers. Real external-customer pull is post-VT-231 (CL-422).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator.db import tenant_connection

logger = logging.getLogger(__name__)

_CONNECTOR_ID = "shopify"

# Phase machine (the 031 CHECK enum). Phase A drives these deterministically.
PHASE_DISCOVERY = "phase_1_discovery"
PHASE_AUTH = "phase_2_auth"
PHASE_SAMPLE = "phase_3_sample_pull"
PHASE_MAPPING = "phase_4_field_mapping"
PHASE_CONFIRMED = "phase_5_confirmed"

# Deterministic affirmations the owner sends after completing the link-out (EN + HI/Hinglish),
# token-exact (the approval_reply / journey discipline — never an LLM guess on the hot path).
_DONE = {
    "done", "ok", "okay", "connected", "finished", "complete", "completed", "yes", "y",
    "ho gaya", "hogaya", "ho", "gaya", "haan", "ha", "kar diya", "kardiya", "हो", "गया",
    "हाँ", "हां", "कर", "दिया", "जुड़", "गई",
}


def _tokens(body: str) -> set[str]:
    import re

    norm = (body or "").strip().casefold().replace("'", "")
    return {t for t in re.split(r"[\s,.!?;:।/\\-]+", norm) if t}


# --- PendingOwnerInput envelope helpers (shape gated by the Pydantic model) ----------------


def _validated_pending(
    *,
    awaiting: str,
    prompt_text: str,
    connector_id: str | None = None,
    walkthrough_url: str | None = None,
    expires_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build + VALIDATE a pending_owner_input envelope through the Pydantic model
    (031 mandates the model gates JSONB writes), returning the JSON-able dict."""
    from orchestrator.agent.integration_agent import PendingOwnerInput

    model = PendingOwnerInput(
        awaiting=awaiting,  # type: ignore[arg-type]
        prompt_text=prompt_text,
        connector_id=connector_id,
        walkthrough_url=walkthrough_url,
        expires_at=expires_at,
        metadata=metadata or {},
    )
    return model.model_dump()


def _write_state(
    tenant_id: UUID | str,
    *,
    phase: str,
    connector_id: str | None,
    pending: dict[str, Any] | None,
) -> None:
    """UPSERT the tenant's tenant_integration_state row (RLS-scoped). Mirrors
    first_data_step.floor._persist's UPSERT shape."""
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            """
            INSERT INTO tenant_integration_state
                (tenant_id, phase, current_connector_id, pending_owner_input)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE SET
                phase = EXCLUDED.phase,
                current_connector_id = EXCLUDED.current_connector_id,
                pending_owner_input = EXCLUDED.pending_owner_input,
                updated_at = now()
            """,
            (
                str(tenant_id),
                phase,
                connector_id,
                Jsonb(pending) if pending is not None else None,
            ),
        )


def read_integration_state(tenant_id: UUID | str) -> dict[str, Any] | None:
    """Return ``{phase, current_connector_id, pending_owner_input}`` or None. RLS-scoped."""
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT phase, current_connector_id, pending_owner_input "
            "FROM tenant_integration_state WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    return {"phase": row[0], "current_connector_id": row[1], "pending_owner_input": row[2]}


def _pending_is_unexpired(pending: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    """A pending_owner_input is live iff it exists, has an ``awaiting`` kind, and either
    carries no ``expires_at`` or one in the future. An expired/empty envelope → not live."""
    if not isinstance(pending, dict) or not pending.get("awaiting"):
        return False
    exp = pending.get("expires_at")
    if not exp:
        return True
    try:
        expires_at = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True  # unparseable → treat as live (fail-OPEN; never strand the owner)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > (now or datetime.now(UTC))


# --- DE-STUB 1: start_connector_setup (real Shopify authorize_url link-out) -----------------


def start_shopify_setup(
    tenant_id: UUID | str,
    shop: str,
    *,
    ttl_minutes: int = 10,
) -> dict[str, str]:
    """Mint the REAL Shopify ``authorize_url`` link-out + write the oauth_completion
    pending-state (the VT-267 handoff). Mirrors api/shopify_oauth.shopify_setup EXACTLY:
    validate the shop domain, mint the VT-289 single-use nonce, build the authorize URL.

    Returns ``{"authorize_url": ...}``. Key is ``authorize_url`` (NOT ``auth_url``).
    """
    from orchestrator.integrations.connectors.shopify import (
        ShopifyConnector,
        validate_shop_domain,
    )
    from orchestrator.integrations.oauth_state import mint_install_state

    from datetime import timedelta

    shop = validate_shop_domain(shop)  # raises ShopDomainError on a bad domain (never trust input)
    state = mint_install_state(tenant_id, _CONNECTOR_ID, target=shop, ttl_minutes=ttl_minutes)
    authorize_url = ShopifyConnector().build_oauth_install_url(
        UUID(str(tenant_id)), shop, state=state
    )
    # The pending expiry mirrors the VT-289 nonce TTL window so the resume hook honours the
    # SAME window the authorize link is valid for (start + ttl_minutes).
    pending_expiry = _iso((datetime.now(UTC) + timedelta(minutes=ttl_minutes)).replace(microsecond=0))
    pending = _validated_pending(
        awaiting="oauth_completion",
        prompt_text=(
            "Tap this link to connect your Shopify store, approve the access, "
            "then reply 'done' here and I'll pull in your customers."
        ),
        connector_id=_CONNECTOR_ID,
        walkthrough_url=authorize_url,
        expires_at=pending_expiry,
        metadata={"shop": shop},
    )
    _write_state(tenant_id, phase=PHASE_AUTH, connector_id=_CONNECTOR_ID, pending=pending)
    logger.info(
        "VT-425 start_shopify_setup tenant=%s connector=%s phase=%s (authorize_url minted)",
        tenant_id, _CONNECTOR_ID, PHASE_AUTH,
    )
    return {"authorize_url": authorize_url}


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --- Connector status read (resume gate: "did the owner finish the OAuth?") -----------------


def shopify_is_connected(tenant_id: UUID | str) -> bool:
    """True iff a Shopify OAuth/credential token row exists for the tenant — the durable,
    DB-truth signal that the owner completed the install (the callback persisted a
    tenant_oauth_tokens row). RLS-scoped. This is the resume status re-check."""
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT 1 FROM tenant_oauth_tokens "
            "WHERE tenant_id = %s AND connector_id = %s LIMIT 1",
            (str(tenant_id), _CONNECTOR_ID),
        ).fetchone()
    return row is not None


# --- DE-STUB 2+3: pull_sample → fixed-schema auto-map → server-side ingest -------------------

# A connector factory is injectable so the Phase-A canary can pass a MOCK Shopify connector
# (the live OAuth+pull is deferred to Fazal's VT-422 Partner app). Production default builds the
# real ShopifyConnector.
ConnectorFactory = Callable[[], Any]


def _default_connector_factory() -> Any:
    from orchestrator.integrations.connectors.shopify import ShopifyConnector

    return ShopifyConnector()


def pull_and_ingest_shopify(
    tenant_id: UUID | str,
    *,
    connector_factory: ConnectorFactory = _default_connector_factory,
) -> dict[str, int]:
    """The Phase-A commit: real ``pull_sample`` → fixed-schema auto-map (NO mapping form,
    NO LLM) → ``ingest_customer_rows(acquired_via='shopify')`` SERVER-SIDE.

    The ingest write runs HERE (not as an agent tool) — the integration agent is fail-CLOSED
    against ledger/write tools (VT-268). Returns counts only (no PII).
    """
    from orchestrator.integrations.connectors.shopify import shopify_sample_row_to_canonical
    from orchestrator.integrations.ingest import CanonicalRow, ingest_customer_rows

    connector = connector_factory()
    sample = connector.pull_sample(UUID(str(tenant_id)))  # real /customers + /checkouts
    rows: list[CanonicalRow] = []
    for raw in sample:
        mapped = shopify_sample_row_to_canonical(raw)
        if mapped is not None:
            rows.append(mapped)

    summary = ingest_customer_rows(tenant_id, rows, acquired_via=_CONNECTOR_ID)
    logger.info(
        "VT-425 pull_and_ingest_shopify tenant=%s sample_rows=%d mapped=%d committed=%d "
        "sales_written=%d (counts only — no PII)",
        tenant_id, len(sample), len(rows), summary.committed, summary.sales_written,
    )
    return {
        "sample_rows": len(sample),
        "mapped": len(rows),
        "committed": summary.committed,
        "ambiguous": summary.ambiguous,
        "dropped": summary.dropped,
        "sales_written": summary.sales_written,
        "sales_skipped_duplicate": summary.sales_skipped_duplicate,
    }


# --- The JOURNEY → INTEGRATION SEAM (option (a) sequential handoff, plan §8) -----------------


def begin_shopify_onboarding(tenant_id: UUID | str, recipient: str | None) -> None:
    """SEAM: the VT-367 journey has completed profile-confirm → hand off to connector
    onboarding. Sets phase_1_discovery + a connector_choice pending-state asking for the
    owner's Shopify domain, and sends the opening nudge in chat. Best-effort send (WABA-gated)."""
    pending = _validated_pending(
        awaiting="connector_choice",
        prompt_text=(
            "Great — your profile's set. To start finding sales to recover, connect your "
            "store. If you sell on Shopify, reply with your store address "
            "(it looks like yourstore.myshopify.com)."
        ),
        connector_id=_CONNECTOR_ID,
    )
    _write_state(tenant_id, phase=PHASE_DISCOVERY, connector_id=_CONNECTOR_ID, pending=pending)
    _send(recipient, pending["prompt_text"])
    logger.info(
        "VT-425 begin_shopify_onboarding tenant=%s phase=%s (journey→integration seam)",
        tenant_id, PHASE_DISCOVERY,
    )


def _send(recipient: str | None, text: str) -> None:
    """Best-effort owner send (WABA-gated/stubbed — never crash the pipeline). Mirrors
    journey._send. The recipient phone is hashed inside the send util (CL-390)."""
    if not recipient or not text:
        return
    try:
        from orchestrator.utils.twilio_send import send_freeform_message

        send_freeform_message(text, recipient)
    except Exception:  # noqa: BLE001 — send is WABA-gated; state advances regardless
        logger.warning("VT-425: owner send failed (recipient hashed in send util) — state advanced")


# --- THE RESUME HOOK (closes the VT-267 chat-resume gap, plan §1a-bis) ----------------------


def maybe_resume_shopify_onboarding(
    tenant_id: UUID | str,
    body: str,
    message_sid: str | None,
    recipient: str | None,
    *,
    connector_factory: ConnectorFactory = _default_connector_factory,
) -> dict[str, Any] | None:
    """THE owner-inbound resume gate (mirrors maybe_handle_journey_reply). Returns a result
    dict if this inbound was consumed as a Shopify-onboarding step (caller short-circuits the
    brain), else None (fall through to the normal pipeline). **FAIL-OPEN**: any error → None.

    Resume semantics — DB-state driven (each inbound is a fresh thread; no checkpointer carry):
      * phase_1_discovery + connector_choice pending → the body is the owner's shop domain →
        mint the authorize_url link-out (start_shopify_setup), advance to phase_2_auth.
      * phase_2_auth + oauth_completion pending → an inbound is the owner saying "done" after the
        link-out → RE-CHECK shopify_is_connected (DB truth). Connected → pull+ingest, advance to
        phase_5_confirmed. Not yet connected → re-send the link, stay in phase_2_auth (no fabrication).
      * phase_5_confirmed → onboarding done; return None (let the normal brain handle the message).
    """
    try:
        # DPDP (compliance-critical): opt-out / DSR ALWAYS wins. This gate runs BEFORE pre_filter,
        # so it MUST NOT consume an opt-out as an onboarding step — fall through to pre_filter,
        # which routes it to the authoritative opt-out/DSR handler (mirrors the journey gate).
        from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

        if matches_opt_out_or_dsr(body or ""):
            return None

        state = read_integration_state(tenant_id)
        if state is None:
            return None  # no integration onboarding in flight → normal pipeline
        pending = state.get("pending_owner_input")
        if not _pending_is_unexpired(pending):
            return None  # nothing live to resume → normal pipeline
        phase = state.get("phase")
        awaiting = pending.get("awaiting") if isinstance(pending, dict) else None

        # --- discovery: capture the shop domain → mint the link-out -------------------------
        if phase == PHASE_DISCOVERY and awaiting == "connector_choice":
            from orchestrator.integrations.connectors.shopify import (
                ShopDomainError,
                validate_shop_domain,
            )

            shop_candidate = (body or "").strip().split()[0] if (body or "").strip() else ""
            try:
                validate_shop_domain(shop_candidate)
            except ShopDomainError:
                _send(
                    recipient,
                    "That doesn't look like a Shopify store address. It should look like "
                    "yourstore.myshopify.com — please reply with that.",
                )
                return {"done": False, "phase": phase, "routed": "shopify_discovery_retry"}
            result = start_shopify_setup(tenant_id, shop_candidate)
            _send(
                recipient,
                "Tap this link to connect your Shopify store, approve the access, then "
                f"reply 'done' here:\n{result['authorize_url']}",
            )
            return {"done": False, "phase": PHASE_AUTH, "routed": "shopify_setup_minted"}

        # --- auth: owner says "done" → re-check connector status (DB truth) ------------------
        if phase == PHASE_AUTH and awaiting == "oauth_completion":
            toks = _tokens(body)
            if not (toks & _DONE):
                # Not a completion affirmation — re-send the link, stay put (no guessing).
                walkthrough = pending.get("walkthrough_url") if isinstance(pending, dict) else None
                if walkthrough:
                    _send(
                        recipient,
                        "When you've connected Shopify, reply 'done'. Here's the link "
                        f"again if you need it:\n{walkthrough}",
                    )
                return {"done": False, "phase": phase, "routed": "shopify_auth_waiting"}
            if not shopify_is_connected(tenant_id):
                # Owner said done but the callback hasn't persisted a token yet — DO NOT
                # fabricate progress. Re-prompt, stay in phase_2_auth.
                walkthrough = pending.get("walkthrough_url") if isinstance(pending, dict) else None
                _send(
                    recipient,
                    "I don't see the connection yet — please finish approving on the Shopify "
                    "page, then reply 'done'."
                    + (f"\n{walkthrough}" if walkthrough else ""),
                )
                return {"done": False, "phase": phase, "routed": "shopify_auth_not_connected"}

            # Connected — pull a sample, fixed-schema auto-map, ingest server-side, confirm.
            counts = pull_and_ingest_shopify(tenant_id, connector_factory=connector_factory)
            _schedule_recurring(tenant_id)
            done_pending = _validated_pending(
                awaiting="cadence_choice",
                prompt_text=(
                    f"Done — I connected your Shopify store and found {counts['committed']} "
                    "customers. I'll keep them up to date and start spotting sales to recover."
                ),
                connector_id=_CONNECTOR_ID,
            )
            _write_state(
                tenant_id, phase=PHASE_CONFIRMED, connector_id=_CONNECTOR_ID, pending=done_pending
            )
            _send(recipient, done_pending["prompt_text"])
            logger.info(
                "VT-425 shopify onboarding CONFIRMED tenant=%s committed=%d (counts only)",
                tenant_id, counts["committed"],
            )
            return {
                "done": True,
                "phase": PHASE_CONFIRMED,
                "routed": "shopify_ingested",
                "committed": counts["committed"],
            }

        # phase_5_confirmed (or any other phase): onboarding not in a resumable step → normal flow.
        return None
    except Exception:  # noqa: BLE001 — owner-inbound HOT PATH: any failure falls through, never blocks
        logger.exception(
            "maybe_resume_shopify_onboarding failed tenant=%s — fall through", tenant_id
        )
        return None


def _schedule_recurring(tenant_id: UUID | str) -> None:
    """Best-effort: schedule the daily recurring Shopify pull (the existing, already-REAL
    setup_recurring_ingestion path). A scheduling failure must not block confirmation."""
    try:
        from datetime import datetime as _dt

        from orchestrator.graph import get_pool
        from orchestrator.integrations.scheduler import _compute_next_run

        cadence = "0 3 * * *"  # daily 03:00 — a Phase-1 cron expression
        next_run = _compute_next_run(cadence, _dt.now(UTC))
        pool = get_pool()
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO tenant_connector_status (
                    tenant_id, connector_id, pull_cadence, next_scheduled_run, enabled
                ) VALUES (%s, %s, %s, %s, TRUE)
                ON CONFLICT (tenant_id, connector_id) DO UPDATE SET
                    pull_cadence = EXCLUDED.pull_cadence,
                    next_scheduled_run = EXCLUDED.next_scheduled_run,
                    enabled = TRUE,
                    updated_at = now()
                """,
                (str(tenant_id), _CONNECTOR_ID, cadence, next_run),
            )
    except Exception:  # noqa: BLE001 — recurring schedule is best-effort; confirmation already set
        logger.warning("VT-425: recurring-ingestion schedule failed tenant=%s (non-blocking)", tenant_id)


__all__ = [
    "PHASE_DISCOVERY",
    "PHASE_AUTH",
    "PHASE_SAMPLE",
    "PHASE_MAPPING",
    "PHASE_CONFIRMED",
    "read_integration_state",
    "shopify_is_connected",
    "start_shopify_setup",
    "pull_and_ingest_shopify",
    "begin_shopify_onboarding",
    "maybe_resume_shopify_onboarding",
]
