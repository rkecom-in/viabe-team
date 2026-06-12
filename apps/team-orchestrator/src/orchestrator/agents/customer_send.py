"""VT-369 Gap-5 PR-1 — the ONE deterministic customer-send choke point for specialist agents.

``agent_send_draft`` is the SOLE path by which an agent-drafted message reaches
a customer (plan §3; CRITICAL-2: the drafting agents are tool-guardrailed away
from every send capability — they emit ``template_name + params`` into
``agent_drafts`` and THIS module sends). Zero LLM (Pillar 1).

The gate stack runs IN ORDER; every gate fails CLOSED with a distinct marker:

  1. batch state — ``approved`` (or mid-batch ``sending``) under L2. The L3
     ``auto_send_pending``-past-``send_not_before`` path is PR-3 scope and the
     branch raises ``NotImplementedError`` here (stub, never a silent send).
  2. template registry — resolves, has an approved SID, ``category ==
     'customer_marketing'``, and pins ``optout_line: true``. SID-less stubs
     (pre-F1) skip as ``skipped_template_not_configured``.
  3. customers row re-read AT SEND TIME — ``opt_out_status == 'subscribed'``
     AND ``complaint_status != 'open'``. Never trusted from draft time.
  4. version-aware marketing consent — ``has_marketing_consent_for_phone``
     over ``record_of_consent``, allowlisted by ``MARKETING_CONSENT_VERSIONS``
     (lives in ``sales_recovery_executor``; EMPTY until counsel clears C2 —
     structurally fail-closed: NO version resolves, NO agent send happens).
  5. agent caps — ``check_agent_send_caps``: tenant daily, customer weekly,
     30d recontact suppression, 2-per-90d ceiling (plan §3e — suppression is
     re-checked HERE, not only at detection).
  6. delegate to the EXISTING VT-45 path (``send_whatsapp_template``) with
     idempotency key ``agent:{draft_id}`` — the ``send_idempotency_keys``
     ledger check is check-then-act on the autocommit pool (the SAME semantics
     as today's VT-45 path, not a single transaction): a sequential replay
     cannot double-send; a crash in the window between Twilio success and the
     ledger INSERT re-sends on replay (pre-existing bounded residual, shared
     with VT-45). VT-45 independently re-runs opt-out/opt-in + the 5000/24h
     tenant cap underneath — defense-in-depth, not a fork.

On success: ``agent_drafts.status='sent'`` + ``message_sid``, an
``agent_customer_contacts`` ledger row (suppression/first-contact/audit
substrate), the batch advances ``approved → sending → sent`` when every draft
is terminal, and completion is reported via the ``agent_work_items`` status.

CL-390: no third-party PII in logs — tenant/draft/batch UUIDs, template_name
and skip markers only. Raw phone never appears in logs or results; it is read
from the RLS'd customers row and passed only into the existing send path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from orchestrator.db import tenant_connection
from orchestrator.templates_registry import TemplateRegistryError, resolve as registry_resolve
from orchestrator.utils.phone_token import hash_phone

logger = logging.getLogger(__name__)

# --- caps (plan §3e; F3 confirms thresholds — these are the proposed defaults) ---
AGENT_SEND_DAILY_TENANT_CAP = 200    # agent sends / tenant / 24h
AGENT_SEND_CUSTOMER_WEEKLY_CAP = 1   # agent sends / customer / 7d
RECONTACT_SUPPRESSION_DAYS = 30      # any agent contact <= 30d ago -> skip
MAX_AGENT_CONTACTS_PER_90D = 2       # lifetime-ish ceiling per rolling 90d
L3_DAILY_AUTO_SEND_CAP = 50          # defined NOW, ENFORCED in PR-3 (L3 auto-send)

# The only category the agent customer-send gate accepts (plan gate #2).
AGENT_SEND_CATEGORY = "customer_marketing"

# PR-1 sends the 'en' variant. agent_drafts (migration 126) carries no
# per-draft language column yet; the hi variant activates with the F1 SID
# drop + a per-draft language (flagged in the PR notes, not silently defaulted
# at F1 time).
_SEND_LANGUAGE = "en"

# --- distinct fail-closed skip markers (one per gate; plan §3 / agent_drafts.skip_reason) ---
SKIP_BATCH_NOT_APPROVED = "skipped_batch_not_approved"
SKIP_TEMPLATE_NOT_CONFIGURED = "skipped_template_not_configured"
SKIP_WRONG_CATEGORY = "skipped_wrong_category"
SKIP_NO_OPTOUT_LINE = "skipped_no_optout_line"
SKIP_OPT_OUT = "skipped_opt_out"
SKIP_COMPLAINT = "skipped_complaint"
SKIP_CUSTOMER_MISSING = "skipped_customer_missing"
SKIP_NO_PHONE = "skipped_no_phone"
SKIP_CONSENT = "skipped_consent"
SKIP_CAP_TENANT_DAILY = "skipped_cap_tenant_daily"
SKIP_CAP_CUSTOMER_WEEKLY = "skipped_cap_customer_weekly"
SKIP_SUPPRESSION_30D = "skipped_suppression_30d"
SKIP_CAP_90D = "skipped_cap_90d"

_DRAFT_TERMINAL_STATUSES = ("sent", "skipped", "halted")


@dataclass(frozen=True, slots=True)
class CapCheckResult:
    """Outcome of the agent-cap gate. ``reason`` is one of the SKIP_* markers."""

    allowed: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AgentSendResult:
    """Outcome of one agent draft send. PII-safe (UUIDs + markers only).

    ``status``: 'sent' | 'already_sent' | 'skipped' | 'failed'.
    """

    draft_id: str
    status: str
    skip_reason: str | None = None
    message_sid: str | None = None
    batch_status: str | None = None


def _col(row: Any, key: str, idx: int) -> Any:
    """Read a column from a psycopg row that may be a dict or a tuple."""
    return row[key] if isinstance(row, dict) else row[idx]


def _marketing_consent_versions() -> frozenset[str]:
    """The counsel-cleared consent-text versions for automated marketing (C2).

    Single source: ``sales_recovery_executor.MARKETING_CONSENT_VERSIONS`` —
    detection and the send gate MUST use the same allowlist. It is EMPTY until
    counsel clears C2, which makes this gate structurally fail-closed: that is
    deliberate, NOT a bug (plan §9 risk 5 — do not "fix" it; C2 resolution is
    a one-constant change + a ledger entry).

    Lazy import: avoids a module-import cycle with the executor and keeps the
    fallback fail-closed if the executor module is absent.
    """
    try:
        from orchestrator.agents import sales_recovery_executor
    except ImportError:  # executor lands in the same PR; absent == no versions
        return frozenset()
    return frozenset(sales_recovery_executor.MARKETING_CONSENT_VERSIONS)


def has_marketing_consent_for_phone(
    tenant_id: UUID | str,
    phone_e164: str,
    *,
    conn: Any = None,
    versions: frozenset[str] | None = None,
) -> bool:
    """Version-aware marketing-consent gate over ``record_of_consent`` (plan gate #4).

    True iff an ACTIVE consent row (``opted_out_at IS NULL``) exists for this
    phone token whose ``consent_text_version`` is in the marketing allowlist.
    Purpose limitation is MECHANICAL: pre-existing/transactional consents are
    structurally excluded until counsel clears their versions (C2).

    Fail-CLOSED: no row, opted-out row, version not allowlisted, or an EMPTY
    allowlist all return False. The allowlist check binds the versions as an
    array param (``= ANY(%s)``) — never an inline ``IN ()`` literal (MED-2:
    a SQL syntax error would break the fail-closed property). The empty set
    short-circuits in Python for the same reason.

    CL-390: the raw phone is tokenised here and never logged or persisted.
    """
    allow = versions if versions is not None else _marketing_consent_versions()
    if not allow:
        return False
    phone_token = hash_phone(phone_e164)
    query = (
        "SELECT 1 FROM record_of_consent "
        "WHERE tenant_id = %s AND phone_token = %s "
        "  AND opted_out_at IS NULL "
        "  AND consent_text_version = ANY(%s)"
    )
    params = (str(tenant_id), phone_token, list(allow))
    if conn is not None:
        return conn.execute(query, params).fetchone() is not None
    with tenant_connection(tenant_id) as own_conn:
        return own_conn.execute(query, params).fetchone() is not None


def check_agent_send_caps(
    tenant_id: UUID | str, customer_id: UUID | str, *, conn: Any
) -> CapCheckResult:
    """The agent-cap gate (plan §3e), counted over ``agent_customer_contacts``.

    Checked in order — tenant daily (200/24h), customer weekly (1/7d), 30d
    recontact suppression, 2-per-90d ceiling. Each failure returns its own
    marker. ``L3_DAILY_AUTO_SEND_CAP`` is defined above but enforced in PR-3
    (no L3 auto-send exists in PR-1). VT-45's 5000/24h tenant cap still
    applies underneath in the delegated send path.

    Known residual (accepted, plan §3e): COUNT-then-send TOCTOU overshoot is
    bounded by concurrency width (~2 messages) — not redesigned here.
    """
    tid, cid = str(tenant_id), str(customer_id)

    row = conn.execute(
        "SELECT count(*) AS c FROM agent_customer_contacts "
        "WHERE tenant_id = %s AND sent_at > now() - interval '24 hours'",
        (tid,),
    ).fetchone()
    if int(_col(row, "c", 0)) >= AGENT_SEND_DAILY_TENANT_CAP:
        return CapCheckResult(allowed=False, reason=SKIP_CAP_TENANT_DAILY)

    row = conn.execute(
        "SELECT "
        "  count(*) FILTER (WHERE sent_at > now() - interval '7 days')  AS weekly, "
        "  count(*) FILTER (WHERE sent_at > now() - make_interval(days => %s)) AS recent, "
        "  count(*) FILTER (WHERE sent_at > now() - interval '90 days') AS ninety "
        "FROM agent_customer_contacts WHERE tenant_id = %s AND customer_id = %s",
        (RECONTACT_SUPPRESSION_DAYS, tid, cid),
    ).fetchone()
    if int(_col(row, "weekly", 0)) >= AGENT_SEND_CUSTOMER_WEEKLY_CAP:
        return CapCheckResult(allowed=False, reason=SKIP_CAP_CUSTOMER_WEEKLY)
    if int(_col(row, "recent", 1)) >= 1:
        return CapCheckResult(allowed=False, reason=SKIP_SUPPRESSION_30D)
    if int(_col(row, "ninety", 2)) >= MAX_AGENT_CONTACTS_PER_90D:
        return CapCheckResult(allowed=False, reason=SKIP_CAP_90D)

    return CapCheckResult(allowed=True)


# --- internals ---------------------------------------------------------------


def _load_draft(conn: Any, tenant_id: str, draft_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT d.id::text AS draft_id, d.batch_id::text AS batch_id, "
        "       d.customer_id::text AS customer_id, d.template_name, d.params, "
        "       d.status AS draft_status, d.skip_reason, d.message_sid, "
        "       b.status AS batch_status, b.work_item_id::text AS work_item_id, b.agent "
        "FROM agent_drafts d "
        "JOIN agent_draft_batches b ON b.tenant_id = d.tenant_id AND b.id = d.batch_id "
        "WHERE d.tenant_id = %s AND d.id = %s",
        (tenant_id, draft_id),
    ).fetchone()
    if row is None:
        return None
    keys = (
        "draft_id", "batch_id", "customer_id", "template_name", "params",
        "draft_status", "skip_reason", "message_sid", "batch_status",
        "work_item_id", "agent",
    )
    return {k: _col(row, k, i) for i, k in enumerate(keys)}


def _mark_draft_skipped(conn: Any, tenant_id: str, draft_id: str, reason: str) -> None:
    """Terminal per-draft skip with its marker. Only ever from 'drafted'."""
    # VT-382 (CL-437.3): 'skipped' is terminal — params are no longer needed; the flip
    # and the redaction share ONE explicit transaction (the pool is autocommit). NO
    # audit row: nothing was sent (capture is for owner-facing SENT text only).
    from orchestrator.agents.outbox_redaction import redact_draft_params

    with conn.transaction():
        row = conn.execute(
            "UPDATE agent_drafts SET status = 'skipped', skip_reason = %s, updated_at = now() "
            "WHERE tenant_id = %s AND id = %s AND status = 'drafted' RETURNING id",
            (reason, tenant_id, draft_id),
        ).fetchone()
        if row is not None:
            redact_draft_params(conn, tenant_id, [draft_id])


def _finalize_batch_if_terminal(
    conn: Any, tenant_id: str, batch_id: str, work_item_id: str
) -> str:
    """Batch -> 'sent' when every draft is terminal; report via the work item status.

    Returns the batch's (possibly unchanged) current status.
    """
    row = conn.execute(
        "SELECT count(*) AS open_count FROM agent_drafts "
        "WHERE tenant_id = %s AND batch_id = %s "
        "  AND status NOT IN ('sent', 'skipped', 'halted')",
        (tenant_id, batch_id),
    ).fetchone()
    if int(_col(row, "open_count", 0)) > 0:
        row = conn.execute(
            "SELECT status FROM agent_draft_batches WHERE tenant_id = %s AND id = %s",
            (tenant_id, batch_id),
        ).fetchone()
        return str(_col(row, "status", 0))

    updated = conn.execute(
        "UPDATE agent_draft_batches SET status = 'sent', updated_at = now() "
        "WHERE tenant_id = %s AND id = %s AND status IN ('approved', 'sending') "
        "RETURNING id",
        (tenant_id, batch_id),
    ).fetchone()
    if updated is not None:
        # VT-382 (CL-437.3): batch terminal 'sent' close — owner_feedback redacts in
        # the SAME txn (the per-draft bodies were redacted at their own
        # sent/skipped/halted flips).
        from orchestrator.agents.outbox_redaction import redact_batch_owner_feedback

        redact_batch_owner_feedback(conn, tenant_id, [batch_id])
        # Completion is reported via the agent_work_items status (the roadmap
        # seam write — report_item_status 'done' — is the dispatch workflow's
        # responsibility, not the send choke point's).
        conn.execute(
            "UPDATE agent_work_items SET status = 'sent', updated_at = now() "
            "WHERE tenant_id = %s AND id = %s",
            (tenant_id, work_item_id),
        )
        logger.info(
            "agent_send: batch terminal tenant=%s batch=%s -> sent", tenant_id, batch_id
        )
        return "sent"
    row = conn.execute(
        "SELECT status FROM agent_draft_batches WHERE tenant_id = %s AND id = %s",
        (tenant_id, batch_id),
    ).fetchone()
    return str(_col(row, "status", 0))


def _skip(
    conn: Any,
    tenant_id: str,
    draft: dict[str, Any],
    reason: str,
    *,
    persist: bool = True,
) -> AgentSendResult:
    """Fail-closed skip: persist the marker on the draft (terminal) and sweep
    the batch toward terminal so an all-skip batch still completes."""
    logger.info(
        "agent_send: skip tenant=%s draft=%s batch=%s template=%s reason=%s",
        tenant_id, draft["draft_id"], draft["batch_id"], draft["template_name"], reason,
    )
    batch_status: str | None = draft["batch_status"]
    if persist:
        _mark_draft_skipped(conn, tenant_id, draft["draft_id"], reason)
        batch_status = _finalize_batch_if_terminal(
            conn, tenant_id, draft["batch_id"], draft["work_item_id"]
        )
    return AgentSendResult(
        draft_id=draft["draft_id"],
        status="skipped",
        skip_reason=reason,
        batch_status=batch_status,
    )


def agent_send_draft(
    tenant_id: UUID | str,
    draft_id: UUID | str,
    *,
    autonomy_level: str = "L2",
    conn: Any = None,
    send_fn: Any | None = None,
) -> AgentSendResult:
    """Send ONE agent draft through the full fail-closed gate stack (module docstring).

    ``conn`` — a tenant-scoped connection (RLS); opened via ``tenant_connection``
    when None. ``send_fn`` — injected Twilio transport for tests; None delegates
    to the live ``twilio_send.send_template_message`` via the VT-45 tool.

    Never raises for gate failures (each returns its marker); raises
    ``NotImplementedError`` for the PR-3 L3 branch and ``ValueError`` for an
    unknown autonomy level — both deliberate fail-LOUD paths.
    """
    if conn is None:
        with tenant_connection(tenant_id) as own_conn:
            return agent_send_draft(
                tenant_id,
                draft_id,
                autonomy_level=autonomy_level,
                conn=own_conn,
                send_fn=send_fn,
            )

    if autonomy_level not in ("L2", "L3"):
        raise ValueError(f"unknown autonomy_level {autonomy_level!r}; allowed: L2, L3")
    if autonomy_level == "L3":
        # PR-3 scope: auto_send_pending past send_not_before + the 1b/1c
        # structural autonomy re-verification + floor re-derivation. Stubbed
        # LOUD — never a silent partial L3 send (plan §3 gate 1).
        raise NotImplementedError(
            "L3 auto-send is PR-3 scope (VT-369) — agent_send_draft only "
            "implements the L2 approved-batch path in PR-1"
        )

    tid = str(tenant_id)
    did = str(draft_id)

    draft = _load_draft(conn, tid, did)
    if draft is None:
        logger.info("agent_send: draft not found tenant=%s draft=%s", tid, did)
        return AgentSendResult(draft_id=did, status="failed", skip_reason="draft_not_found")

    if draft["batch_status"] == "auto_send_pending":
        raise NotImplementedError(
            "auto_send_pending (L3 hold window) is PR-3 scope (VT-369)"
        )

    # In-module idempotency: a terminal draft never re-sends. The ledger check
    # inside the delegated send transaction (gate #6) is the authoritative
    # second layer.
    if draft["draft_status"] == "sent":
        return AgentSendResult(
            draft_id=did,
            status="already_sent",
            message_sid=draft["message_sid"],
            batch_status=draft["batch_status"],
        )
    if draft["draft_status"] in ("skipped", "halted"):
        return AgentSendResult(
            draft_id=did,
            status="skipped",
            skip_reason=draft["skip_reason"] or "already_terminal",
            batch_status=draft["batch_status"],
        )

    # --- Gate 1: batch state (L2 = Pillar-7 approved; 'sending' = mid-batch) ---
    if draft["batch_status"] not in ("approved", "sending"):
        # NOT persisted on the draft: an awaiting_approval batch may still be
        # legitimately approved later — poisoning the draft would be wrong.
        return _skip(conn, tid, draft, SKIP_BATCH_NOT_APPROVED, persist=False)

    # The batch is being processed: approved -> sending (idempotent CAS).
    conn.execute(
        "UPDATE agent_draft_batches SET status = 'sending', updated_at = now() "
        "WHERE tenant_id = %s AND id = %s AND status = 'approved'",
        (tid, draft["batch_id"]),
    )
    draft["batch_status"] = "sending"

    # --- Gate 2: template registry — SID + category + opt-out line ---
    try:
        entry = registry_resolve(draft["template_name"], _SEND_LANGUAGE)
    except TemplateRegistryError:
        return _skip(conn, tid, draft, SKIP_TEMPLATE_NOT_CONFIGURED)
    if entry.content_sid is None:
        # The documented pre-F1 stub (languages: en/hi: null) — fail-closed.
        return _skip(conn, tid, draft, SKIP_TEMPLATE_NOT_CONFIGURED)
    if entry.category != AGENT_SEND_CATEGORY:
        return _skip(conn, tid, draft, SKIP_WRONG_CATEGORY)
    if not entry.optout_line:
        return _skip(conn, tid, draft, SKIP_NO_OPTOUT_LINE)

    # --- Gate 3: customers row re-read AT SEND TIME (via the wrapper layer — the
    # no-direct-tenant-db-access lint owns per-tenant customers SQL) ---
    from orchestrator.db.wrappers import CustomersWrapper

    row = CustomersWrapper().send_eligibility(tid, draft["customer_id"], conn=conn)
    if row is None:
        return _skip(conn, tid, draft, SKIP_CUSTOMER_MISSING)  # gone == never sendable
    opt_out_status = row.get("opt_out_status")
    complaint_status = row.get("complaint_status")
    phone_e164 = row.get("phone_e164")
    if opt_out_status != "subscribed":
        return _skip(conn, tid, draft, SKIP_OPT_OUT)
    if complaint_status == "open":
        return _skip(conn, tid, draft, SKIP_COMPLAINT)
    if not phone_e164:
        return _skip(conn, tid, draft, SKIP_NO_PHONE)

    # --- Gate 4: version-aware marketing consent (C2 allowlist) ---
    if not has_marketing_consent_for_phone(tid, phone_e164, conn=conn):
        return _skip(conn, tid, draft, SKIP_CONSENT)

    # --- Gate 5: agent caps + suppression ---
    caps = check_agent_send_caps(tid, draft["customer_id"], conn=conn)
    if not caps.allowed:
        assert caps.reason is not None
        return _skip(conn, tid, draft, caps.reason)

    # --- Gate 6: delegate to the EXISTING VT-45 send path (not forked) ---
    # Idempotency key 'agent:{draft_id}' is checked against
    # send_idempotency_keys INSIDE send_whatsapp_template's send transaction;
    # VT-45 independently re-runs opt-out/opt-in + the 5000/24h tenant cap.
    from orchestrator.agent.tools.send_whatsapp_template import (
        SendWhatsappTemplateInput,
        send_whatsapp_template,
    )

    raw_params = draft["params"] or {}
    payload = SendWhatsappTemplateInput(
        tenant_id=tid,
        customer_id=draft["customer_id"],
        template_id=draft["template_name"],
        language=_SEND_LANGUAGE,
        template_params={k: str(v) for k, v in raw_params.items()},
        idempotency_key=f"agent:{did}",
    )
    out = send_whatsapp_template(payload, send_fn=send_fn)

    if out.status == "unauthorized":
        # VT-45's own consent/opt-out refusal (defense-in-depth; should have
        # been caught by gates 3/4). Map to the matching terminal marker.
        code = out.error_envelope.code if out.error_envelope else ""
        reason = SKIP_CONSENT if code == "recipient_not_opted_in" else SKIP_OPT_OUT
        return _skip(conn, tid, draft, reason)
    if out.status != "sent":
        # rate_limited / error / dry_run: transient or transport failure — the
        # draft stays 'drafted' (NOT terminal); honest failure to the caller.
        code = out.error_envelope.code if out.error_envelope else out.status
        logger.info(
            "agent_send: send failed tenant=%s draft=%s template=%s code=%s",
            tid, did, draft["template_name"], code,
        )
        return AgentSendResult(
            draft_id=did,
            status="failed",
            skip_reason=f"send_failed:{code}",
            batch_status=draft["batch_status"],
        )

    # --- Success: draft -> sent, contacts ledger row, batch/work-item sweep ---
    # VT-382 (CL-437.3): the status flip + the audit capture + the params redaction run
    # in ONE explicit transaction (the pool is autocommit — without this BEGIN/COMMIT
    # every statement commits alone): capture the EXACT owner-facing text into
    # owner_message_audit, THEN redact the outbox params — atomic both-or-neither, no
    # window where the outbox copy is gone but the audit row absent (or a flip without
    # its capture). Lazy from-import resolves the module attribute at call time (test seam).
    from orchestrator.agents.outbox_redaction import capture_then_redact_draft

    with conn.transaction():
        conn.execute(
            "UPDATE agent_drafts SET status = 'sent', message_sid = %s, skip_reason = NULL, "
            "updated_at = now() WHERE tenant_id = %s AND id = %s",
            (out.message_sid, tid, did),
        )
        capture_then_redact_draft(
            conn, draft, tenant_id=tid, message_sid=out.message_sid, language=_SEND_LANGUAGE
        )
    contact = conn.execute(
        "INSERT INTO agent_customer_contacts "
        "  (tenant_id, customer_id, agent, draft_id, batch_id, template_name, "
        "   autonomy_level, message_sid) "
        "SELECT %s, %s, %s, %s, %s, %s, %s, %s "
        "WHERE NOT EXISTS (SELECT 1 FROM agent_customer_contacts "
        "                  WHERE tenant_id = %s AND draft_id = %s) "
        "RETURNING id",
        (
            tid, draft["customer_id"], draft["agent"], did, draft["batch_id"],
            draft["template_name"], autonomy_level, out.message_sid,
            tid, did,
        ),
    ).fetchone()
    batch_status = _finalize_batch_if_terminal(
        conn, tid, draft["batch_id"], draft["work_item_id"]
    )
    status = "sent" if contact is not None else "already_sent"
    logger.info(
        "agent_send: %s tenant=%s draft=%s batch=%s template=%s sid=%s batch_status=%s",
        status, tid, did, draft["batch_id"], draft["template_name"],
        out.message_sid, batch_status,
    )
    return AgentSendResult(
        draft_id=did,
        status=status,
        message_sid=out.message_sid,
        batch_status=batch_status,
    )


__all__ = [
    "AGENT_SEND_CATEGORY",
    "AGENT_SEND_CUSTOMER_WEEKLY_CAP",
    "AGENT_SEND_DAILY_TENANT_CAP",
    "AgentSendResult",
    "CapCheckResult",
    "L3_DAILY_AUTO_SEND_CAP",
    "MAX_AGENT_CONTACTS_PER_90D",
    "RECONTACT_SUPPRESSION_DAYS",
    "agent_send_draft",
    "check_agent_send_caps",
    "has_marketing_consent_for_phone",
]
