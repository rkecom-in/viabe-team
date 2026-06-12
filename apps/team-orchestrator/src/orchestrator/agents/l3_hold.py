"""VT-384 Gap-5 PR-3 — the L3 auto-send HOLD workflow + delivery anchor + demote CAS.

This module is the wire core. ``customer_send.agent_send_draft`` keeps its diff to the two
sanctioned stub arms; ALL the hold/CAS/anchor logic lives HERE so the send choke point stays
the deterministic per-draft gate stack it already is.

The L3 flow (plan-ack §1, Cowork ruling C-a/C-b/C-c/C-d):

  enter_l3_hold(...)            — the L3 ARM. Eligibility (autonomy L3 + not frozen +
                                  is_always_confirm FALSE, re-derived per batch) → batch →
                                  ``auto_send_pending`` + ``team_l3_presend_notice`` send to the
                                  OWNER → record the notice SID. Starts l3_hold_workflow.
  stamp_delivery_anchor(...)    — the runner status-callback leg calls this when the notice's
                                  ``delivered`` callback lands: CAS-stamp presend_notice_delivered_at
                                  and derive send_not_before = delivered_at + hold_hours (config).
                                  A late callback after a demote is a NO-OP (C-d) — the CAS only
                                  fires while the batch is still ``auto_send_pending``.
  demote_auto_send_pending(...) — the demote CAS (owner-inbound leg in runner + the no-delivery leg
                                  in the workflow): ``auto_send_pending`` → ``awaiting_approval`` +
                                  regression record. C-c collision rule: if an open approval already
                                  exists for the tenant, the demote QUEUES the batch (flips it to
                                  awaiting WITHOUT arming a second approval — mig-128 one-open-per-
                                  tenant is the backstop; the arm happens when the open one resolves).
  l3_hold_workflow(...)         — the DBOS workflow: parks on the run-control poll idiom until
                                  send_not_before passes (delivery-anchored) OR the no-delivery
                                  window elapses with no anchor ⇒ demote. On wake: a CAS re-check
                                  (the batch must STILL be auto_send_pending) ⇒ per-draft
                                  agent_send_draft(autonomy_level='L3') with EVERY gate re-evaluated.

Durations are CONFIG-DRIVEN (config/l3_autonomy.yaml, the trial.yaml pattern — Cowork ruling C-a /
VT-381 TTL lesson). The loader is cached and FAILS CLOSED to the safe defaults on any read error:
a config outage must never widen the hold or skip the demote.

Explicit-transaction discipline (VT-382 / CL-437.3): the orchestrator pool is AUTOCOMMIT — every
multi-statement CAS unit here (the demote flip + regression, the anchor stamp + window derive)
wraps its statements in an explicit ``conn.transaction()`` so a crash can never half-apply it.

CL-390: logs carry tenant/batch/approval ids + counters + statuses only — never owner_feedback,
never draft params, never a phone.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from orchestrator.db import tenant_connection

logger = logging.getLogger(__name__)

AGENT_NAME = "sales_recovery"  # the only Gap-5 agent that reaches L3 in PR-3

# The owner-notification template the L3 arm sends (registry name; owner-targeted, SID-resolved).
PRESEND_NOTICE_TEMPLATE = "team_l3_presend_notice"

# ---------------------------------------------------------------------------
# Config — the trial.yaml pattern (Cowork ruling C-a): durations are DATA, cached, fail-closed.
# ---------------------------------------------------------------------------

_CONFIG = Path(__file__).resolve().parents[3] / "config" / "l3_autonomy.yaml"

# Fail-closed SAFE defaults — used when the yaml is missing/malformed OR a key is absent. These
# are the SAME values the yaml ships with; the duplication is deliberate (a config outage must not
# widen the hold window or skip the demote). NEVER widen these from a test.
_DEFAULT_HOLD_HOURS = 2.0
_DEFAULT_NO_DELIVERY_DEMOTE_MINUTES = 30.0

# Module-level TTL cache: (loaded_at_monotonic, parsed) — picks up a yaml edit without a restart.
_CONFIG_TTL_SECONDS = 60.0
_config_cache: tuple[float, dict[str, Any]] | None = None


def _load_config() -> dict[str, Any]:
    """Cached, fail-closed config load. Any read/parse error returns ``{}`` (the callers then fall
    back to the safe module defaults) — a config outage never alters the hold's safety posture."""
    global _config_cache
    now = time.monotonic()
    if _config_cache is not None and (now - _config_cache[0]) < _CONFIG_TTL_SECONDS:
        return _config_cache[1]
    try:
        import yaml

        data = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
        parsed = data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — fail-closed to defaults on ANY read/parse error
        logger.warning("l3_hold: config load failed; falling back to safe defaults", exc_info=True)
        parsed = {}
    _config_cache = (now, parsed)
    return parsed


