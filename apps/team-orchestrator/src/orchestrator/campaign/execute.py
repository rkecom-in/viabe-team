"""VT-251 — campaign execution seam.

`execute_approved_campaign` fans out an approved campaign to all recipients in
campaign_recipients, calls VT-45 send_whatsapp_template per recipient, and
advances campaigns.status to 'sent'.

Design decisions (Cowork-ruled 2026-05-31):
- D1: idempotency_key = f"{campaign_id}:{customer_id}" — stable per recipient,
  dedupes replays without additional state.
- D2: this seam SENDS + records campaign_messages + sets campaigns.status='sent'
  and STOPS. Attribution is async on the existing close path (VT-176 trigger /
  VT-46 match_transactions). Do NOT compute attribution here.

Architecture:
- `conn` is the tenant-scoped connection (SET LOCAL app.current_tenant in
  effect) for loading recipients, writing skip markers, and updating
  campaigns.status.
- VT-45 (send_whatsapp_template) manages its own pool-connection internally.
  It is called with just the payload; it fetches its pool from get_pool() in
  prod, or uses an injected pool passed as `send_pool`.
- Per recipient: short-circuit opted_out / blocked before calling VT-45
  (defence-in-depth; VT-45 already gates consent, but we skip the call and
  record a 'skipped_opt_out' campaign_messages row instead of an 'unauthorized'
  one — cleaner audit trail + avoids unnecessary pool churn).
- VT-45 handles idempotency, phone resolution, rate limiting, and
  campaign_messages recording for sent/error recipients.
- Partial-failure: per-recipient try/except → record send_status='error' in
  campaign_messages + continue (NOT fatal; mirror VT-241 reject discipline).
- After loop: advance the campaign status to 'sent'. Return count-only summary.

Pillars:
- CL-421: consent gate — never message opted_out / blocked (hard-refuse).
- CL-422: dev holds synthetic data only until VT-231 (prod Mumbai).
- CL-390: log tenant_id / customer_id / campaign_id / status / SID only.
  No PII (no phone, no names, no param values) in any log line.
- CL-418: callers use explicit `git add`; module itself has no git awareness.

VT-321 (#20 complaint-freeze, NON-configurable, fail-closed):
- An OPEN complaint freezes ALL selling to that customer — no exceptions, never
  owner-overridable. This seam enforces it DETERMINISTICALLY: a recipient with
  complaint_status == 'open' is skipped BEFORE the VT-45 send, a distinct
  'skipped_complaint_freeze' marker is written, and it is counted separately
  from opt-out skips. Fail-closed: only 'open' triggers the freeze; missing /
  'none' / 'resolved' / NULL → sellable (status-only column, no complaint
  content at rest — CL-390).
"""

from __future__ import annotations

import logging
from typing import Any, Callable
from uuid import UUID

from orchestrator.db.wrappers import CampaignsWrapper, CustomersWrapper
from orchestrator.knowledge.l2_writer import record_customer_action_marker
from orchestrator.agent.tools.send_whatsapp_template import (
    SendWhatsappTemplateInput,
    SendWhatsappTemplateOutput,
    send_whatsapp_template,
)

logger = logging.getLogger(__name__)

# Opt-out statuses that are hard-refused without calling VT-45.
# VT-45 also gates these, but we short-circuit to write a
# 'skipped_opt_out' marker (cleaner audit trail than 'unauthorized').
_REFUSED_OPT_OUT_STATUSES = frozenset({"opted_out", "blocked", "owner_excluded"})  # VT-84

# VT-321 (#20 complaint-freeze): an OPEN complaint freezes ALL selling to that
# customer — non-configurable, no exceptions. Fail-closed: ONLY 'open' triggers
# the freeze. 'none' / 'resolved' / NULL / missing → sellable. Single-value
# frozenset (not a bare string) so the gate reads identically to the opt-out one
# and any future freeze states slot in here.
_COMPLAINT_FREEZE_STATUSES = frozenset({"open"})


