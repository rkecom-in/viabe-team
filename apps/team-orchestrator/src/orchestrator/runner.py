"""DBOS workflow entry point for an orchestrator pipeline run (VT-3.1).

Pillar 1: no reasoning here — the steps only persist run state and drive the
LangGraph substrate. Pillar 8: one workflow, one substrate.

Each ``@DBOS.step`` is a durable checkpoint. DBOS auto-resumes the workflow
from the last completed step after a crash. Steps are written idempotently so
recovery is safe.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from dbos import DBOS, SetWorkflowID, SetWorkflowTimeout
from psycopg.types.json import Jsonb

from dbos_config import WORKFLOW_TIMEOUT_SECONDS
from orchestrator.db import tenant_connection
from orchestrator.direct_handlers import HANDLERS
from orchestrator.graph import OrchestratorState, get_compiled_graph
from orchestrator.memory.l0_writer import _owner_inputs_enabled
from orchestrator.owner_inputs import run_extraction_for_event
from orchestrator.pre_filter_gate import pre_filter
from orchestrator.state import new_subscriber_state
from orchestrator.types import WebhookEvent
from orchestrator.utils.phone_token import hash_phone

# SHIP GATE (VT-146 / CL 368387c2-cc5a-81ba): owner_inputs extraction
# transmits raw customer message bodies to the classifier vendor for
# structured-intent extraction. Must stay False until the vendor DPA +
# ZDR are executed and the privacy notice is signed (Fazal-owned).
# Flipping this is a reviewed code change by design — do not convert
# to an env var.
OWNER_INPUTS_EXTRACTION_ENABLED = False

logger = logging.getLogger(__name__)


def _brain_owner_inputs_ok(tenant_id: str) -> bool:
    """VT-303 / CL-425 — fail-closed owner_inputs consent check for the brain.

    The brain (dispatch_brain) transmits the owner's inbound body — which may
    carry customer PII — to Anthropic (sub-processor). ``owner_inputs`` is the
    lawful basis (CL-425). Any error reading the flag fails CLOSED (treat as not
    consented): we never transmit on an unknown consent state.
    """
    try:
        return _owner_inputs_enabled(UUID(tenant_id))
    except Exception:  # noqa: BLE001 — fail-closed on any consent-check error
        logger.warning(
            "VT-303: owner_inputs consent check failed (tenant=%s); fail-closed",
            tenant_id,
        )
        return False


def _record_owner_inbound_turn(tenant_id: str, event: WebhookEvent) -> None:
    """VT-583 D2 — record the owner's inbound to the LIFETIME conversation_log EARLY, before any gate
    (approval-resume / journey / integration-resume) can consume it. A gate that consumes-and-closes
    used to leave the message OUT of the manager's lifetime log (the live-run-23 silent-drop class).
    Idempotent per (tenant, message_sid) — the later brain-path record + the journey's own mirror both
    collapse onto this row. Inbound messages only; fail-soft (memory never blocks the pipeline)."""
    if event.message_type != "inbound_message":
        return
    if not (event.body or "").strip():
        return
    try:
        from orchestrator.conversation_log import record_turn

        record_turn(
            tenant_id, "owner", event.body or "",
            message_sid=event.twilio_message_sid, surface="manager",
        )
    except Exception:  # noqa: BLE001 — conversation memory is never a gate on the run
        logger.warning("VT-583: early owner-inbound record failed (fail-soft) tenant=%s", tenant_id)


# VT-583 — the consent ASK is uniquely identifiable in the lifetime log because it instructs the owner
# to reply with the enable phrase (consent_required_handler._ENABLE_PHRASE == "ACTIVATE TEAM"). The
# marker is content-based (the mig-164 surface CHECK only allows journey|manager|system, so we cannot
# tag a bespoke 'consent_ask' surface) — robust because no other assistant send carries this phrase.
_CONSENT_ASK_MARKER = "activate team"


def _last_assistant_turn_was_consent_ask(tenant_id: str, *, within_h: int = 24) -> bool:
    """True iff the MOST-RECENT assistant turn in the lifetime log (within ``within_h`` hours) is a
    consent ASK — its text carries the enable phrase. The consent gate uses this to confirm we actually
    just asked before treating an affirmation as a grant. Fail-soft → False (never grant on a bad read)."""
    try:
        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT text FROM conversation_log WHERE tenant_id = %s AND role = 'assistant' "
                "AND created_at > now() - %s::interval ORDER BY created_at DESC LIMIT 1",
                (str(tenant_id), f"{int(within_h)} hours"),
            ).fetchone()
    except Exception:  # noqa: BLE001 — a read miss must never grant consent
        logger.warning("VT-583: consent-ask marker read failed (fail-soft) tenant=%s", tenant_id)
        return False
    if row is None:
        return False
    text = (row["text"] if isinstance(row, dict) else row[0]) or ""
    return _CONSENT_ASK_MARKER in text.lower()


def _consent_affirm_after_ask(tenant_id: str, body: str) -> bool:
    """VT-583 (CL-2026-07-03-fluid-consent) — True iff THIS reply is an unambiguous affirmation to a
    consent ASK we just sent, so a plain "yes"/"haan"/"start" grants consent via the SAME audited enable
    path the exact "ACTIVATE TEAM" floor uses. Both conditions are required (a grant never rides on a
    guess): (1) a consent ASK was the most-recent thing we sent, and (2) the deterministic (ZERO-LLM —
    the consent boundary forbids a brain transmit here) reply classifier reads an affirm. Fail-safe →
    False (fall to the honest re-ask)."""
    try:
        from orchestrator.pre_filter_gate import classify_consent_intent

        if classify_consent_intent(body) != "affirm":
            return False
        return _last_assistant_turn_was_consent_ask(tenant_id)
    except Exception:  # noqa: BLE001 — any error → the normal consent_required flow (never auto-grant)
        logger.warning("VT-583: consent affirm-after-ask check failed (fail-safe) tenant=%s", tenant_id)
        return False


def _brain_emitted_owner_reply(tenant_id: str, inbound_sid: str | None) -> bool:
    """VT-583 D1 — True iff the brain produced an owner-facing outbound THIS run: an assistant turn in
    the lifetime log at/after the owner's inbound turn (every owner-facing send records one at the
    transport chokepoint). Used to detect a 'completed' run that told the owner NOTHING. Fail-soft →
    True (assume a reply happened — never risk a spurious double-send on an uncertain read)."""
    try:
        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM conversation_log a
                    WHERE a.tenant_id = %s AND a.role = 'assistant'
                      AND a.created_at >= COALESCE(
                          (SELECT o.created_at FROM conversation_log o
                            WHERE o.tenant_id = %s AND o.message_sid = %s AND o.role = 'owner'
                            ORDER BY o.created_at DESC LIMIT 1),
                          now() - interval '30 seconds'
                      )
                ) AS replied
                """,
                (str(tenant_id), str(tenant_id), inbound_sid),
            ).fetchone()
    except Exception:  # noqa: BLE001 — never spam a fallback on an uncertain read
        logger.warning("VT-583: brain-reply detection failed (fail-soft → assume replied) tenant=%s", tenant_id)
        return True
    if row is None:
        return True
    return bool(row["replied"] if isinstance(row, dict) else row[0])


# VT-583 D1 — the honest, substance-railed fallback line (never fabricates specifics). Bilingual; the
# owner's WhatsApp locale picks the register (freeform_acks.resolve_owner_locale).
_COMPLETED_NO_REPLY_FALLBACK = {
    "en": "Got it — I'm on it and I'll update you shortly.",
    "hi": "समझ गया — मैं इस पर काम कर रहा हूँ और जल्द ही आपको अपडेट करूँगा।",
}

# VT-623 Head3 (D1 in-turn wait): when triage started/resumed an async manager_task for THIS turn
# (skip_legacy_dispatch), that durable workflow OWNS the owner reply — the plan summary / send
# confirmation is composed INSIDE it, a beat behind this sync turn. Give it a bounded head-start to
# land IN-TURN before the generic "I'm on it" fallback fires, so a fast async task replies in ONE beat
# instead of "I'm on it" + a delayed real reply (the delegation/approval D1 race). A genuinely-slow
# task still gets the honest ack after the budget. Tunable; measured via the SR/delegate before/after x3.
# T9 inc-3: budget raised 15s→≈96s to cover the observed spawn→SR→collapse→arm chain (~30-60s) — the
# same measured latency the VT-633 D-A approval-arm wait below already budgets for. At 15s the async
# draft nearly ALWAYS missed the turn: D1 fired "I'm on it", the draft landed on the owner's NEXT turn
# (the cross-turn pile-on the §2 judge reads as loop_stall + ignored_speech_act). The loop SHAPE
# (checkpointed poll + DBOS.sleep per iteration) is unchanged — no double-send / no dropped approval
# on replay. Narrow mid-DEPLOY residual (adversarial-verified): a run that exhausted the OLD 15-poll
# budget, recorded a post-loop step, then recovers under the new constant replays a 16th poll where
# close_webhook_run was recorded → DBOSUnexpectedStepError → that single run terminal-fails (its
# "I'm on it" ack already sent; the arm lives in the separate manager_task workflow — unaffected).
# Same risk class as the D-A loop's own introduction at 24 polls.
_D1_INTURN_WAIT_POLL_S = 4.0
_D1_INTURN_WAIT_MAX_POLLS = 24  # ≈96s — bounded so a stuck task never hangs the turn


# VT-633 D-A (approval-arm wait): a CLEAR owner decision that lands while the manager loop is
# still composing/arming its approval must WAIT for the arm (bounded), not be dropped. Budget
# covers the observed spawn→SR→collapse→arm latency (~1 min) with headroom; strictly bounded so
# a stuck loop never hangs the turn (the reply then falls through to the normal path, as today).
_APPROVAL_ARM_WAIT_POLL_S = 4.0
_APPROVAL_ARM_WAIT_MAX_POLLS = 24  # ≈96s


@DBOS.step()
def _open_approval_exists_step(tenant_id: str) -> bool:
    """VT-633 D-A — CHECKPOINTED poll condition for the approval-arm wait (mirrors
    _brain_emitted_owner_reply_step; see that step's replay-determinism note). Never raises:
    a read error reports False (keep waiting / eventually fall through) — failing SOFT here can
    only delay a resolution, never fabricate one."""
    try:
        from orchestrator.agent.approval_resume import find_open_approval_for_tenant

        with tenant_connection(tenant_id) as conn:
            return find_open_approval_for_tenant(conn, tenant_id) is not None
    except Exception:  # noqa: BLE001 — a control-read outage must not kill a live inbound run
        logger.warning("VT-633: open-approval poll read failed (fail-soft) tenant=%s", tenant_id)
        return False


@DBOS.step()
def _open_customer_send_approval_exists_step(tenant_id: str) -> bool:
    """R1 — CHECKPOINTED guard: True iff the tenant has an OPEN approval whose type is a CUSTOMER SEND
    (money; approval_resume._CUSTOMER_SEND_APPROVAL_TYPES). Mirrors _open_approval_exists_step but
    TYPE-scoped — the send-push re-confirm net (_maybe_reconfirm_send_push) must fire ONLY for a real
    customer-send approval, never a non-send governance approval (autonomy_upgrade, …). Never raises:
    a read error reports False (skip the re-confirm) — failing SOFT here can only DROP a re-confirm,
    never fabricate one, and it never resolves/sends anything."""
    try:
        from orchestrator.agent.approval_resume import (
            _CUSTOMER_SEND_APPROVAL_TYPES,
            find_open_approval_for_tenant,
        )

        with tenant_connection(tenant_id) as conn:
            approval = find_open_approval_for_tenant(conn, tenant_id)
        return (
            approval is not None
            and approval.get("approval_type") in _CUSTOMER_SEND_APPROVAL_TYPES
        )
    except Exception:  # noqa: BLE001 — a control-read outage must not kill a live inbound run
        logger.warning(
            "R1: open customer-send approval read failed (fail-soft) tenant=%s", tenant_id
        )
        return False