def hold_hours() -> float:
    """The delivery-anchored hold length in hours (config, fail-closed to the safe default)."""
    cfg = _load_config()
    try:
        return float(cfg.get("hold_hours", _DEFAULT_HOLD_HOURS))
    except (TypeError, ValueError):
        return _DEFAULT_HOLD_HOURS


def no_delivery_demote_minutes() -> float:
    """The no-delivery demote window in minutes (config, fail-closed to the safe default)."""
    cfg = _load_config()
    try:
        return float(cfg.get("no_delivery_demote_minutes", _DEFAULT_NO_DELIVERY_DEMOTE_MINUTES))
    except (TypeError, ValueError):
        return _DEFAULT_NO_DELIVERY_DEMOTE_MINUTES


def _invalidate_config_cache() -> None:
    """Test helper — force the next _load_config() to re-read from disk."""
    global _config_cache
    _config_cache = None


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class L3ArmResult:
    """Outcome of ``enter_l3_hold``. ``armed`` False carries a ``reason`` marker."""

    armed: bool
    batch_id: str
    reason: str | None = None
    presend_notice_sid: str | None = None


@dataclass(frozen=True, slots=True)
class DemoteResult:
    """Outcome of ``demote_auto_send_pending``.

    ``demoted`` — the batch was flipped auto_send_pending → awaiting_approval.
    ``queued``  — demoted but NOT re-armed because an open approval already exists (C-c): the
                  batch sits awaiting; the arm happens when the open approval resolves.
    ``noop``    — the batch was no longer auto_send_pending (already demoted/sent/cancelled);
                  this is the C-d late-callback / double-demote acceptance leg.
    """

    batch_id: str
    demoted: bool
    queued: bool = False
    noop: bool = False


def _col(row: Any, key: str, idx: int) -> Any:
    return row[key] if isinstance(row, dict) else row[idx]


# ---------------------------------------------------------------------------
# The L3 ARM — enter the hold (plan-ack §1)
# ---------------------------------------------------------------------------


def enter_l3_hold(
    tenant_id: UUID | str,
    batch_id: UUID | str,
    *,
    conn: Any,
    send_fn: Any | None = None,
) -> L3ArmResult:
    """Move an L3-eligible drafted batch into the delivery-anchored hold (plan-ack §1).

    Re-derives eligibility AT ARM TIME (never trusts the stored level): autonomy L3 + not frozen,
    and ``is_always_confirm`` FALSE for the batch's customer set + template (the money/bulk/first-
    contact/novel floor — CL-438 non-bypassable). ANY floor trip ⇒ the batch is NOT armed for L3;
    it falls back to the L2 approval path (the caller arms it). A money-bearing template can
    therefore NEVER reach ``auto_send_pending`` — the C2/floor proof.

    On success: batch → ``auto_send_pending``, the owner gets ``team_l3_presend_notice`` (SID
    recorded on the batch), and the caller starts ``l3_hold_workflow``. NO ``send_not_before`` is
    set here — it is delivery-anchored (stamped by the callback leg). ``conn`` is the caller's
    tenant_connection (RLS). ``send_fn`` injects the Twilio transport for tests.
    """
    from orchestrator.agents.autonomy import get_autonomy, is_always_confirm

    tid, bid = str(tenant_id), str(batch_id)

    # Re-read the batch + its agent (never trust caller memory for the agent / status).
    batch = conn.execute(
        "SELECT agent, status FROM agent_draft_batches WHERE tenant_id = %s AND id = %s",
        (tid, bid),
    ).fetchone()
    if batch is None:
        return L3ArmResult(armed=False, batch_id=bid, reason="batch_not_found")
    agent = str(_col(batch, "agent", 0))
    status = str(_col(batch, "status", 1))
    if status not in ("drafting", "awaiting_approval"):
        # Not in an armable-for-L3 state (already approved/sending/terminal) — fail-closed no-op.
        return L3ArmResult(armed=False, batch_id=bid, reason="batch_not_armable")

    state = get_autonomy(tid, agent, conn=conn)
    if state.level != "L3" or state.frozen:
        return L3ArmResult(armed=False, batch_id=bid, reason="not_l3")

    # The money/bulk/first-contact/novel floor — re-derived per batch (autonomy.is_always_confirm).
    drafts = conn.execute(
        "SELECT customer_id::text AS cid, template_name FROM agent_drafts "
        "WHERE tenant_id = %s AND batch_id = %s AND status = 'drafted'",
        (tid, bid),
    ).fetchall()
    if not drafts:
        return L3ArmResult(armed=False, batch_id=bid, reason="empty_batch")
    customer_ids = [str(_col(r, "cid", 0)) for r in drafts]
    template_name = str(_col(drafts[0], "template_name", 1))
    entry_money = _template_is_money_bearing(template_name)
    floor, floor_reason = is_always_confirm(
        tid,
        agent=agent,
        batch_customer_ids=customer_ids,
        template_name=template_name,
        money_bearing=entry_money,
        conn=conn,
    )
    if floor:
        logger.info(
            "l3_hold: always-confirm floor tripped tenant=%s batch=%s reason=%s — L2 fallback",
            tid, bid, floor_reason,
        )
        return L3ArmResult(armed=False, batch_id=bid, reason=f"always_confirm_{floor_reason}")

    # Send the owner the presend notice FIRST (so a send failure aborts the arm — no silent
    # auto_send_pending without a notice). Owner-targeted, like the approval send.
    notice_sid = _send_presend_notice(tid, len(customer_ids), send_fn=send_fn)
    if notice_sid is None:
        return L3ArmResult(armed=False, batch_id=bid, reason="presend_notice_failed")

    # Flip → auto_send_pending + record the notice SID, atomically (explicit txn on the autocommit
    # pool — VT-382 lesson). CAS on status so a concurrent demote/cancel wins cleanly.
    with conn.transaction():
        flipped = conn.execute(
            "UPDATE agent_draft_batches SET status = 'auto_send_pending', "
            "presend_notice_sid = %s, auto_send_pending_at = now(), updated_at = now() "
            "WHERE tenant_id = %s AND id = %s AND status = %s RETURNING id",
            (notice_sid, tid, bid, status),
        ).fetchone()
    if flipped is None:
        return L3ArmResult(armed=False, batch_id=bid, reason="cas_lost")
    logger.info(
        "l3_hold: armed auto_send_pending tenant=%s batch=%s drafts=%d notice_sid=%s",
        tid, bid, len(customer_ids), notice_sid,
    )
    return L3ArmResult(armed=True, batch_id=bid, presend_notice_sid=notice_sid)


