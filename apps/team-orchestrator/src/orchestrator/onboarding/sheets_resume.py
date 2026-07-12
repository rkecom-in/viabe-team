"""VT-608 fix round, CRITICAL 1 — the Google Sheets deterministic resume gate.

THE DEFECT this closes: ``sheets_oauth.start_sheets_oauth`` writes the SAME ``(phase_2_auth,
awaiting='oauth_completion')`` tuple ``shopify_onboarding.py`` uses. ``tenant_integration_state``
has exactly ONE row per tenant (031's PK is ``tenant_id`` alone) — before this fix, the ONLY
consumer of that tuple was ``maybe_resume_shopify_onboarding``, called unconditionally by
``runner.py`` regardless of which connector the tenant is actually onboarding. A Sheets-flow
tenant sitting at that phase dead-ended (``shopify_is_connected`` checks the WRONG connector's
token row, forever reporting "not connected") — or worse, a tenant with a PRIOR Shopify
connection already on file would have that stale token read as "done", firing
``pull_and_ingest_shopify`` (a Shopify order pull) off a Sheets "done" reply.

THE FIX: this module owns the Sheets half of the SAME auth-wait step Shopify's own hook owns —
deliberately NARROWER than Shopify's (no discovery-phase domain-scanning; no LLM auth-intent
classifier reuse, since that classifier's own prompt text is hardcoded to "connect their Shopify
store" and would misdescribe a Sheets flow to the model). Only the fast, deterministic ``_DONE``
token floor is honoured here; anything else falls through to the manager brain (``None``) — the
same safe direction ``maybe_resume_shopify_onboarding``'s own off-script branches already take.
The connector-routing dispatcher that decides WHICH of the two hooks to call lives in
``onboarding.connector_resume``.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

_CONNECTOR_ID = "google_sheet"

# DF1(a) mint-armed: an owner DATA-ACTION ("import my orders now", "map the Zodiac Sign column")
# while the OAuth is still pending (phase_2_auth, not yet connected) must be answered HONESTLY —
# never fabricate importing/mapping. A data-action verb + a data/column object; a bare "done"
# confirmation carries neither and is handled by the _DONE re-check.
_OWNER_DATA_ACTION_RE = re.compile(
    r"\b(?:import|pull|fetch|load|sync|map|mapping|bring)\b[^.?!]*?"
    r"\b(?:order|orders|sales?|customers?|data|columns?|amounts?|dates?|records?|numbers?)\b",
    re.IGNORECASE,
)


def maybe_resume_sheets_onboarding(
    tenant_id: UUID | str,
    body: str,
    message_sid: str | None,
    recipient: str | None,
) -> dict[str, Any] | None:
    """The Sheets auth-wait resume step. Returns a result dict if this inbound was consumed,
    else ``None`` (fall through to the normal pipeline). FAIL-OPEN: any error -> None.

    Resume semantics — DB-state driven, mirrors ``maybe_resume_shopify_onboarding``'s own shape:
      * phase_2_auth + oauth_completion pending + a floor "done" token -> re-check
        ``is_connector_connected`` (DB truth, generalized past Shopify). Connected -> advance to
        phase_3_sample_pull with a 'sample_pull_pending' waypoint (the picker/agent takes it from
        there — this gate's job ends at "OAuth confirmed"). Not yet connected -> honest waiting
        line, stay in phase_2_auth (never fabricate progress).
      * Anything else (a non-floor reply, any other phase) -> None (falls through to the brain —
        no canned reprompt to a genuine question, matching the Shopify gate's own discipline).
    """
    try:
        from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

        # DPDP (compliance-critical): opt-out / DSR ALWAYS wins — this gate runs BEFORE
        # pre_filter, so it must never consume an opt-out as an onboarding step.
        if matches_opt_out_or_dsr(body or ""):
            return None

        from orchestrator.onboarding.shopify_onboarding import (
            PHASE_AUTH,
            PHASE_SAMPLE,
            _DONE,
            _pending_is_unexpired,
            _send,
            _tokens,
            _validated_pending,
            _write_state,
            read_integration_state,
        )

        state = read_integration_state(tenant_id)
        if state is None:
            return None
        pending = state.get("pending_owner_input")
        if not _pending_is_unexpired(pending):
            return None
        phase = state.get("phase")
        awaiting = pending.get("awaiting") if isinstance(pending, dict) else None

        if phase != PHASE_AUTH or awaiting != "oauth_completion":
            # Sample-pull / mapping / commit are the LLM-driven integration_agent's own job
            # (the ten context-scoped tools each read/write this same state) — this deterministic
            # gate is scoped to the auth-wait step only.
            return None

        # DF1(a) mint-armed honesty — an owner DATA-ACTION ("import my orders", "map my columns")
        # while OAuth is still pending must NOT be fabricated. If the connection isn't finished, say
        # so + point at the one step. If it IS connected, fall through (the agent does the import).
        from orchestrator.integrations.commit import is_connector_connected

        if _OWNER_DATA_ACTION_RE.search(body or ""):
            if is_connector_connected(tenant_id, _CONNECTOR_ID):
                return None  # connected -> the integration agent handles the real import/mapping
            walkthrough = pending.get("walkthrough_url") if isinstance(pending, dict) else None
            _send(
                recipient,
                "I can't import or map that yet — your Google Sheet connection isn't finished. "
                "Approve on the Google page and reply 'done', then I'll pull your data in."
                + (f"\n{walkthrough}" if walkthrough else ""),
                tenant_id=tenant_id,
            )
            logger.info(
                "sheets_resume: owner data-action while OAuth pending -> honest not-connected "
                "(no fabrication) tenant=%s",
                tenant_id,
            )
            return {"done": False, "phase": phase, "routed": "sheets_data_action_not_connected"}

        toks = _tokens(body)
        if not (toks & _DONE):
            # No LLM auth-intent classifier reuse here (see module docstring) — a non-floor reply
            # falls through to the brain rather than risk a Shopify-worded misclassification.
            return None

        from orchestrator.integrations.commit import is_connector_connected

        if not is_connector_connected(tenant_id, _CONNECTOR_ID):
            walkthrough = pending.get("walkthrough_url") if isinstance(pending, dict) else None
            _send(
                recipient,
                "I don't see the connection yet — please finish approving on the Google page, "
                "then reply 'done'." + (f"\n{walkthrough}" if walkthrough else ""),
                tenant_id=tenant_id,
            )
            return {"done": False, "phase": phase, "routed": "sheets_auth_not_connected"}

        # Connected — this gate's job is done; hand off to the picker/agent for sheet selection.
        next_pending = _validated_pending(
            awaiting="sample_pull_pending",
            prompt_text=(
                "Your Google account is connected — next, pick which sheet you use for "
                "customers/orders and I'll take it from there."
            ),
            connector_id=_CONNECTOR_ID,
        )
        _write_state(tenant_id, phase=PHASE_SAMPLE, connector_id=_CONNECTOR_ID, pending=next_pending)
        _send(recipient, next_pending["prompt_text"], tenant_id=tenant_id)
        logger.info(
            "VT-608 sheets onboarding OAuth confirmed tenant=%s phase=%s", tenant_id, PHASE_SAMPLE
        )
        return {"done": False, "phase": PHASE_SAMPLE, "routed": "sheets_oauth_confirmed"}
    except Exception:  # noqa: BLE001 — owner-inbound HOT PATH: any failure falls through, never blocks
        logger.exception(
            "maybe_resume_sheets_onboarding failed tenant=%s — fall through", tenant_id
        )
        return None


__all__ = ["maybe_resume_sheets_onboarding"]
