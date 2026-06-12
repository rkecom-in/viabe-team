"""VT-369 Gap-5 PR-1 — the ONE deterministic customer-send choke point for specialist agents.

``agent_send_draft`` is the SOLE path by which an agent-drafted message reaches
a customer (plan §3; CRITICAL-2: the drafting agents are tool-guardrailed away
from every send capability — they emit ``template_name + params`` into
``agent_drafts`` and THIS module sends). Zero LLM (Pillar 1).

The gate stack runs IN ORDER; every gate fails CLOSED with a distinct marker:

  1. batch state — ``approved`` (or mid-batch ``sending``) under L2; for L3 the
     batch must be ``auto_send_pending`` (the delivery-anchored hold window). The
     L3 path takes a ``SELECT ... FOR UPDATE`` row lock on the batch and re-checks
     ``auto_send_pending`` UNDER the lock immediately before the irreversible send
     (gate 6), serialized against ``l3_hold.demote_auto_send_pending`` which takes
     the SAME lock — so a window-expiry send can never fire over an in-flight
     owner demote (the two-sided CAS, in either acquisition order).
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
     tenant cap underneath — defense-in-depth, not a fork. For L3 the send +
     the draft flip + the contact ledger row run inside the gate-1 FOR UPDATE
     transaction (the CAS), so the send is conditioned on the batch still being
     ``auto_send_pending`` under the lock.

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
SKIP_CAP_L3_DAILY = "skipped_cap_l3_daily"             # VT-384: 50/agent/24h L3 auto-send cap
SKIP_SIGNATURE_MISMATCH = "skipped_signature_mismatch" # VT-384: registry vs executor variable drift

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


# --- VT-384 signature cross-check (contract item 5) ---------------------------
#
# The executor drafts params under WINBACK_TEMPLATE_PARAMS; the registry pins the Meta-APPROVED
# variable signature. They MUST agree or the {{n}} positions are wrong (a customer would get
# garbage). registry-as-canon (Cowork ruling #1): the Meta body is immutable, the executor conforms.
# Two layers: (1) the import-time assert below pins the winback case at module load (a drifted
# executor constant fails the import — fail LOUD); (2) the per-send Gate-2b _registry_signature_ok
# catches ANY drift at send time (a mutated registry, a future template) — fail CLOSED.

# template_name -> the executor constant that drafts its params. Extend as Gap-5 grows; a template
# with no entry here has no executor signature to cross-check (the registry IS its only signature).
def _executor_signature_for(template_name: str) -> tuple[str, ...] | None:
    try:
        from orchestrator.agents import sales_recovery_executor
    except ImportError:
        return None
    if template_name == sales_recovery_executor.WINBACK_TEMPLATE_NAME:
        return tuple(sales_recovery_executor.WINBACK_TEMPLATE_PARAMS)
    return None


def _registry_signature_ok(template_name: str, entry: Any) -> bool:
    """True iff the registry's variable signature matches the executor constant for this template
    (order-independent — {{n}} positions are bound by the registry's ordered tuple, which the
    drafting prompt already conforms to; the cross-check guards the SET of names). A template with
    no executor signature has nothing to cross-check → True (the registry is its canon)."""
    expected = _executor_signature_for(template_name)
    if expected is None:
        return True
    return frozenset(entry.variables) == frozenset(expected)


def assert_winback_signature() -> None:
    """Import-time hard assert (contract item 5): the ARMED registry's team_winback_simple variable
    signature matches the executor WINBACK_TEMPLATE_PARAMS. A drift fails the IMPORT (fail LOUD) so
    a mismatched constant can never ship. Best-effort on a missing registry/executor (the dep-less
    smoke imports this module without the yaml on the path)."""
    try:
        from orchestrator.agents import sales_recovery_executor
        from orchestrator.templates_registry import resolve as registry_resolve

        name = sales_recovery_executor.WINBACK_TEMPLATE_NAME
        entry = registry_resolve(name, _SEND_LANGUAGE)
        expected = frozenset(sales_recovery_executor.WINBACK_TEMPLATE_PARAMS)
        got = frozenset(entry.variables)
        if got != expected:
            raise AssertionError(
                f"VT-384 signature drift: registry '{name}' variables {sorted(got)} != "
                f"executor WINBACK_TEMPLATE_PARAMS {sorted(expected)} (registry-as-canon: "
                "conform the executor constant + its draft prompt to the Meta-APPROVED body)"
            )
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001 — a missing registry/executor (dep-less smoke) is not a drift
        logger.debug("assert_winback_signature: registry/executor unavailable — skipped")


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
    tenant_id: UUID | str,
    customer_id: UUID | str,
    *,
    conn: Any,
    autonomy_level: str = "L2",
    agent: str | None = None,
) -> CapCheckResult:
    """The agent-cap gate (plan §3e), counted over ``agent_customer_contacts``.

    Checked in order — L3 daily auto-send (50/agent/24h, L3 only), tenant daily
    (200/24h), customer weekly (1/7d), 30d recontact suppression, 2-per-90d
    ceiling. Each failure returns its own marker. VT-45's 5000/24h tenant cap
    still applies underneath in the delegated send path.

    VT-384: ``L3_DAILY_AUTO_SEND_CAP`` (defined above, commented "ENFORCED in
    PR-3") is now enforced HERE — a per-agent 24h count of L3 auto-sends, gated
    ONLY when ``autonomy_level == 'L3'`` (L2 approved sends are owner-gated, not
    rate-limited by this cap). Requires ``agent`` to scope the count.

    Known residual (accepted, plan §3e): COUNT-then-send TOCTOU overshoot is
    bounded by concurrency width (~2 messages) — not redesigned here.
    """
    tid, cid = str(tenant_id), str(customer_id)

    # --- VT-384 L3 daily auto-send cap (50/agent/24h) — L3 path only ---
    if autonomy_level == "L3" and agent is not None:
        row = conn.execute(
            "SELECT count(*) AS c FROM agent_customer_contacts "
            "WHERE tenant_id = %s AND agent = %s AND autonomy_level = 'L3' "
            "  AND sent_at > now() - interval '24 hours'",
            (tid, agent),
        ).fetchone()
        if int(_col(row, "c", 0)) >= L3_DAILY_AUTO_SEND_CAP:
            return CapCheckResult(allowed=False, reason=SKIP_CAP_L3_DAILY)

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
        # VT-384: 'auto_send_pending' joins the closeable set — an L3 batch stays anchored in
        # auto_send_pending while its drafts send (it never flips to 'sending'), so it closes to
        # 'sent' directly from auto_send_pending once every draft is terminal.
        "UPDATE agent_draft_batches SET status = 'sent', updated_at = now() "
        "WHERE tenant_id = %s AND id = %s AND status IN ('approved', 'sending', 'auto_send_pending') "
        "RETURNING id",
        (tenant_id, batch_id),
    ).fetchone()
    if updated is not None:
        # VT-382 (CL-437.3): batch terminal 'sent' close — owner_feedback redacts on
        # the SAME connection (the per-draft bodies were redacted at their own
        # sent/skipped/halted flips). Txn accuracy (gate F3): this close runs OUTSIDE
        # agent_send_draft's explicit transaction (which wraps only the draft flip +
        # capture) on the autocommit pool — the batch flip and this redaction each
        # commit alone; a crash between them is healed by the daily outbox_redaction
        # sweep (the crash backstop).
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
    # VT-384 (PR-3 stub arm 1, was customer_send.py:377): the L3 auto-send path. The hold-wake
    # leg (agents/l3_hold.l3_hold_workflow) calls this with autonomy_level='L3' on a batch that is
    # 'auto_send_pending'. The wire (eligibility re-derivation + delivery-anchored hold + demote
    # CAS) lives in l3_hold.py; THIS function stays the deterministic per-draft gate stack. The L3
    # arm runs the SAME gates as L2 plus the L3_DAILY cap (gate 5b) and the signature cross-check
    # (gate 2b); the only structural difference is the batch-state gate accepts 'auto_send_pending'
    # for L3 (gate 1). C2 (MARKETING_CONSENT_VERSIONS empty) makes gate 4 fail-closed: ZERO L3
    # sends end-to-end even on a fully-armed batch — the wire is proven against the stop.

    tid = str(tenant_id)
    did = str(draft_id)

    draft = _load_draft(conn, tid, did)
    if draft is None:
        logger.info("agent_send: draft not found tenant=%s draft=%s", tid, did)
        return AgentSendResult(draft_id=did, status="failed", skip_reason="draft_not_found")

    # VT-384 (PR-3 stub arm 2, was customer_send.py:391): an 'auto_send_pending' batch is the L3
    # hold window. It is sendable ONLY through the L3 arm (the hold-wake leg). A stray L2 call on
    # such a batch still fails closed (gate 1 below rejects it — auto_send_pending is not in the L2
    # ('approved','sending') set), so this is the explicit fail-LOUD guard for an L2 caller racing
    # the hold: never a silent send over an in-flight hold.
    if draft["batch_status"] == "auto_send_pending" and autonomy_level != "L3":
        return _skip(conn, tid, draft, SKIP_BATCH_NOT_APPROVED, persist=False)

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

    # --- Gate 1: batch state (L2 = Pillar-7 approved; 'sending' = mid-batch;
    # VT-384 L3 = 'auto_send_pending', the delivery-anchored hold window) ---
    _ok_batch_states = ("auto_send_pending",) if autonomy_level == "L3" else ("approved", "sending")
    if draft["batch_status"] not in _ok_batch_states:
        # NOT persisted on the draft: an awaiting_approval batch may still be
        # legitimately approved later — poisoning the draft would be wrong.
        # VT-384: a demoted (auto_send_pending → awaiting_approval) batch reaching an L3 call
        # lands here too — the two-sided race guard. An expiry send can NEVER fire over an
        # in-flight objection: the owner-inbound demote flipped the batch out of
        # auto_send_pending, so this gate skips it.
        return _skip(conn, tid, draft, SKIP_BATCH_NOT_APPROVED, persist=False)

    if autonomy_level == "L3":
        # WAKE-side cheap pre-check (the fast skip): the batch must still be auto_send_pending.
        # This is NOT the race guard — it is an unlocked early-out so an already-demoted batch skips
        # without running gates 2-5. The AUTHORITATIVE serialization is the FOR UPDATE row lock taken
        # in the irreversible region below (gate 6): the still-pending re-check + the Twilio send +
        # the draft flip all run inside ONE transaction holding that lock, and demote_auto_send_pending
        # takes the SAME FOR UPDATE lock — so whichever side acquires the row first runs to commit
        # while the other blocks, then re-reads status. A window-expiry send can NEVER fire over an
        # in-flight demote (and a demote can never land between this send's status-check and its
        # flip). Unlike L2, the L3 batch does NOT flip to 'sending' — it stays anchored in
        # auto_send_pending so a late owner-inbound demote still wins; the batch closes to 'sent'
        # only when every draft is terminal (_finalize_batch_if_terminal).
        still = conn.execute(
            "SELECT 1 FROM agent_draft_batches WHERE tenant_id = %s AND id = %s "
            "AND status = 'auto_send_pending'",
            (tid, draft["batch_id"]),
        ).fetchone()
        if still is None:
            return _skip(conn, tid, draft, SKIP_BATCH_NOT_APPROVED, persist=False)
    else:
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

    # --- Gate 2b: signature cross-check (VT-384 contract item 5) — the registry's approved
    # variable signature for this template MUST match the executor constant that drafted its
    # params. The import-time assert (assert_winback_signature, below) locks the winback case at
    # module load; this is the per-send Gate-2 hard-refuse for ANY registry/executor drift (a
    # mutated registry, a future template). Fail-closed: a mismatch SKIPS, never sends.
    if not _registry_signature_ok(draft["template_name"], entry):
        return _skip(conn, tid, draft, SKIP_SIGNATURE_MISMATCH)

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

    # --- Gate 5: agent caps + suppression (VT-384: L3 path passes agent + level so the
    # 50/agent/24h L3 auto-send cap is enforced; L2 keeps the existing cap set) ---
    caps = check_agent_send_caps(
        tid, draft["customer_id"], conn=conn,
        autonomy_level=autonomy_level, agent=draft["agent"],
    )
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
    from orchestrator.agents.outbox_redaction import capture_then_redact_draft

    raw_params = draft["params"] or {}
    payload = SendWhatsappTemplateInput(
        tenant_id=tid,
        customer_id=draft["customer_id"],
        template_id=draft["template_name"],
        language=_SEND_LANGUAGE,
        template_params={k: str(v) for k, v in raw_params.items()},
        idempotency_key=f"agent:{did}",
    )

    # VT-384 — the wake-side CAS. For L3 the irreversible send + the draft flip run inside ONE
    # transaction that FIRST takes a FOR UPDATE row lock on the batch and re-confirms it is STILL
    # auto_send_pending UNDER the lock. demote_auto_send_pending takes the SAME FOR UPDATE lock, so
    # the two serialize: if the demote committed first, this SELECT ... FOR UPDATE blocks until it
    # releases, then sees status != 'auto_send_pending' and ABORTS before the send (no send over an
    # objection); if this side acquires first, the demote blocks until the send+flip commit. The
    # Twilio call is held inside the lock deliberately — on this low-volume single-draft L3 path the
    # no-send-over-objection invariant outranks lock duration. L2 keeps its original (unlocked)
    # path: an L2 batch is owner-approved and never demoted out from under a send.
    if autonomy_level == "L3":
        with conn.transaction():
            locked = conn.execute(
                "SELECT status FROM agent_draft_batches WHERE tenant_id = %s AND id = %s "
                "FOR UPDATE",
                (tid, draft["batch_id"]),
            ).fetchone()
            if locked is None or str(_col(locked, "status", 0)) != "auto_send_pending":
                # A concurrent demote (or cancel) won the row — abort BEFORE the irreversible send.
                # NOT persisted on the draft (the batch may be re-approved later via the L2 path).
                return _skip(conn, tid, draft, SKIP_BATCH_NOT_APPROVED, persist=False)
            out = send_whatsapp_template(payload, send_fn=send_fn)
            if out.status == "unauthorized":
                code = out.error_envelope.code if out.error_envelope else ""
                reason = SKIP_CONSENT if code == "recipient_not_opted_in" else SKIP_OPT_OUT
                # _skip opens its own nested transaction (savepoint) — safe inside this txn.
                return _skip(conn, tid, draft, reason)
            if out.status != "sent":
                code = out.error_envelope.code if out.error_envelope else out.status
                logger.info(
                    "agent_send: send failed tenant=%s draft=%s template=%s code=%s",
                    tid, did, draft["template_name"], code,
                )
                # The draft stays 'drafted' (NOT terminal); the txn commits with no flip.
                return AgentSendResult(
                    draft_id=did, status="failed",
                    skip_reason=f"send_failed:{code}", batch_status=draft["batch_status"],
                )
            # Send succeeded — flip + capture + contact + sweep, all under the still-held lock.
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
            draft_id=did, status=status, message_sid=out.message_sid, batch_status=batch_status,
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
    # its capture).
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


# VT-384 import-time signature pin (contract item 5): the ARMED registry's team_winback_simple
# signature MUST match the executor WINBACK_TEMPLATE_PARAMS — a drift fails THIS import (fail
# LOUD). Best-effort on a missing registry/executor (dep-less smoke), so the module stays importable.
assert_winback_signature()


__all__ = [
    "AGENT_SEND_CATEGORY",
    "AGENT_SEND_CUSTOMER_WEEKLY_CAP",
    "AGENT_SEND_DAILY_TENANT_CAP",
    "AgentSendResult",
    "CapCheckResult",
    "L3_DAILY_AUTO_SEND_CAP",
    "MAX_AGENT_CONTACTS_PER_90D",
    "RECONTACT_SUPPRESSION_DAYS",
    "SKIP_CAP_L3_DAILY",
    "SKIP_SIGNATURE_MISMATCH",
    "agent_send_draft",
    "assert_winback_signature",
    "check_agent_send_caps",
    "has_marketing_consent_for_phone",
]