def _template_is_money_bearing(template_name: str) -> bool:
    """Resolve money_bearing off the registry (fail-closed: an unresolved template is treated as
    money-bearing so it trips the always-confirm floor rather than silently auto-sending)."""
    from orchestrator.templates_registry import TemplateRegistryError
    from orchestrator.templates_registry import resolve as registry_resolve

    try:
        return bool(registry_resolve(template_name, "en").money_bearing)
    except TemplateRegistryError:
        return True


def _send_presend_notice(tenant_id: str, send_count: int, *, send_fn: Any | None) -> str | None:
    """Send ``team_l3_presend_notice`` to the OWNER and return its Twilio SID (None on failure).

    Owner-targeted (no customer_id) via send_template_message — the same primitive
    request_owner_approval uses (owner phone resolves from tenants.owner_phone). The notice carries
    owner_name + send_count ONLY (registry signature). ``send_fn`` injects the transport for tests.
    """
    from orchestrator.agents.approval_glue import _owner_display_name

    with tenant_connection(tenant_id) as c:
        owner_name = _owner_display_name(c, tenant_id)
    params = {"owner_name": owner_name, "send_count": str(send_count)}
    try:
        sender = send_fn or _default_notice_sender
        result = sender(tenant_id, params)
    except Exception:  # noqa: BLE001 — a notice send failure aborts the arm (fail-closed)
        logger.warning(
            "l3_hold: presend notice send raised tenant=%s — arm aborted", tenant_id, exc_info=True
        )
        return None
    sid = getattr(result, "message_sid", None) or (result.get("message_sid") if isinstance(result, dict) else None)
    if not getattr(result, "success", None) and isinstance(result, dict):
        if not result.get("success"):
            return None
    if not sid:
        return None
    return str(sid)


def _default_notice_sender(tenant_id: str, params: dict[str, str]) -> Any:
    """Live owner-targeted notice send (lazy import — heavy twilio chain)."""
    from orchestrator.utils.twilio_send import send_template_message

    return send_template_message(UUID(tenant_id), PRESEND_NOTICE_TEMPLATE, params)


# ---------------------------------------------------------------------------
# Delivery anchor (plan-ack §1; C-d late-callback no-op)
# ---------------------------------------------------------------------------


