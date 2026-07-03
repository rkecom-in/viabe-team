"""VT-425 Phase A — conversational Shopify onboarding (the journey→agent seam + resume).

CL-443 (Fazal STANDING 2026-06-25): the conversational agent is the PRIMARY tenant surface;
complex I/O link-outs into the WA in-app browser RETURN to chat (the VT-267 handoff+resume).

This module is the LAUNCH-CRITICAL minimum that onboards a Shopify tenant entirely in chat:

    journey completes  →  [SEAM]  →  start_setup (mint authorize_url link-out)
                                       ↓  owner taps the link, approves in WA browser, returns
    inbound "done"     →  [RESUME] →  re-check connector status (tenant_oauth_tokens)
                                       ↓  status connected → pull_orders (mock-injectable)
                                       →  fixed-schema auto-map → ingest_customer_rows (server-side)
                                       →  phase_5_confirmed (recurring ingestion scheduled)

It MIRRORS the VT-367 deterministic journey discipline (paced, idempotent on message_sid,
fail-OPEN, RLS-scoped via ``tenant_connection``) rather than relying on the LLM integration
agent to orchestrate tool calls across distinct per-message threads (each inbound gets a fresh
thread_id, so the checkpointer carries nothing — resume is DB-state, never thread-state).

DE-STUB MAP (the 3 launch tools, REAL counterparts cited):
  * ``start_connector_setup``  → ``ShopifyConnector.build_oauth_install_url`` + the VT-289
                                  ``mint_install_state`` nonce → a real ``authorize_url`` link-out.
  * ``pull_orders``            → ``ShopifyConnector.pull_orders`` (/orders.json — the sale-of-record;
                                  injectable for the Phase-A canary; LIVE real-merchant OAuth+pull
                                  DEFERRED to VT-422 Partner app). VT-447: orders, NOT abandoned checkouts
                                  — SR recovers lapsed BUYERS. ``pull_sample`` is retained for a future
                                  abandoned-cart-recovery agent.
  * commit/ingest              → ``shopify_order_to_canonical`` (fixed schema, NO mapping form; reads
                                  ``processed_at`` — the real transaction date)
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
import re
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
    norm = (body or "").strip().casefold().replace("'", "")
    return {t for t in re.split(r"[\s,.!?;:।/\\-]+", norm) if t}


# VT-588 — the DISCOVERY-phase off-script router. The owner who is asked for a store address may reply
# with a domain (anywhere in the sentence), a malformed domain ATTEMPT, or something else entirely (a
# question, a "later", ordinary chat). Only the first two are the resume gate's business; the third
# must reach the manager brain, not a canned "that's not a store address" reprompt.
def _scan_shop_domain(body: str) -> str | None:
    """Return the FIRST whitespace token in ``body`` that validates as a Shopify store domain, else
    None. Accepts a domain given ANYWHERE in a sentence ("here it is: mystore.myshopify.com"), not just
    as the bare first token — the offer beat's VT-587 same-message pickup handles the affirm-plus-URL
    case, but an owner still often replies to the address ask with a short sentence, not the bare host."""
    from orchestrator.integrations.connectors.shopify import (
        ShopDomainError,
        validate_shop_domain,
    )

    for tok in (body or "").split():
        candidate = tok.strip().strip(".,!?;:\"'()<>[]")
        try:
            validate_shop_domain(candidate)
            return candidate
        except ShopDomainError:
            continue
    return None


# A domain-SHAPED token: a URL scheme, or a host whose LAST label is a RECOGNISED tld / a shopify label.
# The tld gate is the discriminator (VT-588 adversarial review): a bare ``word.word2`` dotted bigram is
# NOT a domain attempt — a Hinglish owner on mobile with no space after a period ("haan.theek hai",
# "ok.thanks", "yes.done it") must FALL THROUGH to the brain, not earn the canned "that's not a store
# address" reprompt. Only a genuine (if malformed) store address ("mystore.myshopify", "mystore.shopify.com",
# "mystore.com", "www.store.in", "https://…") trips this. An exotic real TLD not listed simply falls
# through to the brain (safe direction — under-reprompt beats over-reprompting ordinary chat).
_DOMAIN_TLDS = (
    "myshopify|shopify|com|net|org|in|co|io|store|shop|biz|info|xyz|online|site|app|dev"
)
_DOMAIN_SHAPED_RE = re.compile(
    rf"(https?://|\b[a-z0-9][a-z0-9-]*\.(?:{_DOMAIN_TLDS})\b)", re.IGNORECASE
)


def _is_domain_attempt(body: str) -> bool:
    """True when ``body`` carries a domain-shaped token (a URL, or a host ending in a recognised tld /
    shopify label) that failed validation — a malformed store address ("mystore.shopify.com" wrong
    subdomain, "mystore.myshopify" missing .com). Such an attempt earns the specific 'that's not a valid
    address' reprompt; ANY message without such a token — a question, a proceed intent, ordinary chat, a
    Hinglish "haan.theek hai", even one that merely says the word 'shopify' — is fallen through to the
    manager brain by the caller (VT-588)."""
    return bool(_DOMAIN_SHAPED_RE.search(body or ""))


# VT-583 (CL-2026-07-03-conversing-surfaces-and-harness): the auth-phase INTENT classifier. The _DONE
# token floor above stays the FAST, DETERMINISTIC path (an unambiguous "done"/"ho gaya" short-circuits
# to the DB re-check). Only a NON-floor reply reaches this classifier, which reads its intent as one of
# done | link | other so the owner who says "its connected now" / "ho gaya install" advances (still
# gated by the authoritative DB shopify_is_connected re-check), the owner who wants a fresh link ("send
# again", "link nahi mila") gets it re-minted, and a question/other gets an HONEST status reply — never
# the canned re-prompt and never silence. Fail-soft → None (caller then treats it as a non-done reply).
_AUTH_INTENT_MODEL = "claude-haiku-4-5-20251001"  # cheap classifier tier (parity with question_brain)
_AUTH_INTENT_TIMEOUT_S = 12.0
_VALID_AUTH_INTENTS = frozenset({"done", "link", "other"})


def _anthropic_key_present() -> bool:
    """A usable (non-sentinel) Anthropic key is on the env — so a unit/CI run with no key never makes a
    live call (the classifier degrades to None and the caller keeps the deterministic non-done path)."""
    import os

    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    return bool(key) and not key.lower().startswith(("test", "sentinel", "dummy", "sk-ant-test"))


def _llm_classify_auth_intent(body: str) -> str | None:
    """Classify a NON-floor auth-phase reply as done|link|other via Haiku. JSON-only, bounded,
    fail-soft → None on any failure (no key / LLM error / timeout / unparseable / off-label)."""
    if not _anthropic_key_present():
        return None
    try:
        import json as _json

        from anthropic import Anthropic

        prompt = (
            "A small Indian business owner was asked to tap a link, connect their Shopify store, and "
            "reply when finished. Classify their reply's INTENT as exactly one of:\n"
            "- done: they say the store is connected / they finished (e.g. 'done', 'ho gaya', "
            "'its connected now', 'installed it')\n"
            "- link: they can't find the link or want it resent (e.g. 'send the link again', "
            "'link nahi mila', 'resend')\n"
            "- other: a question, an unrelated message, or anything that is not a 'done' or a "
            "link request\n"
            "Judge the MEANING across Hindi / Hinglish / English.\n"
            f'Reply: "{(body or "").strip()[:400]}"\n'
            'Return ONLY a JSON object: {"intent": "done|link|other"}. No prose.'
        )
        resp = Anthropic().messages.create(
            model=_AUTH_INTENT_MODEL,
            max_tokens=60,
            messages=[{"role": "user", "content": prompt}],
            timeout=_AUTH_INTENT_TIMEOUT_S,
        )
        raw = resp.content[0].text if resp.content else ""
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        obj = _json.loads(raw[start : end + 1])
        intent = str((obj or {}).get("intent") or "").strip().lower()
        return intent if intent in _VALID_AUTH_INTENTS else None
    except Exception as exc:  # noqa: BLE001 — best-effort; any failure → None (caller keeps deterministic path)
        logger.warning("VT-583: shopify auth-intent classify failed (%s) — deterministic path", type(exc).__name__)
        return None


def classify_auth_intent(body: str, *, llm_fn: Callable[[str], str | None] | None = None) -> str | None:
    """Classify a non-floor auth-phase reply as done|link|other, or None (→ caller's deterministic
    non-done path). ``llm_fn`` is injectable so tests drive classification without a live call."""
    fn = llm_fn or _llm_classify_auth_intent
    try:
        intent = fn(body)
    except Exception:  # noqa: BLE001 — an injected/real classifier error → None (deterministic path)
        return None
    return intent if intent in _VALID_AUTH_INTENTS else None


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


def has_live_resume(tenant_id: UUID | str) -> bool:
    """VT-583 D2 — True iff there is a LIVE connector-onboarding step waiting on the owner's next
    reply: an integration state in a resumable phase (discovery / auth) with an unexpired
    ``pending_owner_input``. The journey's integration-in-flight beat calls this to decide whether the
    downstream resume gate will consume the message (→ let it) or the flow has been orphaned (→ re-offer
    so the owner is never dropped to the cold brain). Fail-soft → False (treat as orphaned; re-offering
    is the safe, no-silence direction)."""
    try:
        state = read_integration_state(tenant_id)
        if not state:
            return False
        if state.get("phase") not in (PHASE_DISCOVERY, PHASE_AUTH):
            return False
        return _pending_is_unexpired(state.get("pending_owner_input"))
    except Exception:  # noqa: BLE001 — a state read must never block owner inbound; assume orphaned
        logger.warning("VT-583: has_live_resume read failed tenant=%s (fail-soft → False)", tenant_id)
        return False


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


# --- DE-STUB 2+3: pull_orders → fixed-schema auto-map → server-side ingest (VT-447) ----------

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
    """The Phase-A commit: real ``pull_orders`` (the sale-of-record substrate) → fixed-schema
    auto-map (NO mapping form, NO LLM) → ``ingest_customer_rows(acquired_via='shopify')`` SERVER-SIDE.

    VT-447: the onboarding sales-history source is real ORDERS (``/orders.json``), NOT abandoned
    checkouts — Sales-Recovery recovers lapsed BUYERS, so the substrate must be actual orders.
    ``pull_sample`` (/customers + /checkouts) is retained for a future abandoned-cart-recovery agent,
    not used here. The order mapper reads ``processed_at`` (the real transaction date).

    The ingest write runs HERE (not as an agent tool) — the integration agent is fail-CLOSED
    against ledger/write tools (VT-268). Returns counts only (no PII).
    """
    from orchestrator.integrations.connectors.shopify import shopify_order_to_canonical
    from orchestrator.integrations.ingest import CanonicalRow, ingest_customer_rows

    connector = connector_factory()
    orders = connector.pull_orders(UUID(str(tenant_id)))  # real /orders.json (sale-of-record)
    rows: list[CanonicalRow] = []
    skipped_non_inr = 0
    for raw in orders:
        mapped = shopify_order_to_canonical(raw)
        if mapped.skipped_non_inr:
            skipped_non_inr += 1
        if mapped.row is not None:
            rows.append(mapped.row)

    summary = ingest_customer_rows(tenant_id, rows, acquired_via=_CONNECTOR_ID)
    logger.info(
        "VT-447 pull_and_ingest_shopify tenant=%s orders_pulled=%d mapped=%d committed=%d "
        "sales_written=%d skipped_non_inr=%d (counts only — no PII)",
        tenant_id, len(orders), len(rows), summary.committed, summary.sales_written, skipped_non_inr,
    )
    return {
        "orders_pulled": len(orders),
        "mapped": len(rows),
        "committed": summary.committed,
        "ambiguous": summary.ambiguous,
        "dropped": summary.dropped,
        "sales_written": summary.sales_written,
        "sales_skipped_duplicate": summary.sales_skipped_duplicate,
        "skipped_non_inr": skipped_non_inr,
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
    _send(recipient, pending["prompt_text"], tenant_id=tenant_id)
    logger.info(
        "VT-425 begin_shopify_onboarding tenant=%s phase=%s (journey→integration seam)",
        tenant_id, PHASE_DISCOVERY,
    )


def _send(recipient: str | None, text: str, *, tenant_id: UUID | str | None = None) -> None:
    """Best-effort owner send (WABA-gated/stubbed — never crash the pipeline). Mirrors
    journey._send. The recipient phone is hashed inside the send util (CL-390).

    ``tenant_id`` (VT-586) — threaded into the send-choke so every resume-gate reply (shop-domain
    retry, auth waiting line, not-connected re-prompt, connected confirm) records to the LIFETIME
    conversation_log ('assistant' leg, surface='journey'). Before VT-586 these reached the owner's
    phone but never hit conversation_log — the Team-Manager's 24h window lost the entire integration
    hand-off, and the server harness read every resume reply as false 'silence'."""
    if not recipient or not text:
        return
    try:
        from orchestrator.utils.twilio_send import send_freeform_message

        send_freeform_message(text, recipient, tenant_id=tenant_id, surface="journey")
    except Exception:  # noqa: BLE001 — send is WABA-gated; state advances regardless
        logger.warning("VT-425: owner send failed (recipient hashed in send util) — state advanced")


# --- VT-583: auth-phase honest waiting line + link re-mint (never silence, never fabricate) --------


def _auth_waiting_line(walkthrough: str | None) -> str:
    """The HONEST auth-waiting reply — sent for EVERY non-done, non-link reply so the owner is never
    dropped (VT-583 D3 fixes the :405 silent edge where an absent walkthrough_url sent nothing). Shows
    the link when we have it; otherwise tells them how to get a fresh one."""
    base = "Looks like your Shopify store isn't connected yet — tap the link I sent to finish, then reply 'done'."
    if walkthrough:
        return f"{base}\n{walkthrough}"
    return f"{base} If you need the link again, just say 'link'."


def _remint_auth_link(
    tenant_id: UUID | str, pending: dict[str, Any] | None, recipient: str | None
) -> bool:
    """Re-mint a FRESH authorize link from the shop stored on the pending envelope (never fabricate a
    domain). Returns True if a fresh link was sent, False if we had to ask for the store address. Either
    way SOMETHING is sent (no silent path)."""
    shop = None
    if isinstance(pending, dict):
        shop = (pending.get("metadata") or {}).get("shop")
    if not shop:
        _send(
            recipient,
            "I don't have your store address on file — reply with it (it looks like "
            "yourstore.myshopify.com) and I'll send a fresh connect link.",
            tenant_id=tenant_id,
        )
        return False
    try:
        result = start_shopify_setup(tenant_id, str(shop))
        _send(
            recipient,
            "Here's a fresh link to connect your Shopify store — tap it, approve the access, then "
            f"reply 'done':\n{result['authorize_url']}",
            tenant_id=tenant_id,
        )
        return True
    except Exception:  # noqa: BLE001 — a re-mint failure still owes the owner an honest line, never silence
        logger.warning("VT-583: shopify link re-mint failed tenant=%s (fail-soft)", tenant_id)
        _send(recipient, _auth_waiting_line(None), tenant_id=tenant_id)
        return False


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
            shop_candidate = _scan_shop_domain(body)
            if shop_candidate is None:
                # VT-588: no valid domain in the reply. A genuine but malformed domain ATTEMPT gets
                # the specific reprompt; ANYTHING ELSE (a question, a proceed intent, ordinary chat)
                # FALLS THROUGH to the manager brain (return None) — the brain answers off-script from
                # the 24h window + the onboarding-state block, then re-nudges to the store-address ask.
                # The flow state persists (pending_owner_input stays live), so the owner's NEXT domain
                # re-engages this gate. Kills the canned-reprompt-to-a-question defect (topic_switch
                # "what do you charge?" / context_retention "did you get my store address?").
                if _is_domain_attempt(body):
                    _send(
                        recipient,
                        "That doesn't look like a Shopify store address. It should look like "
                        "yourstore.myshopify.com — please reply with that.",
                        tenant_id=tenant_id,
                    )
                    return {"done": False, "phase": phase, "routed": "shopify_discovery_retry"}
                return None  # off-script → manager brain (never a canned reprompt to a real question)
            result = start_shopify_setup(tenant_id, shop_candidate)
            _send(
                recipient,
                "Tap this link to connect your Shopify store, approve the access, then "
                f"reply 'done' here:\n{result['authorize_url']}",
                tenant_id=tenant_id,
            )
            return {"done": False, "phase": PHASE_AUTH, "routed": "shopify_setup_minted"}

        # --- auth: owner signals done → re-check connector status (DB truth) -----------------
        if phase == PHASE_AUTH and awaiting == "oauth_completion":
            toks = _tokens(body)
            walkthrough = pending.get("walkthrough_url") if isinstance(pending, dict) else None
            # FAST FLOOR: an unambiguous token "done"/"ho gaya" short-circuits to the DB re-check.
            is_done = bool(toks & _DONE)
            if not is_done:
                # VT-583: a NON-floor reply is intent-classified (done | link | other). The
                # authoritative DB re-check below still gates any "done"; nothing here fabricates
                # progress. Every branch SENDS (no silent path — the :405 edge is closed).
                intent = classify_auth_intent(body)
                if intent == "done":
                    is_done = True  # a done-intent phrasing → fall through to the DB re-check
                elif intent == "link":
                    reminted = _remint_auth_link(tenant_id, pending, recipient)
                    return {
                        "done": False,
                        "phase": phase,
                        "routed": "shopify_auth_link_reminted" if reminted else "shopify_auth_link_need_shop",
                    }
                else:
                    # question / other / classifier-unavailable → HONEST waiting line, ALWAYS sent.
                    _send(recipient, _auth_waiting_line(walkthrough), tenant_id=tenant_id)
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
                    tenant_id=tenant_id,
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
            _send(recipient, done_pending["prompt_text"], tenant_id=tenant_id)
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
    "has_live_resume",
    "shopify_is_connected",
    "start_shopify_setup",
    "pull_and_ingest_shopify",
    "begin_shopify_onboarding",
    "maybe_resume_shopify_onboarding",
]