def _load_recipients(
    conn: Any,
    tenant_id: str,
    campaign_id: str,
) -> list[dict[str, Any]]:
    """Load campaign_recipients joined to their customer status flags.

    Returns list of dicts: {customer_id, opt_out_status, complaint_status}.
    RLS is already scoped via SET LOCAL app.current_tenant on conn.

    CL-390: no phone / name fetched here — opt_out_status + complaint_status
    only (both are status flags, not PII; VT-321 stores no complaint content at
    rest). Phone resolution is done inside VT-45 (Pillar 3: tool owns phone
    access).
    """
    # VT-306: the campaign_recipients⋈customers status read is encapsulated by the
    # wrapper (tenant-matched join) on the caller's tenant-scoped conn.
    rows = CustomersWrapper().list_recipients_for_campaign(
        tenant_id, campaign_id, conn=conn
    )
    # Fail-closed but tolerant: a row missing complaint_status (e.g. a pre-091
    # fixture) is treated as None → sellable; the send-loop gate only freezes on
    # an explicit 'open'.
    return [
        {
            "customer_id": r["customer_id"],
            "opt_out_status": r["opt_out_status"],
            "complaint_status": r.get("complaint_status"),
        }
        for r in rows
    ]


def _load_campaign(
    conn: Any,
    tenant_id: str,
    campaign_id: str,
) -> dict[str, Any] | None:
    """Load the campaign's template_id + body_params from ``plan_json``.

    Returns None if not found (RLS invisible = cross-tenant or missing).

    VT-140 fix: migration 018 (campaigns_v1) DROPPED the dedicated
    ``template_id`` + ``body_params`` columns and replaced them with a single
    ``plan_json`` JSONB carrying the full CampaignPlan v1.0. The send template +
    its params therefore live at ``plan_json -> 'message_plan' ->
    {template_id, template_params, language}`` (collapse persists
    ``CampaignPlanProposed.model_dump(mode='json')``). The original VT-251 query
    read the dropped columns and raised UndefinedColumn against the real schema
    (the unit tests used MagicMock connections, so the break never surfaced).
    Read from plan_json instead. The language is carried alongside the params so
    the execute loop can pass it to VT-45 without a second column.
    """
    # VT-306: load the campaign via the wrapper, then read message_plan from the
    # plan_json dict in Python (find_by_id returns the full row; JSONB -> dict).
    row = CampaignsWrapper().find_by_id(tenant_id, campaign_id, conn=conn)
    if row is None:
        return None
    plan = (row.get("plan_json") or {}).get("message_plan") or {}
    template_id = plan.get("template_id")
    params = dict(plan.get("template_params") or {})
    language = plan.get("language")
    # Carry language inside body_params under the reserved _language key; the
    # execute loop pops it back out (keeps _load_campaign's return shape stable).
    if language:
        params["_language"] = language
    return {"template_id": template_id, "body_params": params}


def _write_skip_ledger(
    conn: Any,
    tenant_id: str,
    customer_id: str,
    idempotency_key: str,
    *,
    reason: str = "opt_out",
) -> None:
    """Record a send_idempotency_keys row for a skip (no send made).

    send_status='skipped' (VT-261 / migration 053). The skip marker is written
    under a DISTINCT ``skip:`` key namespace (VT-262), NOT the live-send key, so
    it dedupes repeated skips (ON CONFLICT DO NOTHING) WITHOUT colliding with the
    real-send idempotency key. The prior bug: writing 'skipped' under the bare
    send key {campaign_id}:{customer_id} meant a later legitimate send to the
    same pair (e.g. after the customer re-subscribes) hit that 'skipped' row in
    _check_idempotency, which echoed a status the VT-45 output Literal cannot
    represent -> pydantic ValidationError -> swallowed as a phantom db_error AND
    the re-eligible recipient suppressed for 24h. Decoupling the namespace fixes
    it: a real re-send sees no prior row under its own key.

    ``reason`` distinguishes skip kinds in the key namespace so an opt-out skip
    and a VT-321 complaint-freeze skip for the SAME pair never collide
    (``skip:opt_out:...`` vs ``skip:complaint_freeze:...``). send_status stays
    'skipped' for both — the only value the CHECK (migration 053) permits; the
    distinct REASON lives in the key namespace + the caller's counter/log line,
    not in a new send_status value.

    CL-390: no PII in this INSERT (customer_id is a UUID; phone is NOT stored).
    """
    skip_key = f"skip:{reason}:{idempotency_key}"
    conn.execute(
        """
        INSERT INTO send_idempotency_keys
            (tenant_id, idempotency_key, customer_id, message_sid, send_status)
        VALUES (%s, %s, %s, NULL, 'skipped')
        ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
        """,
        (tenant_id, skip_key, customer_id),
    )