def stamp_delivery_anchor(
    tenant_id: UUID | str, message_sid: str, *, conn: Any
) -> str | None:
    """The runner status-callback leg: stamp the F6 delivery anchor for a presend notice ``delivered``
    callback and derive ``send_not_before = delivered_at + hold_hours`` (config).

    Matches the SID to an ``auto_send_pending`` batch carrying that ``presend_notice_sid``. The CAS
    fires ONLY while the batch is still auto_send_pending AND the anchor is unset — so a late
    callback that arrives AFTER a demote is a NO-OP (C-d), and a redelivered callback is idempotent.
    Returns the batch_id stamped, or None when nothing matched. Explicit txn (autocommit pool).
    """
    tid = str(tenant_id)
    hh = hold_hours()
    with conn.transaction():
        row = conn.execute(
            "UPDATE agent_draft_batches "
            "SET presend_notice_delivered_at = now(), "
            "    send_not_before = now() + (%s * interval '1 hour'), "
            "    updated_at = now() "
            "WHERE tenant_id = %s AND presend_notice_sid = %s "
            "  AND status = 'auto_send_pending' "
            "  AND presend_notice_delivered_at IS NULL "
            "RETURNING id::text AS bid",
            (hh, tid, message_sid),
        ).fetchone()
    if row is None:
        return None
    bid = str(_col(row, "bid", 0))
    logger.info(
        "l3_hold: delivery anchor stamped tenant=%s batch=%s sid=%s hold_hours=%s",
        tid, bid, message_sid, hh,
    )
    return bid


# ---------------------------------------------------------------------------
# Demote CAS (plan-ack §2; Cowork ruling C-c collision rule)
# ---------------------------------------------------------------------------


def demote_auto_send_pending(
    tenant_id: UUID | str,
    *,
    conn: Any,
    agent: str = AGENT_NAME,
    reason: str = "owner_engaged",
    batch_id: UUID | str | None = None,
) -> list[DemoteResult]:
    """Demote auto_send_pending batch(es) → awaiting_approval (the L2 re-entry), atomically with a
    regression record (same-txn discipline). The two-sided race guard is a REAL row lock: each batch
    is taken with ``SELECT ... FOR UPDATE`` (the SAME lock the wake-side send takes) inside the flip
    transaction, so the demote and the irreversible send SERIALIZE on the row — an expiry send can
    never fire over an in-flight objection, in EITHER acquisition order.

    Scope: a specific ``batch_id`` (the no-delivery / hold-wake leg) OR every auto_send_pending batch
    for (tenant, agent) (the owner-inbound leg — runner doesn't know the batch id). Owner-engagement
    demote means "I want eyes on this," NOT "kill it" (Cowork ruling #2) — the batch re-enters the
    normal approval path; nothing is lost.

    C-c collision rule: the demote target flips the batch to ``awaiting_approval`` but does NOT arm a
    fresh approval when one is already open for the tenant (mig-128 one-open-per-tenant is the
    structural backstop). Such a batch is QUEUED — it sits awaiting; the arm happens when the open
    approval resolves. NEVER two open. A batch already out of auto_send_pending is a NO-OP (C-d).

    Returns one DemoteResult per batch considered. Explicit txn wraps the flip + regression so a
    crash can never demote without recording the regression (or vice-versa).
    """
    tid = str(tenant_id)

    # Is there already an open approval for this tenant? (mig-128: at most one ever.) Read it under
    # the same conn so the collision decision is consistent with the flip below.
    from orchestrator.db.wrappers import PendingApprovalsWrapper

    an_approval_is_open = PendingApprovalsWrapper().has_open_for_tenant(tid, conn=conn)

    if batch_id is not None:
        target_ids = [str(batch_id)]
    else:
        rows = conn.execute(
            "SELECT id::text AS bid FROM agent_draft_batches "
            "WHERE tenant_id = %s AND agent = %s AND status = 'auto_send_pending'",
            (tid, agent),
        ).fetchall()
        target_ids = [str(_col(r, "bid", 0)) for r in rows]

    results: list[DemoteResult] = []
    if not target_ids:
        return results

    from orchestrator.agents.autonomy import record_regression_event

    for bid in target_ids:
        with conn.transaction():
            # VT-384 — the demote half of the two-sided CAS. Take the SAME FOR UPDATE batch-row lock
            # the wake-side send (customer_send.agent_send_draft L3 path) takes, BEFORE the flip and
            # inside this transaction: the two serialize on the row. If the send is mid-flight it
            # holds the lock until it commits — this UPDATE then sees status != 'auto_send_pending'
            # (it became 'sent' / mid-send) and no-ops; if we acquire first, the send blocks until
            # this demote commits, then its locked re-check finds 'awaiting_approval' and aborts
            # before the irreversible Twilio call. Either order ⇒ never a send over the demote.
            locked = conn.execute(
                "SELECT status FROM agent_draft_batches WHERE tenant_id = %s AND id = %s "
                "FOR UPDATE",
                (tid, bid),
            ).fetchone()
            if locked is None or str(_col(locked, "status", 0)) != "auto_send_pending":
                # Already out of auto_send_pending (demoted / sent / sending / cancelled) — C-d no-op.
                results.append(DemoteResult(batch_id=bid, demoted=False, noop=True))
                continue
            flipped = conn.execute(
                "UPDATE agent_draft_batches SET status = 'awaiting_approval', "
                "send_not_before = NULL, updated_at = now() "
                "WHERE tenant_id = %s AND id = %s AND status = 'auto_send_pending' "
                "RETURNING id",
                (tid, bid),
            ).fetchone()
            if flipped is None:
                # Lost the CAS between the lock read and the UPDATE (defensive; the lock makes this
                # unreachable) — C-d no-op.
                results.append(DemoteResult(batch_id=bid, demoted=False, noop=True))
                continue
            # Regression record (streak → 0) — kind 'owner_disengaged'. CRITICAL: this is the ONLY
            # regression kind that resets the streak WITHOUT revoking L3 + WITHOUT cancelling open
            # batches (autonomy.record_regression_event: `revokes = level=='L3' and kind !=
            # 'owner_disengaged'`; the freezing kinds also cancel). The demote means "owner wants
            # eyes on this" (Cowork ruling #2), NOT "kill it" — the batch must SURVIVE as
            # awaiting_approval, so any revoke/freeze kind would wrongly cancel the very batch we
            # just demoted. Same txn as the flip (the regression is atomic with the demote).
            # VT-384 gate-bounce F5: thread the PRECISE reason ('owner_engaged' vs 'no_delivery')
            # into the regression record's detail — the kind is the same 'owner_disengaged' for
            # both demote causes, so detail is what separates an owner-engagement demote from a
            # no-delivery demote downstream (rides the agent_autonomy_regressed event).
            record_regression_event(tid, agent, "owner_disengaged", conn=conn, detail=reason)
        # The arm is OUTSIDE the flip txn: when no approval is open, arm one now; when one is open,
        # the batch is QUEUED (never a second open — C-c). The arm itself owns mig-128 serialization.
        if an_approval_is_open:
            logger.info(
                "l3_hold: demote QUEUED (open approval exists) tenant=%s batch=%s reason=%s",
                tid, bid, reason,
            )
            results.append(DemoteResult(batch_id=bid, demoted=True, queued=True))
        else:
            armed = _arm_demoted_batch(tid, bid, conn=conn)
            results.append(DemoteResult(batch_id=bid, demoted=True, queued=not armed))
            # After the first arm, any further batch in this loop must QUEUE (one-open-per-tenant).
            if armed:
                an_approval_is_open = True
    return results


