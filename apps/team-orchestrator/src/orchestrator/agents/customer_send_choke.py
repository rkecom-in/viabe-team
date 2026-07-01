"""VT-460 — the unified deterministic customer-send choke (shared pre-gate + send-class).

The rail-harness finding: the FULL gate stack lives only in ``customer_send.agent_send_draft``.
Two other paths reach real customer sends WITHOUT the load-bearing onboarded (Gate-0) + WABA-live
pre-gate:
  - the CAMPAIGN path (``campaign.execute.execute_approved_campaign``), and
  - the inbound SESSION path (``integrations.customer_inbound.handle_customer_inbound``).

This module is the ONE place those rails are made CONSISTENT. It does NOT re-implement the gates —
it REUSES the existing functions (``onboarding_gate.is_agent_eligible`` for the onboarded/activation
bar, ``whatsapp_account.wa_send_allowed`` for the Meta-verified WABA-live gate) and exposes a single
shared pre-gate every customer-send path calls before dispatching:

    assert_customer_send_allowed(tenant_id, *, agent, conn) -> CustomerSendGate

The per-recipient consent / opt-out / complaint / caps gates stay where they are:
  - the agent path keeps the full ``agent_send_draft`` stack (gates 2-5),
  - the campaign + tool path keep the VT-45 ``send_whatsapp_template`` consent/opt-out/complaint
    re-reads (CL-421/VT-301/VT-321),
  - the inbound session path is a DISTINCT class (gap d) — first-contact intro + opt-in/opt-out
    acks send to a NOT-YET-consented customer ON PURPOSE (lawful opt-in solicitation, gated by the
    intro-once guard), so it is NOT folded under the marketing-consent gate. It STILL passes the
    onboarded + WABA pre-gate.

This is the SAFETY bound, not a per-send owner approval (design §6 autonomy model): the team acts
autonomously within these deterministic rails; the owner is not in the loop per message.

CL-390: IDs + boolean skip codes only — never a phone, name, or fact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


class CustomerSendClass(str, Enum):
    """The audited class of a customer send. Distinct classes have distinct consent semantics.

    - ``MARKETING``      — business-initiated marketing to a CONSENTED customer (agent win-back,
                           approved campaign). Requires the version-aware marketing-consent gate.
    - ``SESSION_OPTIN``  — VT-287 inbound session class (gap d): first-contact intro + opt-in /
                           opt-out acks, sent inside the 24h customer-initiated window to a
                           not-yet-consented customer (lawful opt-in solicitation). Gated by the
                           intro-once guard + opt-out, NOT the marketing-consent ledger — folding
                           it under the marketing gate would WRONGLY suppress the opt-in invite.
    """

    MARKETING = "marketing"
    SESSION_OPTIN = "session_optin"


# Distinct fail-closed skip markers for the shared pre-gate (mirror the customer_send.SKIP_* style).
SKIP_NOT_ONBOARDED = "skipped_not_onboarded"   # onboarded/activation bar not crossed (Gate-0)
SKIP_WABA_NOT_LIVE = "skipped_waba_not_live"   # WABA not Meta-verified 'live' (universal pre-gate)
SKIP_OUT_OF_POLICY = "skipped_out_of_policy"   # VT-474 A2: the send is outside the owner's policy bound


@dataclass(frozen=True, slots=True)
class CustomerSendGate:
    """Outcome of the shared onboarded + WABA (+ VT-474 policy) pre-gate. ``reason`` is a SKIP_*
    marker when blocked; ``policy_reason`` carries the deterministic OUT_OF_POLICY code when the
    block was the policy bound (which bound was breached — for the escalation/owner-notify path)."""

    allowed: bool
    reason: str | None = None
    policy_reason: str | None = None


def assert_customer_send_allowed(
    tenant_id: UUID | str,
    *,
    agent: str = "sales_recovery",
    conn: Any,
    segment: str | None = None,
    enforce_policy: bool = False,
    observe_policy: bool = False,
) -> CustomerSendGate:
    """The shared, deterministic onboarded + WABA-live (+ VT-474 policy) pre-gate for EVERY
    customer-send path.

    REUSES the existing gate functions (does not re-implement them):
      1. ``onboarding_gate.is_agent_eligible(tenant_id, agent, conn=conn)`` — the Gate-0
         onboarded/activation bar (journey-complete + verification + ≥1 data source + ≥1 customer,
         per the activation registry). Fail-closed (unknown/NULL/error → ineligible).
      2. ``whatsapp_account.wa_send_allowed(tenant_id)`` — the universal WABA-live gate (Meta
         business-verification + privacy URL). Fail-closed (no row / non-live → False).
      3. (VT-474 A2, when ``enforce_policy``) ``business_policy.assert_within_policy`` — the OUTER
         policy bound: CUSTOMER_SEND must be an allowed action type AND the target ``segment`` must be
         allowed. An out-of-policy campaign segment is blocked here, BEFORE the per-recipient
         consent/opt-out/caps gates — the brain cannot target a segment the owner never granted. The
         compliance rails (consent/opt-out/onboarded — VT-460) are UNTOUCHED and still bind; policy is
         an ADDITIONAL outer bound, not a replacement. ``enforce_policy`` defaults False so the
         existing callers/tests are byte-for-byte unchanged; a lane opts in by passing the segment +
         ``enforce_policy=True`` once the onboarding policy grant exists.

    Both compliance gates fail CLOSED. ``conn`` is the caller's RLS-scoped ``tenant_connection``; ALL
    reads run on it (one connection, one RLS scope), so RLS independently confirms the tenant and a
    caller with no substrate pool still works.

    Returns ``CustomerSendGate(allowed=False, reason=SKIP_*)`` on the FIRST failing gate, else
    ``CustomerSendGate(allowed=True)``. This is the pre-gate ONLY — the per-recipient
    consent/opt-out/complaint/caps gates run in the caller's existing stack.
    """
    from orchestrator.agents.onboarding_gate import is_agent_eligible
    from orchestrator.integrations.whatsapp_account import wa_send_allowed

    tid = str(tenant_id)

    # Gate-0: onboarded / activation (reused — the SAME predicate agent_send_draft Gate-0 calls).
    if not is_agent_eligible(tid, agent, conn=conn):
        logger.info(
            "customer_send_choke: pre-gate blocked tenant=%s agent=%s reason=%s",
            tid, agent, SKIP_NOT_ONBOARDED,
        )
        return CustomerSendGate(allowed=False, reason=SKIP_NOT_ONBOARDED)

    # Universal WABA-live pre-gate (reused — was pre-checked ONLY in customer_inbound before VT-460).
    # Pass the caller's RLS-scoped conn so the read runs on it (no second pool connection).
    if not wa_send_allowed(tid, conn=conn):
        logger.info(
            "customer_send_choke: pre-gate blocked tenant=%s agent=%s reason=%s",
            tid, agent, SKIP_WABA_NOT_LIVE,
        )
        return CustomerSendGate(allowed=False, reason=SKIP_WABA_NOT_LIVE)

    # VT-474 A2: the OUTER policy bound (opt-in per caller). Deterministic — the brain cannot target
    # an out-of-policy segment. Fail-closed (no policy row → out of policy → blocked).
    if enforce_policy:
        from orchestrator.agents.business_policy import (
            PolicyActionClass,
            assert_within_policy,
        )

        check = assert_within_policy(
            tid, PolicyActionClass.CUSTOMER_SEND, {"segment": segment}, conn=conn
        )
        if check.out_of_policy:
            logger.info(
                "customer_send_choke: pre-gate blocked tenant=%s agent=%s reason=%s policy=%s",
                tid, agent, SKIP_OUT_OF_POLICY, check.reason,
            )
            return CustomerSendGate(
                allowed=False, reason=SKIP_OUT_OF_POLICY, policy_reason=check.reason
            )
    elif observe_policy:
        # OC1 (VT-533) — SHADOW the policy rail without enforcing: evaluate the SAME check and record
        # the would-be block to tm_audit (observe-only). This surfaces the operational truth needed to
        # safely flip ``enforce_policy=True`` later — chiefly "no policy grant exists yet, so
        # enforcement would block every send". NEVER changes the return; fully fail-soft (a shadow
        # eval error must not touch the compliance pre-gate).
        try:
            from orchestrator.agents.business_policy import (
                PolicyActionClass,
                assert_within_policy,
            )

            check = assert_within_policy(
                tid, PolicyActionClass.CUSTOMER_SEND, {"segment": segment}, conn=conn
            )
            if check.out_of_policy:
                _record_policy_shadow(tid, agent=agent, reason=check.reason, conn=conn)
        except Exception:  # noqa: BLE001 — shadow is observe-only; never break the pre-gate
            logger.warning(
                "customer_send_choke: policy shadow eval failed (fail-soft) tenant=%s", tid,
                exc_info=True,
            )

    return CustomerSendGate(allowed=True)


def _record_policy_shadow(tid: str, *, agent: str, reason: str | None, conn: Any) -> None:
    """OC1 — record a would-be policy block as an observe-only tm_audit row (``decides`` layer)."""
    from orchestrator.observability.decorators import _observability_context
    from orchestrator.observability.tm_audit import emit_tm_audit

    ctx = _observability_context.get()
    emit_tm_audit(
        event_layer="decides",
        event_kind="policy_shadow",
        actor=agent or "team_manager",
        tenant_id=tid,
        run_id=ctx.run_id if ctx is not None else None,
        summary=(
            "customer_send policy rail would BLOCK (observe-only; enforce_policy is off) — "
            f"reason={reason}"
        ),
        decision={"action_class": "customer_send", "policy_reason": reason, "would_block": True},
        severity="info",
        status="observed",
        conn=conn,
    )


__all__ = [
    "CustomerSendClass",
    "CustomerSendGate",
    "SKIP_NOT_ONBOARDED",
    "SKIP_WABA_NOT_LIVE",
    "SKIP_OUT_OF_POLICY",
    "assert_customer_send_allowed",
]