@DBOS.step()
def _tenant_is_opted_out_step(tenant_id: str) -> bool:
    """R5 / CD6 — CHECKPOINTED read of ``tenants.opt_out`` for the opted-out RESUME leg. A @DBOS.step
    (a LIVE read, like the other webhook-path control reads) so replay re-walks the same routing.
    Fail-soft False: a read error can only SKIP the resume leg (the restart falls through to the normal
    pipeline), never fabricate a consent clear."""
    try:
        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT opt_out FROM tenants WHERE id = %s LIMIT 1", (tenant_id,)
            ).fetchone()
        if row is None:
            return False
        return bool(row["opt_out"] if isinstance(row, dict) else row[0])
    except Exception:  # noqa: BLE001 — a control-read outage must not kill a live inbound run
        logger.warning("CD6: opt_out read failed (fail-soft) tenant=%s", tenant_id)
        return False


@DBOS.step()
def _should_wait_for_approval_arm(tenant_id: str, body: str) -> bool:
    """VT-633 D-A — CHECKPOINTED gate for the approval-arm wait: True iff the reply is a CLEAR
    deterministic decision (classify_approval_reply — never the LLM classifier pre-arm; an
    ambiguous reply must not wait at all) OR a SEND-PUSH cue (R1 — is_send_push_cue: an
    ambiguous-but-explicit "just send it" the classifier deliberately holds as None), AND a manager
    task is actively in flight (the only situation in which an arm can still be coming). A @DBOS.step
    because has_active_task is a LIVE read: left un-memoized, a replay could re-evaluate it
    differently and skip/enter the sleep loop with a different step count (the same divergence class
    the poll step guards). Fail-soft False: an error here just means the reply falls through as it
    always did.

    R1: adding the send-push cue extends the mid-turn ARM RACE cover to it too — an urgent
    "sabko bhej do" that PRECEDES the arm now waits for the arm and is then re-confirmed by the R1 net
    (_maybe_reconfirm_send_push), instead of falling through to the brain (which reads its own turn as
    approval-taken / spawns a competing plan). The send-push cue never RESOLVES the approval (try_resume
    still returns None on it); it only earns the bounded wait so the re-confirm net can see the arm."""
    try:
        from orchestrator.manager import task_store
        from orchestrator.owner_inputs.approval_reply import (
            classify_approval_reply,
            is_send_push_cue,
        )

        if classify_approval_reply(body or "") is None and not is_send_push_cue(body or ""):
            return False
        return task_store.has_active_task(tenant_id)
    except Exception:  # noqa: BLE001 — gating must never kill a live inbound run
        logger.warning("VT-633: arm-wait gate read failed (fail-soft) tenant=%s", tenant_id)
        return False


@DBOS.step()
def _journey_represent_instead_of_consent_ask(tenant_id: str, event: Any) -> bool:
    """VT-693 — while an onboarding journey is ACTIVE, a message the journey gate did not
    consume must NOT fall into the ACTIVATE consent pitch (the measured mid-questions
    non-sequitur). Re-present the journey's current question instead and consume the turn.
    A ``@DBOS.step`` (it sends — at-most-once across replay). Returns True iff it handled the
    turn; False (incl. any error, fail-open) → the normal consent_required flow runs."""
    try:
        from orchestrator.onboarding.journey import _current, get_journey

        g = get_journey(tenant_id)
        if g is None or g.get("status") != "active":
            return False
        q = _current(g)
        if not q:
            return False
        prompt = q.get("prompt_en") or ""
        if not prompt:
            return False
        recipient = getattr(event, "sender_phone", None)
        if not recipient:
            return False
        # VT-701 (live: "What does that mean?" got a robotic re-ask) — the re-present is
        # HELPFUL: the question's plain-language explanation leads when we have one, and the
        # question rides journey._send so its suggestion buttons / card formatting come along.
        # Deterministic (no LLM — this path exists precisely because the journey gate failed).
        from orchestrator.onboarding.journey import _send as _journey_send
        from orchestrator.onboarding.question_brain import field_help

        help_en = str(q.get("help_en") or "").strip() or field_help(str(q.get("field") or ""))[0]
        q2 = dict(q)
        if help_en:
            q2["prompt_en"] = f"{help_en}\n\n{prompt}"
        else:
            q2["prompt_en"] = f"Let's finish setting up first — {prompt}"
        _journey_send(recipient, q2, "en", tenant_id=tenant_id)
        return True
    except Exception:  # noqa: BLE001 — fail-open to the normal consent flow
        logger.warning("VT-693 journey-represent guard failed (fail-open) tenant=%s", tenant_id)
        return False


@DBOS.step()
def _post_turn_drain_step(tenant_id: str, recipient: str | None) -> bool:
    """VT-683 P2b — the post-turn owner-comms drain: after a COMPLETED owner turn (an idle
    moment by construction — the exchange just finished), deliver at most ONE queued
    owner-comms item (approval > report > notice) inside the still-open 24h session. A
    ``@DBOS.step`` for the same replay-safety reason as ``_send_owner_reply_step``: the
    underlying freeform send must fire AT MOST ONCE across a mid-turn worker restart.
    Deliberately NOT called on the approval-resume branch — that turn wakes the manager
    loop (campaign execution + its own follow-ups), not an idle moment; the */10 scheduled
    sweep's idle gate covers those tenants instead. Never raises (drain_one is best-effort;
    belt-wrapped anyway because this sits on the live inbound path)."""
    try:
        from orchestrator.owner_surface.freeform_acks import resolve_owner_locale
        from orchestrator.owner_surface.owner_comms_drainer import drain_one

        delivered = drain_one(tenant_id, recipient, lang=resolve_owner_locale(tenant_id))
        return delivered is not None
    except Exception:  # noqa: BLE001 — a drain failure must never break the turn
        logger.warning("VT-683 post-turn drain failed (fail-soft) tenant=%s", tenant_id)
        return False


@DBOS.step()
def _send_owner_reply_step(tenant_id: str, recipient: str | None, text: str | None) -> bool:
    """Shared canonical REPLAY-SAFE in-turn owner send for the deterministic seam nets (D2/D3).

    A detector is a PURE function that only RETURNS text; the ONE send site is here, inside a
    ``@DBOS.step`` — a completed step returns its checkpointed result on replay, so the underlying
    Twilio ``create()`` fires AT MOST ONCE (no double-send on a mid-turn worker restart). This is the
    single reason nets never send from the plain-fn seam/runner body directly. ``send_freeform_ack``
    records the 'assistant' leg (surface='manager') so the D1 in-turn wait + fallback below see a
    reply and do NOT double-send. Never raises (send_freeform_ack swallows send errors)."""
    if not recipient or not text:
        return False
    from orchestrator.owner_surface.freeform_acks import send_freeform_ack

    return send_freeform_ack(tenant_id, recipient, text)


@DBOS.step()
def _brain_emitted_owner_reply_step(tenant_id: str, inbound_sid: str | None) -> bool:
    """VT-623 Head3 — the CHECKPOINTED form of :func:`_brain_emitted_owner_reply`, used ONLY as the D1
    in-turn-wait poll condition. Module-level ``@DBOS.step`` (mirrors ``read_webhook_pause`` /
    manager.workflow ``_approval_still_pending``): a poll loop that ``DBOS.sleep``s between a NON-step
    live read would replay a DIFFERENT number of sleeps after a mid-turn worker restart (the read moves
    with wall-clock), shifting every later step's function_id → DBOS non-determinism → a wedged run.
    Memoizing the condition makes replay re-walk the identical sleep sequence. Never raises (the inner
    read fail-softs to True), so a control-read outage can't kill a live inbound run. The D1 fallback's
    OWN one-shot call below stays the plain function — a single non-step read is deterministic by count."""
    return _brain_emitted_owner_reply(tenant_id, inbound_sid)


def _send_completed_no_reply_fallback(tenant_id: str, event: WebhookEvent) -> None:
    """VT-583 D1 — a brain run that COMPLETED but produced no owner-facing send owes the owner ONE honest
    acknowledgement (never silence, never a fabricated specific). Sends through the existing in-session
    manager path (records its own assistant turn, so it can't loop). Best-effort — never breaks the run."""
    recipient = event.sender_phone or None
    if not recipient:
        return
    try:
        from orchestrator.owner_surface.freeform_acks import resolve_owner_locale, send_freeform_ack

        locale = resolve_owner_locale(tenant_id)
        body = _COMPLETED_NO_REPLY_FALLBACK["hi" if locale == "hi" else "en"]
        send_freeform_ack(tenant_id, recipient, body)
        logger.info("VT-583 D1: completed-no-reply fallback sent (tenant=%s)", tenant_id)
    except Exception:  # noqa: BLE001 — the safety-net send must never break the durable run
        logger.warning("VT-583 D1: completed-no-reply fallback failed (fail-soft) tenant=%s", tenant_id)


# T8 — re-surface copy: a RESUME cue ("do what you were saying / continue") that lands while an
# approval is already armed must re-point the owner at THAT plan, not spawn a competing one. Honest
# (there IS a plan waiting), advancing (says exactly what to do), and it NEVER claims a send or
# invents cohort details — the specifics live in the original approval ask still on the thread.
_RESURFACE_PENDING_APPROVAL = {
    "en": (
        "You've already got a plan waiting for your approval — reply \"yes\" to send it, "
        "or tell me what you'd like to change."
    ),
    "hi": (
        "Aapki approval ke liye ek plan pehle se taiyaar hai — bhejne ke liye \"yes\" bol dein, "
        "ya batayein kya badalna chahenge."
    ),
}


def _maybe_resurface_pending_approval(tenant_id: str, event: WebhookEvent) -> bool:
    """T8 — if the inbound is a RESUME cue AND an approval is already armed, re-surface THAT
    approval and CONSUME the turn, instead of letting it fall through to triage/new_task.

    The confirmed §2 breaker (m_conversation_interruption_midtask_resume_winback): the owner says
    "chalo jo pehle bol raha tha wahi karo" (resume) while a win-back approval is pending; T5
    correctly refuses to auto-SEND on that vague reply (classify -> None), but the turn then falls
    through and the Manager drafts a SECOND, different plan and deflects ("settle the other one
    first") — an ignored_speech_act / wrong_action / loop_stall. This complements T5: on a resume
    cue we ADVANCE by re-pointing the owner at the plan already waiting, never a competing draft,
    never a claimed send.

    Deterministic + honest + best-effort: opt-out/DSR and non-resume replies are excluded (they must
    keep their normal path); any failure returns False so the normal pipeline still runs."""
    recipient = event.sender_phone or None
    if not recipient:
        return False
    try:
        from orchestrator.owner_inputs.approval_reply import is_resume_cue
        from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

        body = event.body or ""
        # opt-out / DSR always wins (compliance) — never re-surface an approval over a STOP.
        if matches_opt_out_or_dsr(body) or not is_resume_cue(body):
            return False
        if not _open_approval_exists_step(tenant_id):
            return False

        from orchestrator.owner_surface.freeform_acks import resolve_owner_locale, send_freeform_ack

        locale = resolve_owner_locale(tenant_id)
        send_freeform_ack(
            tenant_id, recipient, _RESURFACE_PENDING_APPROVAL["hi" if locale == "hi" else "en"]
        )
        logger.info("T8: resume-cue re-surfaced pending approval (tenant=%s)", tenant_id)
        return True
    except Exception:  # noqa: BLE001 — a re-surface hiccup must not break the durable run
        logger.warning("T8: approval re-surface failed (fail-soft) tenant=%s", tenant_id)
        return False