def _resolve_batch_run_id(tenant_id: str, batch_id: str, *, conn: Any) -> str | None:
    """Resolve the REAL pipeline_runs id the approval must FK to, from the batch's work_item_id.

    The dispatch workflow (coordinator._open_agent_run) opened a pipeline_runs row under the
    DETERMINISTIC ``_agent_run_id(work_item_id)`` (uuid5 of the work item) — that row EXISTS. The
    L2 approval the demote arms must reference THAT run (pending_approvals.run_id FKs pipeline_runs):
    a fresh uuid4 would FK-violate and the arm would never succeed (the stranding gap). We re-read
    the batch's work_item_id under the caller's RLS conn and mirror the coordinator's uuid5 derive.

    Returns None when the batch row is gone OR its work_item_id has no pipeline_runs row (a corner —
    the caller then keeps the queue path; it never raises). The run-row existence check keeps the arm
    from attempting an FK-violating insert when the dispatch run somehow never opened."""
    from orchestrator.agents.coordinator import _agent_run_id

    row = conn.execute(
        "SELECT work_item_id::text AS wid FROM agent_draft_batches "
        "WHERE tenant_id = %s AND id = %s",
        (tenant_id, batch_id),
    ).fetchone()
    if row is None:
        return None
    work_item_id = str(_col(row, "wid", 0))
    run_id = _agent_run_id(work_item_id)
    # Confirm the dispatch run row actually exists before we hand it to the FK-bound arm. A batch
    # whose run row is somehow absent → return None so the caller keeps the caught-refusal queue path.
    run_row = conn.execute(
        "SELECT 1 FROM pipeline_runs WHERE tenant_id = %s AND id = %s",
        (tenant_id, run_id),
    ).fetchone()
    if run_row is None:
        return None
    return run_id