def _write_opt_out_skip_ledger(
    conn: Any,
    tenant_id: str,
    customer_id: str,
    idempotency_key: str,
) -> None:
    """Opt-out skip marker (CL-421). Thin wrapper over ``_write_skip_ledger``.

    Kept as a named entry point because VT-261's real-DB test imports it
    directly. Writes under the ``skip:opt_out:`` namespace.
    """
    _write_skip_ledger(
        conn, tenant_id, customer_id, idempotency_key, reason="opt_out"
    )


def _write_complaint_freeze_skip_ledger(
    conn: Any,
    tenant_id: str,
    customer_id: str,
    idempotency_key: str,
) -> None:
    """VT-321 (#20) complaint-freeze skip marker. Thin wrapper.

    Writes under the DISTINCT ``skip:complaint_freeze:`` namespace so a freeze
    skip never collides with an opt-out skip for the same campaign:customer pair
    (the freeze and the opt-out are independent reasons; either alone suffices to
    skip). send_status='skipped' like every other skip. CL-390: customer_id only.
    """
    _write_skip_ledger(
        conn, tenant_id, customer_id, idempotency_key, reason="complaint_freeze"
    )


def _advance_campaign_status(
    conn: Any,
    tenant_id: str,
    campaign_id: str,
) -> None:
    """Advance the campaign status to sent (VT-306: via the wrapper, tenant-predicated)."""
    CampaignsWrapper().set_status(tenant_id, campaign_id, "sent", conn=conn)


# Type alias for the injectable send function (matches VT-45's public API).
# The injected callable receives (payload, *, pool) -> SendWhatsappTemplateOutput.
_SendFn = Callable[..., SendWhatsappTemplateOutput]