# R1 — send-push re-confirm copy. An ambiguous send PUSH ("jaldi karo, sabko bhej do" / a long
# "seedha bhej do" / a bare weak "theek hai") that lands against an OPEN customer-send approval must be
# re-confirmed, NOT sent and NOT fallen-through to the brain. Honest (the plan IS armed, waiting),
# money-safe (NOTHING is sent), and it names the ONE explicit reply that resolves — so the owner is a
# single "haan bhej do" from the unchanged approval-resume path. Never claims a send, never invents
# cohort details (those live in the original approval ask still on the thread).
_RECONFIRM_SEND_PUSH = {
    "en": (
        "Your plan is armed and waiting for your go-ahead — I haven't sent anything yet. "
        "Reply \"haan bhej do\" to send it now, or tell me what you'd like to change."
    ),
    "hi": (
        "Aapka plan taiyaar hai aur aapki haan ka intezaar kar raha hai — maine abhi tak kuch "
        "nahi bheja. Bhejne ke liye \"haan bhej do\" likhein, ya batayein kya badalna hai."
    ),
}


def _maybe_reconfirm_send_push(tenant_id: str, event: WebhookEvent) -> bool:
    """R1 — if the inbound is a SEND-PUSH cue AND an OPEN customer-send approval is armed, RE-CONFIRM
    that approval and CONSUME the turn, instead of letting an ambiguous send-intent reply fall through
    to the brain (which was measured to (a) read its own turn as approval-taken and claim a send, or
    (b) spawn a SECOND competing plan / deflect "settle the other one first" — both bulk-send breakers).

    Complements T5/T8: T5 refuses to auto-SEND on the ambiguous reply (classify -> None), T8 re-points a
    vague RESUME cue, and this re-confirms an explicit-but-unresolvable SEND PUSH. It STRICTLY TIGHTENS —
    only SPEAKS (a re-confirm), never resolves/approves/sends; the plan stays ARMED and NOTHING is sent;
    the >12-token CD5 ambiguity floor is untouched.

    Deterministic + honest + best-effort: opt-out/DSR is excluded FIRST (a STOP keeps its compliance
    path — never re-confirm over a STOP), the approval TYPE must be a real customer-send (money), the
    send goes through the replay-safe ``_send_owner_reply_step`` (@DBOS.step — at-most-once), and any
    failure returns False so the normal pipeline still runs."""
    recipient = event.sender_phone or None
    if not recipient:
        return False
    try:
        from orchestrator.owner_inputs.approval_reply import is_send_push_cue
        from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

        body = event.body or ""
        # opt-out / DSR always wins (compliance) — never re-confirm over a STOP.
        if matches_opt_out_or_dsr(body) or not is_send_push_cue(body):
            return False
        # Type-scoped: only a real OPEN customer-send approval (money) earns the re-confirm; a resolved
        # or non-send approval reports False here, so this can never be reached by a resolved approval.
        if not _open_customer_send_approval_exists_step(tenant_id):
            return False

        from orchestrator.owner_surface.freeform_acks import resolve_owner_locale

        locale = resolve_owner_locale(tenant_id)
        _send_owner_reply_step(
            tenant_id, recipient, _RECONFIRM_SEND_PUSH["hi" if locale == "hi" else "en"]
        )
        logger.info(
            "R1: send-push cue re-confirmed pending customer-send approval (tenant=%s)", tenant_id
        )
        return True
    except Exception:  # noqa: BLE001 — a re-confirm hiccup must not break the durable run
        logger.warning("R1: send-push re-confirm failed (fail-soft) tenant=%s", tenant_id)
        return False


def _load_preferred_language(tenant_id: str) -> str | None:
    """VT-416 PR-3 wiring — read the tenant's WhatsApp language preference.

    Resolves ``tenants.preferred_language ?? language_preference`` under the
    tenant_connection (RLS-scoped), so the per-tenant value reaches
    ``SubscriberState['preferred_language']`` and the output_composer renders
    the right-language template variant (a Hindi-preference owner gets the
    Hindi variant — the bug PR-3 made latent, now LIVE). Column semantics
    mirror ``get_business_profile``'s locale resolution (mig 001):
    ``preferred_language`` (nullable, the explicit per-tenant choice) wins,
    else ``language_preference`` (NOT NULL DEFAULT 'en').

    Best-effort: returns ``None`` on ANY read failure (missing row, DB error)
    — the composer then falls back to its global ``TENANT_DEFAULT_LANGUAGE``
    default, so a language-read hiccup NEVER breaks dispatch. Returning the
    raw column value (not normalised here) keeps this read dumb; the composer
    owns 'en'/'hi' validation + the fallback.
    """
    try:
        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT preferred_language, language_preference "
                "FROM tenants WHERE id = %s LIMIT 1",
                (tenant_id,),
            ).fetchone()
        if row is None:
            return None
        # The shared pool uses ``row_factory=dict_row`` (graph.get_pool), so the
        # row is keyed by column name; tolerate a tuple row too (raw connections).
        if isinstance(row, dict):
            preferred, language_pref = (
                row.get("preferred_language"),
                row.get("language_preference"),
            )
        else:
            preferred, language_pref = row[0], row[1]
        return preferred or language_pref or None
    except Exception as exc:  # noqa: BLE001 — language read is best-effort
        logger.warning(
            "VT-416: preferred_language read failed (tenant=%s); "
            "composer will use the global default",
            tenant_id,
            extra={"exc": repr(exc)},
        )
        return None


@DBOS.step()
def open_run(tenant_id: str, run_id: str) -> None:
    """Record the run as started. Idempotent (ON CONFLICT) so recovery is safe."""
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running') "
            "ON CONFLICT (id) DO NOTHING",
            (run_id, tenant_id),
        )


@DBOS.step()
def invoke_graph(tenant_id: str, run_id: str, inbound: str) -> list[str]:
    """Run the LangGraph substrate for this run. thread_id == run_id."""
    state: OrchestratorState = {
        "tenant_id": UUID(tenant_id),
        "run_id": UUID(run_id),
        "history": [inbound],
    }
    result = get_compiled_graph().invoke(state, config={"configurable": {"thread_id": run_id}})
    return list(result["history"])


@DBOS.step()
def close_run(tenant_id: str, run_id: str) -> None:
    """Mark the run completed. Idempotent.

    tenant_id is required so the UPDATE runs under tenant_connection — under
    RLS the WHERE id = %s is scoped by the USING clause, so without the GUC
    set the UPDATE is a silent no-op (CL-71).
    """
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status = 'completed', ended_at = now() WHERE id = %s",
            (run_id,),
        )


@DBOS.workflow()
def pipeline_run(tenant_id: str, run_id: str, inbound: str) -> dict[str, Any]:
    """Durable orchestrator pipeline run — three checkpointed steps."""
    open_run(tenant_id, run_id)
    history = invoke_graph(tenant_id, run_id, inbound)
    close_run(tenant_id, run_id)
    return {"tenant_id": tenant_id, "run_id": run_id, "history": history}


def run_pipeline(tenant_id: str, run_id: str, inbound: str) -> dict[str, Any]:
    """Run ``pipeline_run`` durably, keyed on ``run_id`` for idempotency.

    The 6-minute timeout and run_id-as-workflow-id are applied here: invoking
    twice with the same run_id returns the first run's result without
    re-executing (DBOS idempotency).
    """
    with SetWorkflowTimeout(WORKFLOW_TIMEOUT_SECONDS), SetWorkflowID(run_id):
        return pipeline_run(tenant_id, run_id, inbound)


# --- VT-3.3a: Twilio inbound webhook ingress pipeline ------------------------
#
# A separate workflow from pipeline_run (VT-3.1's LangGraph-substrate smoke
# path) — the ingress pipeline is ingress -> Pre-Filter Gate -> direct handler.
# pipeline_run is left untouched so VT-3.1's synthetic tests keep passing.


# Keys forbidden from any JSONB persisted into pipeline_runs.trigger_payload
# or pipeline_steps.input_envelope. ``body`` is the WhatsApp message text;
# the rest are defensive aliases. Centralised here so a future caller cannot
# bypass redaction by passing a body-bearing dict — VT-144 (PR #45) placed
# the pop at the caller (webhook_pipeline_run); this PR pushes it to the
# persistence boundary so NO write path to either sink can leak.
_REDACTED_KEYS_AT_REST = frozenset({"body", "message_body", "raw_text", "content"})