def _arm_demoted_batch(tenant_id: str, batch_id: str, *, conn: Any) -> bool:
    """Arm the L2 approval for a just-demoted batch. Returns True if armed, False if the arm
    refused (e.g. the one-open-per-tenant mutex raced us — the batch stays queued awaiting). Never
    raises into the caller: a refusal QUEUES the batch (it is already awaiting_approval).

    The run id is the REAL dispatch run (resolved from the batch's work_item_id via the coordinator's
    deterministic uuid5), NOT a fresh uuid4 — pending_approvals.run_id FKs pipeline_runs, so a random
    id would FK-violate and the arm would never succeed (the stranding gap this fixes). A batch whose
    run row is somehow absent (the corner) keeps the queue path: the resolver returns None and we
    QUEUE rather than raise."""
    from orchestrator.agents.approval_glue import ApprovalArmRefused, arm_agent_send_approval

    run_id = _resolve_batch_run_id(tenant_id, batch_id, conn=conn)
    if run_id is None:
        logger.warning(
            "l3_hold: demote arm has no dispatch run row (queued) tenant=%s batch=%s",
            tenant_id, batch_id,
        )
        return False
    try:
        arm_agent_send_approval(tenant_id, run_id, batch_id)
        return True
    except ApprovalArmRefused as exc:
        logger.info(
            "l3_hold: demote arm refused (queued) tenant=%s batch=%s code=%s",
            tenant_id, batch_id, exc.code,
        )
        return False
    except Exception:  # noqa: BLE001 — an arm failure leaves the batch awaiting (queued), never raises
        logger.warning(
            "l3_hold: demote arm errored (queued) tenant=%s batch=%s",
            tenant_id, batch_id, exc_info=True,
        )
        return False


def rearm_stranded_batch(tenant_id: UUID | str, *, conn: Any, agent: str = AGENT_NAME) -> str | None:
    """Re-arm trigger for a QUEUED-but-stranded demoted batch (the coordinator sweep leg).

    A C-c collision demote flips a batch to ``awaiting_approval`` WITHOUT arming a fresh approval
    when one is already open for the tenant (mig-128 one-open-per-tenant). When that open approval
    resolves, NOTHING re-arms the queued batch — it would strand ``awaiting_approval`` forever with no
    approval row (the exact stranded-rows class the VT-382 gate blocked on). This sweep leg closes
    that loop: each pass, for a tenant having an ``awaiting_approval`` batch with NO open approval at
    all, it arms the OLDEST such batch via the SAME real-run-id path as the demote arm.

    One-open-per-tenant respected: if ANY approval is already open for the tenant, this is a NO-OP
    (the open one is the tenant's single slot — the queued batch waits for it to resolve). At most one
    batch arms per call (the oldest). Returns the armed batch_id, or None when nothing was armed
    (no stranded batch, an approval already open, or the oldest candidate's run row is absent — it
    stays queued for the next pass). Never raises into the sweep (best-effort per the sweep contract).
    """
    tid = str(tenant_id)
    try:
        from orchestrator.db.wrappers import PendingApprovalsWrapper

        approvals = PendingApprovalsWrapper()
        # One-open-per-tenant: an already-open approval IS the tenant's single slot. Don't arm over it.
        if approvals.has_open_for_tenant(tid, conn=conn):
            return None
        # The oldest awaiting_approval batch for (tenant, agent) with NO open approval referencing it —
        # i.e. a stranded/queued demote (wrapper-scoped composite read, VT-72). FIFO fairness.
        bid = approvals.find_unarmed_awaiting_batch(tid, agent, conn=conn)
        if bid is None:
            return None
        armed = _arm_demoted_batch(tid, bid, conn=conn)
        if armed:
            logger.info(
                "l3_hold: re-armed stranded demoted batch tenant=%s batch=%s agent=%s",
                tid, bid, agent,
            )
            return bid
        # Refusal (queue-busy raced us, or the run row is absent) — stays queued for the next pass.
        return None
    except Exception:  # noqa: BLE001 — best-effort sweep leg; a re-arm failure never halts the sweep
        logger.warning(
            "l3_hold: re-arm sweep leg errored tenant=%s agent=%s", tid, agent, exc_info=True
        )
        return None


# ---------------------------------------------------------------------------
# The HOLD workflow (plan-ack §1/§2) — DBOS, parks on the run-control poll idiom
# ---------------------------------------------------------------------------

# Poll cadence: 15s per read keeps a long park from flooding the DBOS system tables while staying
# responsive (the run_control._DURABLE_POLL_S precedent). Both the read and the inter-poll wait are
# checkpointed @DBOS.step / DBOS.sleep, so a parked hold survives a worker restart (acceptance §6).
_HOLD_POLL_S = 15.0