def execute_approved_campaign(
    tenant_id: str | UUID,
    campaign_id: str | UUID,
    *,
    conn: Any,
    send_template_fn: _SendFn | None = None,
    send_pool: Any | None = None,
) -> dict[str, int]:
    """Fan out an approved campaign to all recipients and mark it sent.

    Parameters
    ----------
    tenant_id:
        UUID of the owning tenant (str or UUID).
    campaign_id:
        UUID of the approved campaign (str or UUID).
    conn:
        Open psycopg3 connection with SET LOCAL app.current_tenant already
        applied (RLS-scoped). Used for: loading recipients, writing opt_out
        skip markers, and advancing campaigns.status. VT-45 uses its own pool
        internally (or send_pool if injected).
    send_template_fn:
        Injected for tests. Defaults to `send_whatsapp_template` from VT-45.
        Signature: (payload: SendWhatsappTemplateInput, *, pool=None)
        -> SendWhatsappTemplateOutput.
    send_pool:
        Optional pool injected alongside send_template_fn in tests. Passed
        through to send_template_fn as the `pool` kwarg.

    Returns
    -------
    dict with counts: {sent, skipped_opt_out, skipped_complaint_freeze, failed}.
    No PII (CL-390): counts only, no customer ids, no SIDs.

    VT-321 (#20): a recipient whose customers.complaint_status == 'open' is
    skipped BEFORE the VT-45 send (fail-closed, non-configurable), a distinct
    'skipped_complaint_freeze' marker is written, and it is tallied in the
    ``skipped_complaint_freeze`` count — separate from ``skipped_opt_out``.

    Raises
    ------
    RuntimeError if the campaign row is not found (cross-tenant or missing).

    Partial failures are NOT raised — they are recorded in campaign_messages
    with send_status='error' and included in the 'failed' count.

    D2: Attribution is deferred to the VT-176 close trigger. This function
    does NOT call match_transactions or get_attribution_data.
    """
    tenant_id_str = str(tenant_id)
    campaign_id_str = str(campaign_id)

    # VT-328 (VT-365) — cancelled tenants must NOT dispatch outbound customer campaigns. This is
    # THE single enforcement point (Pillar 8): every present/future caller funnels through this fn,
    # not just the supervisor node. Phase is derived SERVER-SIDE from the tenant's own row via the
    # RLS-scoped conn — never a client field (IDOR, VT-293/294). Short-circuit BEFORE loading
    # recipients or sending; inbound owner replies never reach here, so they stay untouched.
    from orchestrator.billing.graceful_exit import dispatch_allowed

    _guard_row = conn.execute(
        "SELECT phase FROM tenants WHERE id = %s", (tenant_id_str,)
    ).fetchone()
    if _guard_row is None:
        raise RuntimeError(
            f"execute_approved_campaign: tenant {tenant_id_str} not found (dispatch guard)"
        )
    _phase = _guard_row["phase"] if isinstance(_guard_row, dict) else _guard_row[0]
    if not dispatch_allowed(_phase):
        logger.info(
            "execute_approved_campaign: dispatch_blocked tenant=%s campaign=%s phase=%s",
            tenant_id_str, campaign_id_str, _phase,
        )
        return {
            "sent": 0,
            "skipped_opt_out": 0,
            "skipped_complaint_freeze": 0,
            "failed": 0,
            "dispatch_blocked": 1,
        }

    # VT-460 gaps (a)+(b): the SHARED onboarded (Gate-0) + WABA-live pre-gate — the SAME deterministic
    # rail the agent path runs at agent_send_draft Gate-0/0b. Before VT-460 the campaign path reached
    # real customer sends WITHOUT the onboarded/activation bar and discovered a not-live WABA only as
    # a downstream Twilio 4xx; this closes that asymmetry by REUSING the existing gate functions
    # (is_agent_eligible + wa_send_allowed) via the unified choke. Fail-closed: a non-onboarded /
    # not-live tenant sends ZERO and the campaign short-circuits (count-only summary, no PII).
    from orchestrator.agents.customer_send_choke import assert_customer_send_allowed

    # OC1 (VT-533): shadow the customer-send policy rail on this live campaign path (observe-only —
    # enforce_policy stays off, so the return is unchanged). Surfaces "enforcement would block here"
    # data (chiefly: no policy grant seeded yet) to de-risk flipping enforce_policy=True later.
    _pregate = assert_customer_send_allowed(
        tenant_id_str, agent="sales_recovery", conn=conn, observe_policy=True
    )
    if not _pregate.allowed:
        logger.info(
            "execute_approved_campaign: pre_gate_blocked tenant=%s campaign=%s reason=%s",
            tenant_id_str, campaign_id_str, _pregate.reason,
        )
        return {
            "sent": 0,
            "skipped_opt_out": 0,
            "skipped_complaint_freeze": 0,
            "failed": 0,
            "pre_gate_blocked": 1,
        }

    _send_fn: _SendFn = send_template_fn if send_template_fn is not None else send_whatsapp_template

    # --- Load campaign row (template_id + params) ---
    campaign = _load_campaign(conn, tenant_id_str, campaign_id_str)
    if campaign is None:
        raise RuntimeError(
            f"execute_approved_campaign: campaign {campaign_id_str} not found "
            f"for tenant {tenant_id_str} — cross-tenant or missing"
        )

    template_id: str = campaign["template_id"]
    # body_params may be a psycopg3-decoded dict (JSONB) or None.
    raw_params: dict[str, str] = dict(campaign["body_params"] or {})

    # Derive language from body_params if the caller embedded it there,
    # otherwise default to "en" (Phase-1: single locale until hi is live).
    language: str = raw_params.pop("_language", "en")
    body_params: dict[str, str] = raw_params

    # --- Load recipients ---
    recipients = _load_recipients(conn, tenant_id_str, campaign_id_str)

    logger.info(
        "execute_approved_campaign: tenant=%s campaign=%s recipients=%d "
        "template=%s",
        tenant_id_str, campaign_id_str, len(recipients), template_id,
    )

    sent = 0
    skipped_opt_out = 0
    skipped_complaint_freeze = 0
    failed = 0

    # VT-460 gap (c): the gated extent of the customer-send fan-out. The transport refuses a
    # customer send outside this context; the campaign path is a legitimate gated caller (the
    # onboarded + WABA pre-gate ran above; per-recipient consent/opt-out/complaint gates run in the
    # loop + VT-45). A future direct un-gated caller to a customer phone fails closed.
    from orchestrator.utils.twilio_send import customer_send_context

    with customer_send_context():
        for recipient in recipients:
            customer_id_str = recipient["customer_id"]
            opt_out_status: str | None = recipient.get("opt_out_status")
            complaint_status: str | None = recipient.get("complaint_status")
            idempotency_key = f"{campaign_id_str}:{customer_id_str}"

            # --- VT-321 complaint-freeze gate (#20, fail-closed, non-configurable) ---
            # An OPEN complaint freezes ALL selling to this customer — no exceptions,
            # never owner-overridable. Checked FIRST and independently of opt-out:
            # we do NOT call VT-45, write a distinct 'complaint_freeze' skip marker,
            # count it separately, and continue. Fail-closed: ONLY 'open' triggers;
            # missing / 'none' / 'resolved' / NULL → sellable.
            if complaint_status in _COMPLAINT_FREEZE_STATUSES:
                try:
                    _write_complaint_freeze_skip_ledger(
                        conn, tenant_id_str, customer_id_str, idempotency_key,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Skip-ledger write is best-effort; the no-call is the actual
                    # freeze. Log (no PII) but don't fail the loop.
                    logger.info(
                        "execute_approved_campaign: complaint_skip_ledger_write_error "
                        "tenant=%s customer=%s err=%s",
                        tenant_id_str, customer_id_str, type(exc).__name__,
                    )
                skipped_complaint_freeze += 1
                logger.info(
                    "execute_approved_campaign: skipped_complaint_freeze tenant=%s "
                    "customer=%s status=%s",
                    tenant_id_str, customer_id_str, complaint_status,
                )
                continue

            # --- Defence-in-depth consent gate (CL-421) ---
            # VT-45 also gates this, but we short-circuit to write a
            # 'skipped_opt_out' marker instead of an 'unauthorized' envelope
            # (cleaner audit trail; avoids unnecessary pool churn).
            if opt_out_status in _REFUSED_OPT_OUT_STATUSES:
                try:
                    _write_opt_out_skip_ledger(
                        conn, tenant_id_str, customer_id_str, idempotency_key,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Writing the skip ledger row is best-effort; log but don't
                    # fail the loop (defence-in-depth: the no-call is sufficient).
                    logger.info(
                        "execute_approved_campaign: skip_ledger_write_error "
                        "tenant=%s customer=%s err=%s",
                        tenant_id_str, customer_id_str, type(exc).__name__,
                    )
                skipped_opt_out += 1
                logger.info(
                    "execute_approved_campaign: skipped_opt_out tenant=%s "
                    "customer=%s status=%s",
                    tenant_id_str, customer_id_str, opt_out_status,
                )
                continue

            # --- Send via VT-45 (handles idempotency, rate limit, campaign_messages) ---
            try:
                payload = SendWhatsappTemplateInput(
                    tenant_id=tenant_id_str,
                    customer_id=customer_id_str,
                    template_id=template_id,
                    language=language,  # type: ignore[arg-type]
                    template_params=body_params,
                    idempotency_key=idempotency_key,
                )
                result: SendWhatsappTemplateOutput = _send_fn(payload, pool=send_pool)
            except Exception as exc:  # noqa: BLE001
                # Unexpected exception from the send path — log and continue.
                logger.info(
                    "execute_approved_campaign: send_exception tenant=%s "
                    "customer=%s err=%s",
                    tenant_id_str, customer_id_str, type(exc).__name__,
                )
                failed += 1
                continue

            if result.status in ("sent", "dry_run"):
                sent += 1
                logger.info(
                    "execute_approved_campaign: sent tenant=%s customer=%s "
                    "sid=%s status=%s",
                    tenant_id_str, customer_id_str,
                    result.message_sid, result.status,
                )
                # VT-320: the agent ACTED on this customer (a recovery contact) →
                # record a customer-referencing L2 marker so VT-76's reconstitution
                # sweep has real rows to anonymize on opt-out (else a forever no-op).
                # Best-effort + idempotent per (customer, campaign): a marker failure
                # must NOT break the send loop. Own tenant_connection (RLS GUC).
                try:
                    record_customer_action_marker(
                        tenant_id_str, customer_id_str,
                        action="campaign_send", dedup_source=campaign_id_str,
                    )
                except Exception:  # noqa: BLE001 — marker is best-effort observability
                    logger.info(
                        "execute_approved_campaign: vt320_marker_error tenant=%s customer=%s",
                        tenant_id_str, customer_id_str,
                    )
            elif result.status == "unauthorized":
                # VT-45 refused — opt_out caught by the tool (should not reach
                # here if our defence-in-depth gate runs first, but VT-45's
                # consent gate is authoritative). Count as skipped.
                skipped_opt_out += 1
                logger.info(
                    "execute_approved_campaign: unauthorized tenant=%s customer=%s "
                    "code=%s",
                    tenant_id_str, customer_id_str,
                    result.error_envelope.code if result.error_envelope else "unknown",
                )
            else:
                # rate_limited, error, or any other non-success status.
                failed += 1
                logger.info(
                    "execute_approved_campaign: send_failed tenant=%s customer=%s "
                    "status=%s code=%s",
                    tenant_id_str, customer_id_str,
                    result.status,
                    result.error_envelope.code if result.error_envelope else "none",
                )

    # --- Advance campaign status → sent (D2: stop here, no attribution) ---
    # VT-65 PR-2: the status UPDATE + campaign_sent emit (with the cohort for
    # TARGETED edges) atomic in one txn — NOT the I/O send loop above.
    from orchestrator.knowledge.kg_emit import drain_kg_events, emit_kg_event
    from orchestrator.knowledge.kg_vocab import KgEventType

    with conn.transaction():
        _advance_campaign_status(conn, tenant_id_str, campaign_id_str)
        emit_kg_event(conn, KgEventType.CAMPAIGN_SENT, tenant_id_str, {
            "campaign_id": campaign_id_str,
            "customer_ids": [r["customer_id"] for r in recipients],
        })
    drain_kg_events(tenant_id_str)

    summary = {
        "sent": sent,
        "skipped_opt_out": skipped_opt_out,
        "skipped_complaint_freeze": skipped_complaint_freeze,
        "failed": failed,
    }
    logger.info(
        "execute_approved_campaign: done tenant=%s campaign=%s summary=%s",
        tenant_id_str, campaign_id_str, summary,
    )
    return summary


__all__ = ["execute_approved_campaign"]