def _redact_for_persistence(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``payload`` with all
    ``_REDACTED_KEYS_AT_REST`` removed.

    Single source of truth for "what must never reach
    ``pipeline_runs.trigger_payload`` / ``pipeline_steps.input_envelope``".
    Shallow-copy is correct here — the persisted envelope is one level
    of keys; redaction operates on top-level keys only by design (per
    the VT-144 / VT-Privacy-Body brief, message-content fields live at
    the top level).
    """
    return {k: v for k, v in payload.items() if k not in _REDACTED_KEYS_AT_REST}


@DBOS.step()
def open_webhook_run(tenant_id: str, run_id: str, trigger_payload: dict) -> None:
    """Record the inbound run in pipeline_runs. Idempotent — a redelivered
    MessageSid maps to the same run_id. trigger_payload is phone-tokenised.

    Body-key redaction is applied at this persistence boundary (NOT by
    the caller) so no future caller can leak message content into
    ``trigger_payload``. The redacted dict is wrapped in ``Jsonb`` for
    the INSERT; the input dict is not mutated.
    """
    safe_payload = _redact_for_persistence(trigger_payload)
    # VT-309: record the run AND the L2 owner_message_received episodic event in
    # ONE txn (atomic per Cowork ruling 20260603T191000Z). LIVE dispatch path —
    # highest care: the payload carries ONLY derived/structural fields
    # (message_type + body LENGTH), NEVER the raw body (CL-390 / CL-330). The
    # body never enters the episodic row. Gated to real inbound messages (not
    # status-callbacks, not dupes); deterministic event_id → idempotent on
    # redelivery / DBOS step retry.
    with tenant_connection(tenant_id) as conn, conn.transaction():
        conn.execute(
            "INSERT INTO pipeline_runs "
            "(id, tenant_id, run_type, status, trigger_payload) "
            "VALUES (%s, %s, 'twilio_inbound', 'running', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (run_id, tenant_id, Jsonb(safe_payload)),
        )
        if trigger_payload.get("message_type") == "inbound_message" and not trigger_payload.get(
            "dupe_status"
        ):
            from orchestrator.knowledge.l2_types import L2EventType
            from orchestrator.knowledge.l2_writer import (
                deterministic_event_id,
                record_episodic_event,
            )

            record_episodic_event(
                tenant_id,
                L2EventType.OWNER_MESSAGE_RECEIVED,
                payload={
                    "message_type": "inbound_message",
                    "body_length": len(trigger_payload.get("body") or ""),
                    "has_media": bool(trigger_payload.get("num_media", 0)),
                    "run_id": run_id,
                },
                referenced_entity_type="run",
                referenced_entity_id=run_id,
                event_id=deterministic_event_id(
                    tenant_id, L2EventType.OWNER_MESSAGE_RECEIVED, run_id
                ),
                conn=conn,
            )


@DBOS.step()
def record_webhook_received(tenant_id: str, run_id: str, envelope: dict) -> None:
    """Write the webhook_received step_record (step_seq=0) to pipeline_steps.

    The envelope is phone-tokenised — no plaintext PII (Pillar 3 / Pillar 7).
    Body-key redaction is applied at this persistence boundary so no
    future caller can leak message content into ``input_envelope``.

    Idempotency is provided by the DBOS workflow-id boundary for COMPLETED
    steps. A crash between the SQL commit and DBOS recording the step causes
    re-execution on workflow resume — hence the ON CONFLICT (run_id, step_seq)
    DO NOTHING clause. Migration 014's UNIQUE (run_id, step_seq) constraint
    makes ON CONFLICT well-defined.
    """
    safe_envelope = _redact_for_persistence(envelope)
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "INSERT INTO pipeline_steps "
            "(run_id, tenant_id, step_seq, step_kind, input_envelope, status) "
            "VALUES (%s, %s, 0, 'webhook_received', %s, 'completed') "
            "ON CONFLICT (run_id, step_seq) DO NOTHING",
            (run_id, tenant_id, Jsonb(safe_envelope)),
        )


@DBOS.step()
def close_webhook_run(tenant_id: str, run_id: str, status: str) -> None:
    """Mark the inbound run finished. Idempotent.

    tenant_id is required so the UPDATE runs under tenant_connection — the
    WHERE id = %s is scoped by the RLS USING clause, so without the GUC set
    the UPDATE silently affects 0 rows (CL-71).
    """
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status = %s, ended_at = now() WHERE id = %s",
            (status, run_id),
        )


# --- VT-374 N3/B2: the pre-dispatch_brain run-control hold ---------------------
#
# Poll/bound for the webhook_inbound pause seam. 15s per poll keeps a long pause
# from flooding the DBOS system tables (one checkpointed step per read) while
# staying responsive to an ops /release; 1800s (30 min) is the max park — no
# worker holds a durable run forever (B2). On exceeding the bound the run closes
# as status='paused' (mig-052 CHECK member) and the brain is NOT dispatched.
_RUN_CONTROL_POLL_S = 15.0
_RUN_CONTROL_MAX_HOLD_S = 1800.0


@DBOS.step()
def read_webhook_pause(tenant_id: str) -> bool:
    """Checkpointed control read for the pre-dispatch_brain hold (N3).

    Module-level ``@DBOS.step`` so the qualname is stable for DBOS recovery — a
    paused run survives a worker restart and resumes the hold (plan §10.2).
    ``check_pause`` inside never raises (F9 two-tier): a control-read outage
    cannot fail this step and kill a live inbound run.
    """
    from orchestrator.run_control import check_pause

    return check_pause(tenant_id, "webhook_inbound")


@DBOS.step()
def close_webhook_run_paused(tenant_id: str, run_id: str) -> None:
    """Close a max-hold-exceeded inbound run as status='paused' (B2). Idempotent.

    'paused' is a legal pipeline_runs_status_check member (mig 052);
    ``terminal_state_metadata.paused_by_run_control`` marks it for the panel
    (Phase B copy obligation: this run is parked, not failed). tenant GUC per
    CL-71 (RLS scopes the UPDATE).
    """
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status = 'paused', ended_at = now(), "
            "terminal_state_metadata = %s WHERE id = %s",
            (Jsonb({"paused_by_run_control": True}), run_id),
        )


# Dispatch final_status values that mean the agent dispatch TERMINATED (failure/
# limit), vs 'completed' (success) and 'paused' (not terminal — resumes later).
_DISPATCH_TERMINATED_STATUSES = frozenset({"aborted_hard_limit", "escalated", "failed"})


@DBOS.step()
def record_dispatch_terminal_episodic(
    tenant_id: str, run_id: str, final_status: str, terminal_path: str | None
) -> None:
    """VT-309 — emit the L2 agent-dispatch lifecycle episodic event for a brain
    dispatch's terminal status.

    'completed' → agent_dispatch_completed; a terminated status → agent_dispatch_
    terminated; 'paused' (and anything unrecognised) → no emit (not a terminal
    decision — never guess). Best-effort: an emit failure must not fail the
    durable workflow. Safely at-least-once, NOT txn-atomic with the pipeline_runs
    status write — these are derived observability-lifecycle events (the run-status
    row is the source of truth); the DBOS step boundary + deterministic event_id
    make a retry a no-op (episodic_events UNIQUE(tenant_id, event_id)).
    """
    # VT-356: terminal_path is str | None (the terminated branch carries it raw), so the payload
    # value type is str | None — annotate it, else mypy widens to dict[str, str] from the first
    # branch and the 2nd branch's None-able entry is flagged.
    payload: dict[str, str | None]
    if final_status == "completed":
        event_type = "agent_dispatch_completed"
        payload = {"run_id": run_id, "outcome": terminal_path or final_status}
    elif final_status in _DISPATCH_TERMINATED_STATUSES:
        event_type = "agent_dispatch_terminated"
        payload = {"run_id": run_id, "reason": final_status, "terminal_path": terminal_path}
    else:
        return  # paused / unrecognised → not a terminal decision
    try:
        from orchestrator.knowledge.l2_writer import (
            deterministic_event_id,
            record_episodic_event,
        )

        record_episodic_event(
            tenant_id,
            event_type,
            payload=payload,
            referenced_entity_type="run",
            referenced_entity_id=run_id,
            event_id=deterministic_event_id(tenant_id, event_type, run_id),
        )
    except Exception:  # noqa: BLE001 — L2 projection must never fail the workflow
        logger.exception(
            "VT-309 dispatch-terminal L2 emit failed (tenant=%s run=%s status=%s)",
            tenant_id,
            run_id,
            final_status,
        )


# Twilio status-callback states (vs a plain inbound message).
_CALLBACK_STATES = {"delivered", "read", "failed", "undelivered"}


def build_webhook_event(fields: dict[str, Any], dupe_status: bool) -> WebhookEvent:
    """Construct a WebhookEvent from raw Twilio fields. Plain helper (no LLM)."""
    callback_state = fields.get("MessageStatus")
    is_callback = callback_state in _CALLBACK_STATES
    return WebhookEvent(
        body=str(fields.get("Body", "")),
        sender_phone=str(fields.get("From", "")),
        message_type="status_callback" if is_callback else "inbound_message",
        twilio_message_sid=fields.get("MessageSid"),
        status_callback_state=callback_state if is_callback else None,
        dupe_status=dupe_status,
        num_media=int(fields.get("NumMedia", 0) or 0),
        media_url_0=fields.get("MediaUrl0"),
    )


@DBOS.step()
def record_inbound_message_sid(tenant_id: str, message_sid: str) -> bool:
    """Record the MessageSid in the idempotency ledger — the FIRST workflow step.

    Returns True if newly inserted, False if already seen. C2 fix (CL-72): this
    runs inside the durable workflow boundary, so a half-completed ingress can
    never leave a row that makes the next attempt look like a duplicate.
    """
    with tenant_connection(tenant_id) as conn:
        cur = conn.execute(
            "INSERT INTO twilio_inbound_events (message_sid, tenant_id) "
            "VALUES (%s, %s) ON CONFLICT (message_sid) DO NOTHING",
            (message_sid, tenant_id),
        )
        return cur.rowcount == 1


@DBOS.step()
def try_resume_pending_approval(tenant_id: str, body: str, message_sid: str | None) -> str | None:
    """VT-47 — if the tenant has a PAUSED run awaiting owner approval, treat
    this inbound message as the approval decision and resume that run.

    Returns the resolved decision verb ('approved'|'rejected'|'needs_changes')
    if this message was consumed as an approval reply, else None (the message
    is a normal inbound — fall through to pre_filter/dispatch).

    Pillar 7: an unclear reply (other / low-confidence) does NOT resolve the
    gate (resolve_decision_from_reply returns None) — the run stays paused and
    the message falls through. We never guess approval.

    Steps (all under the tenant GUC so RLS is real):
      1. Find the most-recent open pending_approvals for the tenant.
      2. Classify the reply (VT-49). None -> not consumed.
      3. Mark the row resolved (decision + status + resolved_at).
      4. Resume the paused LangGraph run with Command(resume={decision}).
      5. Drive the ORIGINAL paused run's pipeline_runs.status -> 'completed'.
    """
    # VT-369 CRITICAL-1 (live compliance bug, DPDP): opt-out / DSR ALWAYS wins over the
    # approval classifier. 'stop' and 'cancel' are members of approval_reply._REJECT_KW,
    # so without this guard an owner opt-out ("STOP" / "बंद करो" / "delete my data")
    # arriving while ANY approval is open would be CONSUMED here as a campaign/batch
    # rejection instead of reaching the authoritative opt-out / DSR handler. Mirrors the
    # journey-gate guard (onboarding/journey.py maybe_handle_journey_reply): return None
    # so the inbound falls through to pre_filter, which routes it to opt_out_handler /
    # dsr_handler. The open approval row stays open (the 30-min timeout sweep owns it).
    from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

    if matches_opt_out_or_dsr(body or ""):
        return None

    from orchestrator.agent.approval_resume import (
        find_open_approval_for_tenant,
        mark_approval_resolved,
        resolve_decision_from_reply,
        resume_run,
    )

    with tenant_connection(tenant_id) as conn:
        approval = find_open_approval_for_tenant(conn, tenant_id)
    if approval is None:
        return None

    decision = resolve_decision_from_reply(
        body, tenant_id=tenant_id, approval_type=approval.get("approval_type"),
    )
    if decision is None:
        # Unclear reply — leave the gate paused (Pillar 7: no guessing). For a customer-SEND
        # approval this is also where a vague resume ("do what you were saying") lands now, instead
        # of being mis-resolved to 'approved' — the send never fires; the turn falls through.
        return None

    # VT-309: resolve the approval + emit the L2 episodic decision ATOMICALLY
    # (one txn — the autocommit site the plan flagged; now wrapped per Cowork
    # ruling 20260603T191000Z). approved → campaign_approved, rejected →
    # campaign_rejected; needs_changes has no L2 milestone type → no emit.
    with tenant_connection(tenant_id) as conn, conn.transaction():
        # VT-369: owner_feedback (the raw reply body) is threaded through so a
        # needs_changes on an agent_customer_send approval can store it on the
        # draft batch (RLS-protected agent_draft_batches.owner_feedback; CL-390 —
        # it is persisted, never logged). The batch-state application happens
        # INSIDE mark_approval_resolved (the single resolution choke point, shared
        # with the timeout sweep), atomic with this transaction.
        resolved = mark_approval_resolved(
            conn, tenant_id, approval["id"], decision,
            owner_message_sid=message_sid, owner_feedback=body,
        )
        # VT-334: a 'defer' that only EXTENDS the window returns resolved=False — the run stays
        # paused (no L2 emit, no resume). The L2 + resume happen only on a real resolution
        # (incl. an exhausted defer, which resolves as a rejection).
        _l2_event = (
            {
                "approved": "campaign_approved",
                "rejected": "campaign_rejected",
                "defer": "campaign_rejected",  # an exhausted defer resolves as a rejection
            }.get(decision)
            if resolved
            else None
        )
        # Only campaign approvals map to an L2 milestone; other approval_types
        # (sensitive_data_access, …) have no campaign_* episodic type.
        if _l2_event is not None and approval.get("approval_type") == "campaign_send":
            from orchestrator.knowledge.l2_writer import (
                deterministic_event_id,
                record_episodic_event,
            )

            _campaign_id = approval.get("campaign_id")
            record_episodic_event(
                tenant_id,
                _l2_event,
                payload={
                    "campaign_id": _campaign_id,
                    "approval_id": str(approval["id"]),
                },
                referenced_entity_type="campaign" if _campaign_id else "approval",
                referenced_entity_id=_campaign_id or approval["id"],
                event_id=deterministic_event_id(tenant_id, _l2_event, approval["id"]),
                conn=conn,
            )

    # VT-334: a defer that only EXTENDED the window leaves the run PAUSED — do not resume or
    # close. The owner gets another 48h; the next reply re-enters here.
    if not resolved:
        logger.info(
            "approval-resume: deferred (window extended) tenant=%s approval=%s",
            tenant_id, approval["id"],
        )
        return decision

    # VT-369: agent-surface approvals resolve through DURABLE STATE — the draft
    # batch was flipped (approved / edit_requested / rejected / cancelled) inside
    # the resolution transaction above. There is NO supervisor-graph checkpoint to
    # resume under this run_id (the agent dispatch workflow owns its own run
    # lifecycle and picks the batch status up on its next deterministic step), so
    # resume_run/close_webhook_run are campaign-path-only.
    if approval.get("approval_type") == "agent_customer_send":
        # VT-418 — the L2 owner-approve→send DRIVER arm seam. The approval-resolution
        # transaction ABOVE has committed the batch flip to 'approved' (approval_glue
        # apply_agent_decision). On an APPROVED resolution, start the durable send workflow
        # NOW that the flip is durable (start-after-commit — starting inside the resolve txn
        # would orphan a workflow on a rollback; mirrors the L3 arm's start-after-flip). Only
        # 'approved' drives a send; needs_changes/rejected/cancelled flip the batch to a
        # non-sendable state and the helper's 'status=approved' guard makes them safe no-ops.
        # The start is idempotent on the l2_send_{batch_id} workflow-id (a redelivered
        # owner-reply cannot spawn two drivers) and errors are swallowed (the reconciler sweep
        # is the recovery seam) — the owner-reply path must never fail on the arm.
        if decision == "approved":
            from orchestrator.agents.l2_send import (
                start_l2_send_for_resolved_approval,
            )

            start_l2_send_for_resolved_approval(str(tenant_id), str(approval["id"]))
        logger.info(
            "approval-resume: resolved (agent surface, durable-state) tenant=%s "
            "approval=%s decision=%s",
            tenant_id, approval["id"], decision,
        )
        return decision

    # VT-609 fix round 2 (CRITICAL): a business-policy proposal has NO supervisor-graph checkpoint
    # to resume either — ``propose_business_policy_grant`` opens only a MINIMAL ``pipeline_runs`` row
    # to satisfy the FK (mirroring ``business_impact_choke.dispatch_autonomy_offer``'s own
    # minimal-provenance-run pattern), never a paused LangGraph run. Without this branch the generic
    # fallback below would call ``resume_run`` against a thread_id with no checkpoint at all — a
    # guaranteed error, and the FIRST bug this specific approval_type would hit even after the grant
    # itself (applied inside ``mark_approval_resolved``'s transaction above, via ``_apply_agent_glue``
    # -> ``apply_business_policy_decision``) already landed. Close the minimal run so it doesn't sit
    # 'running' forever, then return — same durable-state shape as ``agent_customer_send`` above.
    if approval.get("approval_type") == "business_policy_grant":
        close_webhook_run(tenant_id, approval["run_id"], "completed")
        logger.info(
            "approval-resume: resolved (business_policy_grant, durable-state) tenant=%s "
            "approval=%s decision=%s",
            tenant_id, approval["id"], decision,
        )
        return decision

    # VT-633 F-1 — a MANAGER-LOOP-armed approval (its run was minted by workflow.py's
    # _dispatch_specialist_step: pipeline_runs.run_type='manager_dispatch') must NOT graph-resume.
    # The legacy resume re-enters request_owner_approval_node whose arm-idempotency guard only
    # matches an OPEN row — the just-RESOLVED row misses it, so the node re-armed a SECOND
    # approval + re-sent the summary/template (the duplicate-emission disease, observed live),
    # while the actual execution never ran. For a loop dispatch the LOOP is the single reactor:
    # its _approval_still_pending poll sees the resolution within seconds and its approved-branch
    # owns the campaign execution (F-2). Resolving the row above is this path's whole job.
    try:
        with tenant_connection(tenant_id) as conn:
            _rt_row = conn.execute(
                "SELECT run_type FROM pipeline_runs WHERE id = %s",
                (str(approval["run_id"]),),
            ).fetchone()
        _run_type = (
            (_rt_row.get("run_type") if isinstance(_rt_row, dict) else _rt_row[0])
            if _rt_row is not None else None
        )
    except Exception:  # noqa: BLE001 — fail-soft: an unreadable run_type takes the legacy path
        logger.warning(
            "approval-resume: run_type read failed (tenant=%s); assuming legacy path", tenant_id
        )
        _run_type = None
    if _run_type == "manager_dispatch":
        logger.info(
            "approval-resume: resolved loop-armed approval (tenant=%s approval=%s decision=%s) — "
            "no graph resume; the manager loop reacts",
            tenant_id, approval["id"], decision,
        )
        return decision

    # Resume the suspended graph (re-enters the interrupting node; the node's
    # arm_pause_request is a no-op now the row is resolved). Then close the
    # original paused run.
    paused_run_id = approval["run_id"]
    terminal_state = resume_run(paused_run_id, decision)

    # VT-562 — the loop must not end silently at "executed". On an APPROVED resolution the
    # resumed graph ran campaign_execute, whose terminal state carries campaign_execution_summary
    # (sent/skipped/failed/killed) — consumed by nothing before now. Report the honest outcome to
    # the owner (free-form, in-window: the owner just replied ⇒ inside the 24h window) BEFORE we
    # close. maybe_report_campaign_outcome no-ops when the resume did not execute a campaign
    # (rejected/needs_changes carry no summary) or a run-control HOLD produced no send, and is
    # FULLY FAIL-SOFT: the campaign already sent, so a report-send failure must never fail the
    # resume/close (it logs + fires the outbound_failure alert). Belt-and-braces try/except so
    # even an unexpected raise cannot strand the paused run un-closed.
    try:
        from orchestrator.owner_surface.campaign_outcome import (
            maybe_report_campaign_outcome,
        )

        maybe_report_campaign_outcome(tenant_id, terminal_state, run_id=paused_run_id)
    except Exception:  # noqa: BLE001 — the outcome report must never block the run close
        logger.exception(
            "approval-resume: outcome-report raised (fail-soft) tenant=%s run=%s",
            tenant_id, paused_run_id,
        )

    close_webhook_run(tenant_id, paused_run_id, "completed")

    logger.info(
        "approval-resume: resolved tenant=%s approval=%s run=%s decision=%s",
        tenant_id,
        approval["id"],
        paused_run_id,
        decision,
    )
    return decision


@DBOS.step()
def stamp_l3_delivery_anchor(tenant_id: str, message_sid: str) -> str | None:
    """VT-384 — the L3 delivery-anchor leg. A ``delivered`` status callback for an L3
    ``team_l3_presend_notice`` stamps the F6 anchor on the matching auto_send_pending batch and
    derives send_not_before = delivered_at + hold_hours (config). Idempotent + no-op for a
    callback that matches no auto_send_pending batch (C-d: a late callback after a demote does
    NOTHING — the stamp CAS only fires while the batch is still auto_send_pending). Returns the
    stamped batch_id or None. Checkpointed @DBOS.step so a redelivered callback re-runs safely."""
    from orchestrator.agents.l3_hold import stamp_delivery_anchor

    with tenant_connection(tenant_id) as conn:
        return stamp_delivery_anchor(tenant_id, message_sid, conn=conn)


@DBOS.step()
def demote_l3_on_owner_inbound(tenant_id: str) -> int:
    """VT-384 — the demote CAS leg (plan-ack §2). A substantive owner inbound (NOT a kill keyword —
    B2's freeze path cancels outright; opt-out/DSR ALSO freeze via their handlers but are no longer
    excluded from this non-cancelling demote — F1 belt-and-braces) while the tenant has an
    auto_send_pending L3 batch means "I want eyes on this": demote each such batch
    auto_send_pending → awaiting_approval + a regression record, atomically. The two-sided race
    guard: whichever side wins the row CAS, a hold-expiry send can NEVER fire over this in-flight
    objection (the wake-side re-check in agent_send_draft Gate 1 sees the demoted state). The C-c
    collision rule (an open approval already exists ⇒ QUEUE, never two open) lives in
    demote_auto_send_pending. Returns the number of batches demoted. Checkpointed for recovery."""
    from orchestrator.agents.l3_hold import demote_auto_send_pending

    with tenant_connection(tenant_id) as conn:
        results = demote_auto_send_pending(tenant_id, conn=conn, reason="owner_engaged")
    return sum(1 for r in results if r.demoted)


@DBOS.workflow()
def webhook_pipeline_run(tenant_id: str, run_id: str, twilio_fields: dict) -> dict[str, Any]:
    """Durable inbound-webhook pipeline: dedup -> ingress -> Pre-Filter -> handler.

    Started by /api/orchestrator/twilio-ingress with a workflow_id derived from
    the Twilio MessageSid (DBOS exactly-once idempotency). Dedup detection and
    event construction happen inside this durable boundary (C2 fix, CL-72).
    """
    message_sid = str(twilio_fields.get("MessageSid", ""))
    newly_inserted = record_inbound_message_sid(tenant_id, message_sid)
    event = build_webhook_event(twilio_fields, dupe_status=not newly_inserted)
    state = new_subscriber_state(UUID(tenant_id), UUID(run_id))
    # VT-416 PR-3 wiring — thread the tenant's language preference INTO state so
    # the output_composer's per-tenant resolver activates live (without this the
    # key is always absent → composer hits the global TENANT_DEFAULT_LANGUAGE
    # fallback for EVERY tenant, so a Hindi-preference owner silently got English).
    # Additive + best-effort: a read failure leaves the key absent and the
    # composer falls back to the global default — dispatch is never blocked.
    state["preferred_language"] = _load_preferred_language(tenant_id)

    # Phone-tokenise before anything is persisted (Pillar 3 / Pillar 7).
    # Body-key redaction lives at the persistence boundary inside
    # ``open_webhook_run`` / ``record_webhook_received`` — see
    # ``_redact_for_persistence`` above. The caller no longer pops body
    # so future call sites cannot leak by forgetting to pop; centralised
    # at the writer per VT-Privacy-Writer-Side. The in-memory ``event``
    # keeps body intact for request-scoped readers (pre_filter, the
    # owner_inputs extraction writer when its SHIP GATE clears).
    tokenised = event.model_dump()
    if event.sender_phone:
        tokenised["sender_phone"] = hash_phone(event.sender_phone)

    open_webhook_run(tenant_id, run_id, tokenised)
    record_webhook_received(tenant_id, run_id, tokenised)

    # VT-583 D2 — record the owner's inbound to the lifetime conversation_log EARLY (before any gate can
    # consume-and-close), so a message consumed by the approval / journey / integration-resume gate is
    # never lost from the manager's lifetime log (the live-run-23 silent-drop class). Idempotent per
    # message_sid; fail-soft; inbound-only. The later brain-path record + journey's own mirror dedup onto
    # this row. Placed after the run row exists so the tenant-scoped write has its RLS context.
    _record_owner_inbound_turn(tenant_id, event)

    # VT-524 (B1) — owner-notification delivery ledger. Persist the async delivery truth
    # (delivered/failed) against the owner send, keyed by the outbound message_sid, for EVERY
    # status-callback state — runs BEFORE pre_filter (which Rejects 'delivered' as
    # observability-only) so the delivery result is never lost. Fail-soft; a no-op when no owner
    # send matches the sid.
    if event.message_type == "status_callback" and event.twilio_message_sid:
        from orchestrator.owner_surface.owner_notification import (
            record_owner_notification_delivery,
        )

        record_owner_notification_delivery(
            tenant_id, event.twilio_message_sid, event.status_callback_state
        )

    # VT-384 — the L3 delivery-anchor leg. A 'delivered' status callback for the owner's
    # team_l3_presend_notice stamps the F6 anchor on its auto_send_pending batch and starts the
    # hold clock (send_not_before = delivered_at + hold_hours). Runs BEFORE pre_filter (which
    # Rejects delivered callbacks as observability-only) so the anchor is never lost. The stamp is
    # a no-op for any non-L3 delivered callback (no matching auto_send_pending batch). The status
    # callback's 'From' is the owner phone, so the ingress already resolved this run to the right
    # tenant. After stamping THIS run ends clean — a delivered callback is not a routed message.
    if (
        event.message_type == "status_callback"
        and event.status_callback_state == "delivered"
        and event.twilio_message_sid
    ):
        stamped_batch = stamp_l3_delivery_anchor(tenant_id, event.twilio_message_sid)
        if stamped_batch is not None:
            close_webhook_run(tenant_id, run_id, "completed")
            return {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "routed": "l3_delivery_anchor",
                "handler": None,
                "batch_id": stamped_batch,
            }

    # VT-146 — owner-input extraction seam. Reads body from the
    # request-scoped ``event`` (NOT from any persisted column; VT-144
    # stripped raw body from trigger_payload / input_envelope), routes
    # it to the classifier in ``orchestrator.owner_inputs`` (which owns
    # the LLM seam — Pillar 1 keeps runner.py deterministic; the LLM
    # call lives behind the writer's boundary), persists only the
    # derived intent / segment / occasion row to ``owner_inputs``.
    # ``run_extraction_for_event`` is best-effort internally —
    # classifier or write failure logs and returns None; the inbound
    # pipeline never breaks. No body leaves this function via
    # persistence; the body text crosses the wire to the classifier
    # only.
    #
    # Gated by ``OWNER_INPUTS_EXTRACTION_ENABLED`` (module-level
    # constant) — stays False until the vendor DPA + ZDR + the
    # privacy notice clear. See the constant's comment above.
    if OWNER_INPUTS_EXTRACTION_ENABLED:
        run_extraction_for_event(UUID(tenant_id), UUID(run_id), event)

    # VT-47 — owner-approval RESUME gate. If this tenant has a run PAUSED on
    # an owner-approval interrupt, an inbound owner message is the approval
    # decision: classify it (VT-49), resolve the pending_approvals row, and
    # resume the paused run via Command(resume=...). Status callbacks are not
    # decisions, so only inbound_message events are considered. When consumed,
    # THIS inbound run ends cleanly (the work was the resume); we do not also
    # route it through pre_filter/dispatch (that would double-handle the reply).
    if event.message_type == "inbound_message" and not event.dupe_status:
        resumed_decision = try_resume_pending_approval(
            tenant_id, event.body or "", event.twilio_message_sid
        )
        if resumed_decision is None and _should_wait_for_approval_arm(
            tenant_id, event.body or ""
        ):
            # VT-633 D-A — the owner's CLEAR decision can PRECEDE the manager loop's approval
            # arm by seconds (live canary: reply at :30, arm at :47 — a 17s gap; the loop's
            # spawn → SR → collapse → arm chain runs ~1 min behind the draft ask). Dropping the
            # reply lost the decision forever: the armed approval then waited its full window
            # with nobody left to resolve it — a dropped money action. Give the arm a bounded
            # head-start and re-run the resume once it lands. Ambiguous replies and no-task
            # turns never wait (the gate step above); the opt-out/DSR guard runs FIRST inside
            # try_resume_pending_approval, and it re-runs on the post-arm attempt, so a STOP is
            # never consumed as a decision here. DBOS-replay: gate + poll condition are both
            # @DBOS.step (memoized) — replay re-walks the identical sleep count (the VT-623 B1
            # pattern; see _brain_emitted_owner_reply_step's note).
            _polls = 0
            while _polls < _APPROVAL_ARM_WAIT_MAX_POLLS:
                if _open_approval_exists_step(tenant_id):
                    resumed_decision = try_resume_pending_approval(
                        tenant_id, event.body or "", event.twilio_message_sid
                    )
                    break
                DBOS.sleep(_APPROVAL_ARM_WAIT_POLL_S)
                _polls += 1
        if resumed_decision is not None:
            close_webhook_run(tenant_id, run_id, "completed")
            return {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "routed": "approval_resume",
                "handler": None,
                "decision": resumed_decision,
            }
        # T8 — the reply was NOT a resolvable decision (T5 vague-resume -> None), but if it is a
        # RESUME cue and an approval is already armed, re-surface THAT approval and consume the turn
        # here, BEFORE the journey/dispatch gates below spawn a competing plan. Complements T5:
        # refuse to auto-send on a vague resume (there), advance by re-pointing at the pending plan
        # (here). Non-resume / opt-out replies return False and keep their normal path.
        if _maybe_resurface_pending_approval(tenant_id, event):
            close_webhook_run(tenant_id, run_id, "completed")
            return {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "routed": "approval_resurfaced",
                "handler": None,
            }

        # R1 (full-77 cluster-1, sr_consequential_bulk_send + sr_always_confirm_first_contact_floor) —
        # the reply was NOT a resolvable decision (T5 -> None) and NOT a vague RESUME cue (T8 declined),
        # but if it is a SEND PUSH ("jaldi karo, sabko bhej do" / a long "seedha bhej do" / a bare weak
        # "theek hai") AND a customer-send approval is already armed, RE-CONFIRM that approval and consume
        # the turn HERE — before the journey/dispatch gates spawn a competing plan or the brain reads its
        # own turn as approval-taken. Only SPEAKS (never sends); the plan stays ARMED; opt-out/DSR and the
        # customer-send type are guarded inside. Ordered AFTER the T8 re-surface so a vague resume keeps
        # its own copy, and only reachable when try_resume left the approval unresolved.
        if _maybe_reconfirm_send_push(tenant_id, event):
            close_webhook_run(tenant_id, run_id, "completed")
            return {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "routed": "approval_reconfirm",
                "handler": None,
            }

    # R5 / CD6 (CL-2026-07-12-global-stop-is-optout-reconsent-to-resume) — the opted-out RESUME leg.
    # An OPTED-OUT tenant's explicit restart cue ("START" / "restart karo" / "resume" — the template's
    # promised "reply START") clears opt_out via the SOLE audited clearer (data_inputs_enable_handler,
    # symmetric to opt_out_handler) and consumes the turn. Ordered AFTER the opt-out/DSR guard (a STOP /
    # "delete my data" from an opted-out tenant is NOT a resume — matches_opt_out_or_dsr excludes it, so
    # a repeat STOP stays opted-out / a DSR still routes to dsr_handler) and BEFORE the journey/connector
    # gates so the restart is not eaten downstream. This is the ONLY consent-gate relaxation in the
    # batch and stays exactly bounded: it clears opt_out ONLY through the existing audited handler, ONLY
    # on the enumerated restart cues (matches_restart_cue — do NOT widen), and — critically — clearing
    # opt_out here does NOT auto-resume any armed approval or queued send: no armed row is touched, and
    # the send chokepoint (execute_approved_campaign, T13b) re-reads opt_out, so a pending campaign still
    # needs the normal approval path afterward. FAIL-OPEN: any error falls through to the normal pipeline.
    if event.message_type == "inbound_message" and not event.dupe_status:
        from orchestrator.pre_filter_gate import matches_opt_out_or_dsr, matches_restart_cue

        _resume_body = event.body or ""
        if (
            matches_restart_cue(_resume_body)
            and not matches_opt_out_or_dsr(_resume_body)
            and _tenant_is_opted_out_step(tenant_id)
        ):
            HANDLERS["data_inputs_enable_handler"](event, state)
            close_webhook_run(tenant_id, run_id, "completed")
            logger.info("CD6: opted-out tenant re-consented via restart cue (tenant=%s)", tenant_id)
            return {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "routed": "optout_resume_reconsent",
                "handler": "data_inputs_enable_handler",
            }

    # VT-367 — onboarding-JOURNEY gate. While an onboarding journey is active (or a fresh tenant's
    # FIRST inbound, which lazy-starts it so the first message never reaches the cold brain), an
    # inbound owner message routes to the journey handler BEFORE pre_filter/dispatch. FAIL-OPEN:
    # maybe_handle_journey_reply swallows any error + returns None → the normal pipeline runs (owner
    # inbound is never blocked by a journey-check failure). Only inbound, non-dupe (idempotency is
    # double-guarded: the VT-149 message_sid UNIQUE seam above + handle_reply's last_message_sid).
    # Lazily imported so non-journey paths don't pay the import cost.
    #
    # VT-609 (Loop Package 4) had this gate DISABLED in enforce mode, on the design assumption that
    # the Manager brain reliably spawns the onboarding_conductor specialist for onboarding turns.
    # T14 (§2 judge, onboarding_privacy_skeptic x3, 2026-07-11) MEASURED that assumption false: on an
    # active-journey tenant the brain spawned the conductor 0/4 turns — the "Complete Setup" kickoff,
    # the owner's volunteered profile fields, and the "are we set up now?" status ask all completed
    # SILENT (D1 "I'm on it"), the fields were never recorded, and the judge scores it
    # ignored_speech_act + loop_stall. Same LLM-gated-handoff failure class as VT-623/VT-626.
    # So the deterministic journey gate now runs in EVERY mode, with a VT-608-style DEFER: when the
    # loop genuinely owns the tenant's onboarding (an active manager_task whose CURRENT step targets
    # onboarding_conductor), the gate skips — no dual-writer race on the journey state. In legacy/
    # shadow a conductor-owned CURRENT step is effectively unreachable (this gate consumes journey
    # turns before the brain can spawn one), so behavior there is unchanged in practice — and where
    # one somehow exists, deferring is equally correct. This deliberately reverses VT-609 Package
    # 4's enforce-bypass acceptance
    # (test_runner_onboarding_mode_gate.py updated in the same commit) — the conductor remains the
    # enforce-mode owner WHEN SPAWNED; the gate is the deterministic floor for when it is not.
    #
    # ENFORCE runs the NARROW speech-act-aware gate (enforce_journey_gate: kickoff button +
    # in-flight answers + an honest setup-status line; QUESTIONS fall through to the brain), NOT
    # the full walker — running the raw walker in enforce was measured WORSE (dcc402f, x3: the
    # script ignores a privacy question and the post-profile flow fabricates a platform
    # assumption; 3/4/3 breakers vs the bypass's 1/2/2). Legacy/shadow keep the full walker
    # byte-identical.
    journey_loop_owns_turn = False
    if event.message_type == "inbound_message" and not event.dupe_status:
        try:
            from orchestrator.manager.task_store import has_active_onboarding_conductor_step

            journey_loop_owns_turn = has_active_onboarding_conductor_step(tenant_id)
        except Exception:  # noqa: BLE001 — defer-check failure must never block the journey gate
            logger.warning(
                "T14: has_active_onboarding_conductor_step check failed tenant=%s "
                "(fail-open -> journey gate runs)",
                tenant_id,
            )

    if (
        event.message_type == "inbound_message"
        and not event.dupe_status
        and not journey_loop_owns_turn
    ):
        from orchestrator.manager.loop_mode import is_enforce

        if is_enforce():
            from orchestrator.onboarding.enforce_journey_gate import (
                maybe_handle_enforce_journey_turn,
            )

            journey_result = maybe_handle_enforce_journey_turn(
                tenant_id, event.body or "", event.twilio_message_sid, event.sender_phone
            )
        else:
            from orchestrator.onboarding.journey import maybe_handle_journey_reply

            journey_result = maybe_handle_journey_reply(
                tenant_id, event.body or "", event.twilio_message_sid, event.sender_phone
            )
        if journey_result is not None:
            close_webhook_run(tenant_id, run_id, "completed")
            return {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "routed": "onboarding_journey",
                "handler": None,
                "journey_done": journey_result.get("done"),
            }

    # VT-425 — the integration-onboarding RESUME gate (closes the VT-267 chat-resume gap).
    # After a link-out, an inbound WhatsApp message must RESUME the connector onboarding in chat
    # (re-check the connector status, advance the phase) — NOT re-enter the brain fresh. Each
    # inbound gets a distinct thread_id, so the LangGraph checkpointer carries nothing; resume is
    # DB-state driven off tenant_integration_state.pending_owner_input. Mirrors the journey gate:
    # runs BEFORE pre_filter, inbound + non-dupe only, FAIL-OPEN (any error → None → normal flow).
    # Opt-out / DSR is short-circuited inside the gate (returns None) so it never consumes a STOP.
    #
    # VT-608 ruling 1 — the DEFER check: in enforce mode the loop owns a tenant's integration
    # objective once dispatched (its specialist reads/writes the SAME tenant_integration_state
    # truth this legacy gate does). An active loop task currently ON an integration_agent step
    # means the loop already owns this turn — the gate DEFERS (skips entirely, falls through to
    # the normal brain/loop dispatch path) rather than racing the loop for the same phase-state
    # writes. No active loop-owned integration step (the common case today, and the ONLY case
    # in legacy/shadow mode) → gate behavior is BYTE-IDENTICAL to before this ruling. Fail-open:
    # a defer-check failure must never block the legacy gate's own resume (falls through to it,
    # not the reverse — this is a NEW check layered in front of an EXISTING fail-open gate).
    integration_loop_owns_turn = False
    if event.message_type == "inbound_message" and not event.dupe_status:
        try:
            from orchestrator.manager.task_store import has_active_integration_step

            integration_loop_owns_turn = has_active_integration_step(tenant_id)
        except Exception:  # noqa: BLE001 — defer-check failure must never block the legacy gate
            logger.warning(
                "VT-608: has_active_integration_step check failed tenant=%s (fail-open -> legacy gate runs)",
                tenant_id,
            )

    if (
        event.message_type == "inbound_message"
        and not event.dupe_status
        and not integration_loop_owns_turn
    ):
        # VT-608 fix round CRITICAL 1 — route on the tenant's actual connector (tenant_
        # integration_state has ONE row per tenant; a Sheets-flow tenant must never be
        # intercepted by the Shopify-only hook). See onboarding.connector_resume's own docstring.
        from orchestrator.onboarding.connector_resume import maybe_resume_connector_onboarding

        resume_result = maybe_resume_connector_onboarding(
            tenant_id, event.body or "", event.twilio_message_sid, event.sender_phone
        )
        if resume_result is not None:
            close_webhook_run(tenant_id, run_id, "completed")
            return {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "routed": "integration_onboarding_resume",
                "handler": None,
                "onboarding_done": resume_result.get("done"),
            }

        # VT-626 — deterministic FIRST-CONTACT connect route. The resume gate above only fires on an
        # EXISTING connector state; a first "connect my Sheet/Shopify" ask had no deterministic net and
        # relied on the LLM emitting spawn_integration (intermittent D1 stall / fake handoff — same
        # LLM-gated-handoff class as VT-623). This mints the OAuth link-out (sheets) / kicks off discovery
        # (shopify) deterministically. Shares this block's inbound + non-dupe + not-loop-owned guard;
        # FAIL-OPEN inside. Runs AFTER resume (a live flow is handled above) and BEFORE the brain.
        from orchestrator.onboarding.connector_first_contact import (
            maybe_start_connector_onboarding,
        )

        first_contact_result = maybe_start_connector_onboarding(
            tenant_id, event.body or "", event.twilio_message_sid, event.sender_phone
        )
        if first_contact_result is not None:
            close_webhook_run(tenant_id, run_id, "completed")
            return {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "routed": "integration_first_contact",
                "handler": None,
                "onboarding_done": first_contact_result.get("done"),
            }

        # D2 (Fazal 2026-07-12 #2) — honest capability-disclosure for an UNSUPPORTED paid ad-boost.
        # No paid-ad executor exists (roster = 3 specialists; marketing is advisory), so a paid-boost
        # ask is DISCLOSED here — BEFORE the brain/triage/spend path — never routed into the spend gate
        # (arming an approval for the unexecutable = impossible-promise). Money-safe: this net only
        # speaks (no send/effect). Reply goes via the Step-1 replay-safe owner-reply step. Shares this
        # block's inbound + non-dupe + not-loop-owned guard. FAIL-OPEN -> normal path.
        try:
            from orchestrator.onboarding.capability_disclosure import (
                compose_capability_disclosure,
                detect_unsupported_action,
            )

            if detect_unsupported_action(event.body or ""):
                from orchestrator.owner_surface.freeform_acks import resolve_owner_locale

                _cap_locale = resolve_owner_locale(tenant_id)
                _send_owner_reply_step(
                    tenant_id,
                    event.sender_phone,
                    compose_capability_disclosure(locale=_cap_locale),
                )
                close_webhook_run(tenant_id, run_id, "completed")
                return {
                    "run_id": run_id,
                    "tenant_id": tenant_id,
                    "routed": "capability_disclosure_unsupported_boost",
                    "handler": None,
                }
        except Exception:  # noqa: BLE001 — the D2 net must never block the turn (fail-open)
            logger.warning(
                "D2 capability-disclosure net failed tenant=%s (fail-open -> normal path)",
                tenant_id,
                exc_info=True,
            )

    # VT-384 — the demote CAS leg (plan-ack §2). A substantive owner inbound during an L3 hold
    # demotes the auto_send_pending batch to awaiting_approval (the owner wants eyes on it; the
    # batch re-enters the normal approval path — nothing is lost). The demote runs BEFORE pre_filter
    # so a window-expiry send can never fire over the objection (two-sided race). FAIL-OPEN: a
    # demote-check failure must never block owner inbound — wrapped best-effort.
    #
    # VT-384 gate-bounce F1 (BELT-AND-BRACES): opt-out / DSR are NO LONGER excluded here. Those
    # phrasings ("stop automatic sending" / "auto band karo") now route to opt_out_handler/dsr_handler,
    # which invoke the FREEZE path (cancel holds outright — strictly stronger than this demote). The
    # demote is NON-CANCELLING (flip → awaiting_approval, no kill), so stacking it under that freeze is
    # safe: whichever lands first, no send fires over the owner's objection. Only the kill keyword stays
    # excluded — it FREEZES via autonomy_kill_handler (cancel outright), so a demote+regress here would
    # be redundant with the cancel and could fight the freeze's batch-cancel.
    if event.message_type == "inbound_message" and not event.dupe_status:
        from orchestrator.pre_filter_gate import matches_kill_keyword

        body = event.body or ""
        if not matches_kill_keyword(body):
            try:
                demote_l3_on_owner_inbound(tenant_id)
            except Exception:  # noqa: BLE001 — a demote-check failure must never block owner inbound
                logger.exception(
                    "VT-384: L3 demote-on-owner-inbound failed (tenant=%s run=%s)",
                    tenant_id, run_id,
                )

    result = pre_filter(event, state)
    handler_name: str | None = None
    # VT-356: `routed` is a local observability label (logged/returned), not the route-decision
    # type — widen to str so the VT-303 'consent_required' branch (below) is assignable.
    routed: str = result.kind
    final_status = "completed"
    if result.kind == "direct_handler":
        # VT-384 — the autonomy_kill_handler / autonomy_enable_handler that pre_filter rules b2/b3
        # route to are registered in orchestrator.direct_handlers.HANDLERS (alongside the original
        # 7). This dispatch line routes any registered name; an unregistered route would KeyError
        # here, which the test_vt384_handler_dispatch_realdb registration-pin guards against.
        handler_name = result.handler_name
        HANDLERS[handler_name](event, state)
    elif result.kind == "brain":
        # VT-579 — LIFETIME conversation log (inbound leg). pre_filter routed this owner message to the
        # Team-Manager (real conversation), so record it as an 'owner' turn — the manager's always-on
        # window (dispatch._build_manager_conversation_block) + the lifetime search both read this table.
        # Placed HERE (post tenant-resolve + prefilter=brain, BEFORE the consent gate below) because this
        # is STORAGE of the tenant's OWN message in a tenant-scoped RLS'd table, NOT a transmit to
        # Anthropic — the consent gate governs the transmit, and the window is only READ inside
        # dispatch_brain, which that same gate still guards. Inbound-message only (a status callback
        # carries no body); idempotent per message_sid. Then fire the off-hot-path compaction guard. Both
        # fail-soft internally — conversation memory never blocks the run. NOT reached for opt-out/DSR
        # (those route to direct_handler above), nor for status callbacks / dupes.
        if event.message_type == "inbound_message":
            from orchestrator.conversation_log import maybe_compact, record_turn

            record_turn(
                tenant_id,
                "owner",
                event.body or "",
                message_sid=event.twilio_message_sid,
                surface="manager",
            )
            maybe_compact(tenant_id)
        # VT-303 / CL-425 — owner_inputs consent gate on the brain transmit
        # (Option B). The brain transmits the owner's inbound body (may carry
        # customer PII) to Anthropic; owner_inputs is the lawful basis. Scope
        # the gate to real inbound messages — status-callback brain routes carry
        # no body, so there is nothing to gate. Fail-closed: FALSE/unknown →
        # NO transmit; send the conservative enable-prompt instead. The owner
        # turns it on via the enable keyword (data_inputs_enable_handler).
        if event.message_type == "inbound_message" and not _brain_owner_inputs_ok(tenant_id):
            # VT-583 (CL-2026-07-03-fluid-consent) — fluid consent GRANT. The exact "ACTIVATE TEAM" floor
            # (pre_filter Rule a2) still wins first + unchanged; here, an owner who replies to the consent
            # ASK with a plain affirmation ("yes" / "haan" / "start") routes to the SAME audited enable
            # path (data_inputs_enable_handler → owner_inputs=true + confirm). Gated hard: BOTH a
            # consent_ask must be the last thing we sent AND a deterministic (zero-LLM — the consent
            # boundary forbids a brain transmit here) affirm. Anything else → the honest re-ask. Additive
            # + fail-safe: any uncertainty falls to consent_required (never an auto-grant on a guess).
            if _consent_affirm_after_ask(tenant_id, event.body or ""):
                handler_name = "data_inputs_enable_handler"
                routed = "consent_granted_by_intent"
                HANDLERS[handler_name](event, state)
            elif _journey_represent_instead_of_consent_ask(tenant_id, event):
                # VT-693 — during an ACTIVE onboarding journey, a non-answer owner message must
                # stay in the journey's conversation: the measured non-sequitur was the ACTIVATE
                # consent pitch interjecting between profile questions ("Can you fetch my details
                # from online sources?" → a consent pitch). The journey's current beat re-presents
                # instead; the data-inputs ask returns AFTER the journey settles.
                handler_name = None
                routed = "journey_represent_over_consent_ask"
            else:
                handler_name = "consent_required_handler"
                routed = "consent_required"
                HANDLERS[handler_name](event, state)
        else:
            # VT-374 N3/B2 — the webhook-path run-control boundary: PAUSE-ONLY (no
            # override ever matches dispatch_brain — registry allowed_keys=∅). Durable
            # BOUNDED hold: each control read is its own @DBOS.step and the wait between
            # polls is DBOS.sleep, both checkpointed, so a paused run survives a worker
            # restart and resumes the hold (plan §10.2); paused_ms counts poll intervals
            # (deterministic under DBOS replay), not wall-clock. The direct-handler
            # branch above (opt-out / DSR) is pause-EXEMPT by construction (I6).
            # Concurrently-held runs release with NO ordering guarantee (N3).
            #
            # B2 max-hold: past _RUN_CONTROL_MAX_HOLD_S the run closes status='paused'
            # (terminal_state_metadata.paused_by_run_control) and returns WITHOUT
            # dispatching the brain — no worker parks forever. The message IS recorded
            # (sid ledger + run row + webhook_received step above); releasing the pause
            # means FUTURE messages flow — this run does not auto-resume. Panel copy
            # obligation (Phase B): surface parked runs with exactly that wording.
            paused_ms = 0
            max_hold_exceeded = False
            while read_webhook_pause(tenant_id):
                if paused_ms >= int(_RUN_CONTROL_MAX_HOLD_S * 1000):
                    max_hold_exceeded = True
                    break
                DBOS.sleep(_RUN_CONTROL_POLL_S)
                paused_ms += int(_RUN_CONTROL_POLL_S * 1000)
            from orchestrator.observability.pipeline_observability import (
                record_intervention,
            )

            if max_hold_exceeded:
                # B1: 'held' timeline row BEFORE the close (write_step needs the
                # run row; record_intervention never raises).
                record_intervention(
                    tenant_id,
                    run_id,
                    workflow_kind="webhook_inbound",
                    step_name="dispatch_brain",
                    paused_ms=paused_ms,
                    action="held",
                )
                close_webhook_run_paused(tenant_id, run_id)
                return {
                    "run_id": run_id,
                    "tenant_id": tenant_id,
                    "routed": "run_control_max_hold",
                    "handler": None,
                }
            if paused_ms:
                # B1: a released hold lands on the run's timeline with the mig-131
                # paused_ms column set (the dead-columns fix).
                record_intervention(
                    tenant_id,
                    run_id,
                    workflow_kind="webhook_inbound",
                    step_name="dispatch_brain",
                    paused_ms=paused_ms,
                    action="released",
                )
            # VT-606 (team-lead ruling round 2) — the triage seam, mode-gated at the read site.
            # legacy: get_loop_mode() reads the env var and returns immediately — ZERO new LLM/DB
            # calls, the hot path below is BYTE-IDENTICAL to pre-VT-606. shadow: triage classifies
            # observationally (a new_task creates an inert plan row; the dispatch_brain call below
            # STILL runs unconditionally — shadow never owns a reply/effect). enforce: triage may
            # itself own this turn's routing (new_task/answer_pending), in which case
            # skip_legacy_dispatch tells us to skip dispatch_brain below — untested-live until
            # VT-611 gates enforce on anywhere. Scoped to real inbound messages only (a status
            # callback carries no body — nothing to triage), mirroring the record_turn guard above.
            skip_legacy_dispatch = False
            triage_outcome: str | None = None
            if event.message_type == "inbound_message":
                from orchestrator.manager.triage_seam import triage_seam

                seam_result = triage_seam(
                    UUID(tenant_id), event.body or "", event.twilio_message_sid or run_id,
                )
                skip_legacy_dispatch = seam_result.skip_legacy_dispatch
                # T9 — thread the triage outcome so dispatch_brain suppresses async specialist
                # spawns on an answerable turn (direct_reply / task_status) and answers in-turn.
                triage_outcome = seam_result.outcome
                # Shared infra (D3/cluster-5b) — a deterministic in-turn reply from the enforce seam
                # is delivered via the ONE canonical checkpointed step (replay-safe; at-most-once).
                # Placed BEFORE the D1 in-turn wait + fallback: recording the 'assistant' turn makes
                # _brain_emitted_owner_reply see a reply, so neither double-sends.
                if seam_result.direct_reply_text is not None:
                    _send_owner_reply_step(
                        tenant_id, event.sender_phone, seam_result.direct_reply_text
                    )

            # VT-193: brain wired into supervisor graph via dispatch_brain.
            # Replaces the VT-3.4 placeholder (record_brain_pending + 'escalated'
            # final status) that the 2026-05-27 E2E surfaced. Imported lazily
            # so non-brain webhook paths don't pay the langchain/langgraph
            # import cost.
            #
            # VT-606: skip_legacy_dispatch (enforce mode only) means the triage seam already owns
            # this turn's routing (new_task/answer_pending) — dispatch_brain is NOT a brain dispatch
            # for this turn, so record_dispatch_terminal_episodic/audit_run_isolation (both scoped
            # to "brain path only") are correctly skipped too; final_status stays at its "completed"
            # default (set above) so close_webhook_run + the VT-88 fallback below still run exactly
            # as they would for any other clean turn — no dangling run, no skipped cleanup.
            if not skip_legacy_dispatch:
                from orchestrator.agent.dispatch import dispatch_brain

                dispatch_result = dispatch_brain(
                    event=event,
                    state=state,
                    run_id=UUID(run_id),
                    tenant_id=UUID(tenant_id),
                    triage_outcome=triage_outcome,
                )
                final_status = dispatch_result.final_status
                # VT-309: L2 agent-dispatch lifecycle event (completed/terminated).
                # Brain path only — direct-handler/reject/consent runs are not agent
                # dispatches. Skips 'paused' (resolves later on resume).
                record_dispatch_terminal_episodic(
                    tenant_id, run_id, final_status, dispatch_result.terminal_path
                )
                # VT-73 POST-FLIGHT isolation audit: service-role scan of this run's
                # pipeline_steps — assert no step was logged under another tenant
                # (catches a leak that escaped pre/in-flight). Best-effort detect+alert.
                from orchestrator.context_validator import audit_run_isolation

                audit_run_isolation(UUID(run_id), UUID(tenant_id))

                # VT-608 RULING 3 — the deterministic ingestion-commit executor. The integration
                # agent's own commit_ingestion TOOL never writes the customer/ledger substrate
                # (VT-268); this is the non-agent, server-side code path that actually performs it,
                # mirroring the campaign effect rail's propose-then-execute shape. A cheap no-op
                # for every tenant/turn EXCEPT one whose just-dispatched turn left
                # tenant_integration_state at 'ingestion_commit_pending' (this legacy/shadow
                # dispatch path is the ONLY place a Sheets/Shopify commit proposed via the agent's
                # tool surface — as opposed to the Shopify-specific deterministic resume hook above,
                # which calls pull_and_ingest_shopify directly and never goes through this — gets
                # executed today; enforce mode's own hook lives in
                # manager.workflow._dispatch_specialist_step). Fail-soft: an executor failure must
                # never crash the webhook run; it leaves the phase at ingestion_commit_pending
                # (observable via verify_connector) rather than fabricating success.
                try:
                    from orchestrator.integrations.commit import execute_pending_ingestion_commit

                    # VT-608 fix round MAJOR 1 — this webhook run's own run_id is the SAME value
                    # dispatch_brain's observability_context set as ctx.run_id, so it matches
                    # whatever commit_ingestion armed the proposal with THIS turn (never a stale
                    # proposal from an earlier, unrelated turn).
                    execute_pending_ingestion_commit(tenant_id, current_turn_id=run_id)
                except Exception:  # noqa: BLE001 — never block the webhook run's own close
                    logger.exception(
                        "VT-608: execute_pending_ingestion_commit failed tenant=%s run=%s",
                        tenant_id, run_id,
                    )

            # VT-623 Head3 (D1 in-turn wait): if THIS turn started/resumed an async manager_task
            # (skip_legacy_dispatch), that workflow owns the owner reply — give it a bounded head-start
            # to land IN-TURN so the D1 check below suppresses the redundant "I'm on it" when the real
            # reply arrives. The poll condition is the CHECKPOINTED _brain_emitted_owner_reply_step (NOT
            # the plain read) so a mid-turn worker restart replays the identical sleep count — see that
            # step's note. Fail-soft: it returns True on a read error, ending the wait early (assume
            # replied — never risk a double-send).
            if (
                skip_legacy_dispatch
                and final_status == "completed"
                and event.message_type == "inbound_message"
            ):
                _polls = 0
                while _polls < _D1_INTURN_WAIT_MAX_POLLS:
                    if _brain_emitted_owner_reply_step(tenant_id, event.twilio_message_sid):
                        break
                    DBOS.sleep(_D1_INTURN_WAIT_POLL_S)
                    _polls += 1

            # VT-583 D1 (THE biggest silent-drop): a brain run that COMPLETED but produced NO owner-facing
            # send left the owner in silence. Detect it (no assistant turn in the lifetime log at/after
            # this inbound) and send ONE honest, substance-railed acknowledgement through the in-session
            # manager path. ONLY for a 'completed' inbound run — never for a status callback (not this
            # branch), a reject (observability-only, dispatch didn't run), an escalation / hard-limit
            # (final_status != completed; VT-88 support_bot already acks those), or a paused-by-design run
            # (final_status == paused). Fail-soft throughout. Also covers the enforce/skip_legacy_dispatch
            # case (VT-606) — the triage seam's own routing doesn't necessarily reply either, so the
            # owner must not be left in silence there either.
            if (
                final_status == "completed"
                and event.message_type == "inbound_message"
                and not _brain_emitted_owner_reply(tenant_id, event.twilio_message_sid)
            ):
                _send_completed_no_reply_fallback(tenant_id, event)
    # result.kind == "reject" → observability-only; the run ends clean (completed).

    close_webhook_run(tenant_id, run_id, final_status)

    # VT-88 SupportBot: on an UNRESOLVED terminal the owner must get SOMETHING (not silence)
    # — an ack; the 2nd+ unresolved run in 24h also escalates to Fazal. Runs AFTER the status
    # is persisted (the deterministic counter includes this run). Best-effort — the fallback
    # must never break the durable run.
    try:
        from orchestrator.owner_surface.support_bot import maybe_escalate_support

        maybe_escalate_support(
            tenant_id=tenant_id, run_id=run_id, event=event, final_status=final_status
        )
    except Exception:  # noqa: BLE001 — the fallback must never break the workflow
        logger.exception(
            "VT-88 support escalation hook failed (tenant=%s run=%s)", tenant_id, run_id
        )

    # VT-683 P2b — post-turn owner-comms drain: the owner's turn is fully settled (status
    # persisted, support hook done); deliver at most ONE queued comms item into the open
    # session. AFTER close_webhook_run like the VT-88 hook (never blocks the durable run) and
    # checkpointed inside the step (at-most-once across replay).
    _post_turn_drain_step(tenant_id, event.sender_phone or None)

    return {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "routed": routed,
        "handler": handler_name,
    }