def _hold_state_body(tenant_id: str, batch_id: str) -> str:
    """Plain body for the hold's checkpointed poll step. Returns one of:
      'send_now'   — batch still auto_send_pending AND send_not_before has passed (anchor set);
      'demote'     — no delivery anchor and the no-delivery window has elapsed;
      'wait'       — still auto_send_pending, neither condition met yet;
      'gone'       — batch left auto_send_pending (demoted by owner inbound / cancelled / sent).
    Module-level so the @DBOS.step qualname is stable for DBOS recovery."""
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT status, "
            "  (send_not_before IS NOT NULL AND send_not_before <= now()) AS due, "
            "  presend_notice_delivered_at IS NULL AS no_delivery, "
            # VT-384: the no-delivery window is anchored on HOLD ENTRY (auto_send_pending_at),
            # NOT created_at — a batch drafted long before it was armed must still get the full
            # config grace from when the hold actually began. COALESCE to updated_at (also set at
            # the enter_l3_hold flip) for any legacy row armed before the auto_send_pending_at
            # column landed — never created_at, which would insta-demote a stale-created batch.
            "  (COALESCE(auto_send_pending_at, updated_at) + (%s * interval '1 minute')) <= now() "
            "    AS demote_window_elapsed "
            "FROM agent_draft_batches WHERE tenant_id = %s AND id = %s",
            (no_delivery_demote_minutes(), tenant_id, batch_id),
        ).fetchone()
    if row is None:
        return "gone"
    status = str(_col(row, "status", 0))
    if status != "auto_send_pending":
        return "gone"
    due = bool(_col(row, "due", 1))
    no_delivery = bool(_col(row, "no_delivery", 2))
    window_elapsed = bool(_col(row, "demote_window_elapsed", 3))
    if no_delivery and window_elapsed:
        return "demote"
    if due:
        return "send_now"
    return "wait"


_hold_state_step: Any | None = None


def l3_hold_workflow(tenant_id: str, batch_id: str) -> dict[str, Any]:
    """The durable L3 hold (plan-ack §1/§2). Parks on the run-control poll idiom until:

      - send_not_before passes (delivery anchored) ⇒ WAKE-side CAS re-check (the batch must STILL
        be auto_send_pending — the two-sided race guard), then per-draft
        ``agent_send_draft(autonomy_level='L3')`` with EVERY existing gate re-evaluated at send
        time (C2 consent / caps / idempotency / registry / signature). The C2 empty frozenset makes
        this ZERO sends end-to-end even on a fully-armed L3 batch.
      - the no-delivery window elapses with no anchor ⇒ ``demote_auto_send_pending`` (no informed
        silence on an undelivered notice).
      - the batch leaves auto_send_pending (owner-inbound demote / cancel) ⇒ the workflow exits a
        no-op (the owner-inbound leg already demoted; whichever side won the CAS, no send fires).

    dbos imports lazily so the module stays importable dep-less (the run_control precedent)."""
    from dbos import DBOS  # lazy — only the DBOS-workflow caller reaches here

    global _hold_state_step
    if _hold_state_step is None:
        _hold_state_step = DBOS.step()(_hold_state_body)

    while True:
        decision = _hold_state_step(tenant_id, batch_id)
        if decision == "wait":
            DBOS.sleep(_HOLD_POLL_S)
            continue
        if decision == "gone":
            logger.info("l3_hold: workflow exit (batch left auto_send_pending) tenant=%s batch=%s",
                        tenant_id, batch_id)
            return {"tenant_id": tenant_id, "batch_id": batch_id, "outcome": "gone"}
        if decision == "demote":
            _hold_demote_step(tenant_id, batch_id)
            return {"tenant_id": tenant_id, "batch_id": batch_id, "outcome": "demoted_no_delivery"}
        # send_now
        sent = _hold_send_step(tenant_id, batch_id)
        return {"tenant_id": tenant_id, "batch_id": batch_id, "outcome": "sent", **sent}


def _hold_demote_step_body(tenant_id: str, batch_id: str) -> None:
    """No-delivery demote leg (checkpointed). Demotes THIS batch with reason='no_delivery'."""
    with tenant_connection(tenant_id) as conn:
        demote_auto_send_pending(tenant_id, conn=conn, reason="no_delivery", batch_id=batch_id)


def _hold_send_step_body(tenant_id: str, batch_id: str) -> dict[str, Any]:
    """Wake-side send leg (checkpointed). WAKE CAS re-check is inside agent_send_draft's gate 1
    (batch must be auto_send_pending). Sends every drafted row through the L3 send path; EVERY gate
    re-runs (C2/caps/idempotency/registry/signature). Returns per-status counters (IDs-only)."""
    from orchestrator.agents.customer_send import agent_send_draft

    with tenant_connection(tenant_id) as conn:
        # Re-confirm the batch is STILL auto_send_pending (two-sided race: the owner-inbound demote
        # may have won between the poll and here). If not, send nothing.
        still = conn.execute(
            "SELECT agent FROM agent_draft_batches WHERE tenant_id = %s AND id = %s "
            "AND status = 'auto_send_pending'",
            (tenant_id, batch_id),
        ).fetchone()
        if still is None:
            return {"sent": 0, "skipped": 0, "raced_out": 1}
        batch_agent = str(_col(still, "agent", 0))
        # VT-384 (contract item 4 — the owner-disengagement substrate): a silent PROCEED (the notice
        # was delivered, the owner stayed silent the whole hold, and we are now firing the auto-send)
        # is a silent-notice event — bump consecutive_silent_l3_notices. Independent of the consent
        # gate: even with C2 empty (zero customer sends) the silence is informed by the DELIVERED
        # notice, so the disengagement counter still advances. grant_l3 zeroes it on a fresh grant.
        conn.execute(
            "UPDATE tenant_agent_autonomy "
            "SET consecutive_silent_l3_notices = consecutive_silent_l3_notices + 1, updated_at = now() "
            "WHERE tenant_id = %s AND agent = %s",
            (tenant_id, batch_agent),
        )
        draft_ids = [
            str(_col(r, "did", 0))
            for r in conn.execute(
                "SELECT id::text AS did FROM agent_drafts "
                "WHERE tenant_id = %s AND batch_id = %s AND status = 'drafted'",
                (tenant_id, batch_id),
            ).fetchall()
        ]
    counters = {"sent": 0, "skipped": 0, "failed": 0}
    for did in draft_ids:
        # Each call opens its own tenant_connection (the gate stack's contract). autonomy_level='L3'
        # routes through the (now real) L3 arm of agent_send_draft.
        result = agent_send_draft(tenant_id, did, autonomy_level="L3")
        if result.status in ("sent", "already_sent"):
            counters["sent"] += 1
        elif result.status == "skipped":
            counters["skipped"] += 1
        else:
            counters["failed"] += 1
    logger.info(
        "l3_hold: hold-wake send tenant=%s batch=%s sent=%d skipped=%d failed=%d",
        tenant_id, batch_id, counters["sent"], counters["skipped"], counters["failed"],
    )
    return counters


_hold_demote_step: Any | None = None
_hold_send_step: Any | None = None


def _ensure_hold_steps() -> None:
    """Lazily decorate the demote/send legs as @DBOS.step (stable qualnames for recovery)."""
    from dbos import DBOS

    global _hold_demote_step, _hold_send_step
    if _hold_demote_step is None:
        _hold_demote_step = DBOS.step()(_hold_demote_step_body)
    if _hold_send_step is None:
        _hold_send_step = DBOS.step()(_hold_send_step_body)


def _hold_demote_step(tenant_id: str, batch_id: str) -> None:
    _ensure_hold_steps()
    assert _hold_demote_step is not None
    return _hold_demote_step(tenant_id, batch_id)  # type: ignore[no-any-return]


def _hold_send_step(tenant_id: str, batch_id: str) -> dict[str, Any]:
    _ensure_hold_steps()
    assert _hold_send_step is not None
    return _hold_send_step(tenant_id, batch_id)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Registration (house pattern — register-before-launch_dbos(); idempotent)
# ---------------------------------------------------------------------------

_registered = False


def register_l3_hold() -> None:
    """Apply ``@DBOS.workflow`` to :func:`l3_hold_workflow`. Call from main.py lifespan BEFORE
    ``launch_dbos()`` (the coordinator/run_control precedent — workflow registration must be in the
    registry when launch computes the app_version hash). Idempotent."""
    from dbos import DBOS

    global _registered
    if _registered:
        return
    DBOS.workflow()(l3_hold_workflow)
    _registered = True


def start_l3_hold(tenant_id: str, batch_id: str) -> None:
    """Start the durable hold workflow for an armed batch (idempotent on the workflow_id —
    DBOS.start_workflow no-ops on a known id). Keyed on the batch so a redelivered arm cannot
    spawn two holds for one batch."""
    from dbos import DBOS, SetWorkflowID

    workflow_id = f"l3_hold_{batch_id}"
    with SetWorkflowID(workflow_id):
        DBOS.start_workflow(l3_hold_workflow, tenant_id, batch_id)


__all__ = [
    "AGENT_NAME",
    "PRESEND_NOTICE_TEMPLATE",
    "DemoteResult",
    "L3ArmResult",
    "demote_auto_send_pending",
    "enter_l3_hold",
    "hold_hours",
    "l3_hold_workflow",
    "no_delivery_demote_minutes",
    "rearm_stranded_batch",
    "register_l3_hold",
    "stamp_delivery_anchor",
    "start_l3_hold",
]
