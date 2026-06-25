"""VT-90 / VT-365 — trial-lifecycle daily sweep (the action side of trial_evaluator).

One off-peak daily sweep: for each active, un-subscribed trial tenant,
evaluate_trial → act:
  - expire → apply_transition('trial_expired') (phase trial → dormant `lapsed`) +
             fire the VT-359 trial-end subscribe-link nudge ONCE (the owner can
             still subscribe from `lapsed`).
  - warn   → notify `trial_ending` (day trial_end - warn_lead). No phase change.

VT-365 (Fazal 2026-06-09): NO trial extensions, no money clawback. A trial that
elapses without an explicit owner `subscribe` simply EXPIRES to `lapsed`
(dormant, re-subscribable). The old extend/exhaust + clawback paths are removed.

NO LLM (Pillar 1). The owner notify is an INJECTABLE seam — the real owner-WABA
send is gate-live (NEEDS-FAZAL: provision the Meta SIDs); the default stub logs.
Idempotent: expire → `lapsed` (re-scan skips it — scope is phase='trial' only).
apply_transition is the SOLE phase mutator.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

logger = logging.getLogger(__name__)

# Owner notify seam: (tenant_id, template_name, language, params) -> None.
NotifyFn = Callable[[UUID, str, str, dict[str, Any]], None]


def _default_notify(
    tenant_id: UUID, template_name: str, language: str, params: dict[str, Any]
) -> None:
    """STUB owner notify (owner-WABA gate-live, NEEDS-FAZAL SIDs). Logs intent."""
    logger.info(
        "trial_sweep: notify queued tenant=%s template=%s lang=%s (owner-WABA gate-live)",
        tenant_id, template_name, language,
    )


def _owner_notify(
    tenant_id: UUID,
    template_name: str,
    language: str,
    params: dict[str, Any],
    *,
    send_fn: Callable[..., Any] | None = None,
) -> None:
    """VT-426 (Row C, hardened) — the REAL trial-ending owner-WhatsApp notify seam.

    Resolves the template **by NAME** from the registry (``twilio_templates.yaml``,
    the runtime mirror of ``.viabe/templates.md``) and sends it to the owner via the
    VT-393 owner-utility seam (``owner_surface.send_owner_template``). NO hard-coded
    SID — the SID is looked up by ``(template_name, language)`` (CL template-registry
    rule).

    FAIL-CLOSED by default (VT-426 hardening). Three INDEPENDENT gates each cause a
    loud-log, 0-send, no-crash SKIP — the seam sends ONLY when ALL pass:

      GATE 1 (``approved_for_live``, the PRIMARY gate): the resolved registry Entry
        must carry ``approved_for_live is True``. The field DEFAULTS FALSE when absent
        from the yaml, so a template with a real, Meta-approved SID still sends NOTHING
        on deploy until Fazal explicitly flips ``approved_for_live: true`` in
        ``twilio_templates.yaml``. This is the deploy-safe gate — the daily 7 AM cron
        cannot fire a single real owner send while the flag is false.
      GATE 2 (audience): the Entry's ``audience`` must be exactly ``"owner"``. The
        owner-notify seam never sends a customer-audience template to an owner number.
      GATE 3 (validate_params): EVERY declared template variable ({{1}}, {{2}}, …) must
        have a non-empty value in ``params``. A missing/empty positional would let
        Twilio render the template's SAMPLE value (the VT-400 "Hi Raj Cafe" bug), so a
        template that would render a sample is NEVER sent.

    Pre-existing fail-safe SKIPs (retained): unregistered template
    (``UnknownTemplateError``), absent language variant
    (``UnknownLanguageVariantError``), pending-approval stub SID
    (``content_sid is None``), no reachable WhatsApp recipient, and a send that returns
    ``success=False`` or raises. A notify failure NEVER aborts the daily sweep (one
    tenant's send must not stall the rest).

    ``send_fn`` is an injectable send seam (defaults to the live
    ``owner_surface.send_owner_template``) so tests record the call with 0 real Twilio.
    """
    from orchestrator import templates_registry
    from orchestrator.utils.twilio_send import get_tenant_whatsapp_number

    # 1. Registry-by-NAME resolution — fail-safe SKIP if absent/pending. Resolve BEFORE
    #    touching the recipient so an unregistered/stub template never reaches a send.
    try:
        entry = templates_registry.resolve(template_name, language)
    except templates_registry.UnknownLanguageVariantError:
        logger.warning(
            "trial_sweep: SKIP owner-notify tenant=%s template=%s lang=%s — no '%s' "
            "language variant in the registry (NEEDS-FAZAL); nothing sent.",
            tenant_id, template_name, language, language,
        )
        return
    except templates_registry.UnknownTemplateError:
        logger.warning(
            "trial_sweep: SKIP owner-notify tenant=%s template=%s lang=%s — template "
            "is UNREGISTERED in twilio_templates.yaml (NEEDS-FAZAL SID); nothing sent.",
            tenant_id, template_name, language,
        )
        return

    # GATE 1 (PRIMARY, fail-closed): approved_for_live. Defaults FALSE in the registry
    # when the yaml key is absent, so even a real Meta-approved SID sends NOTHING until
    # Fazal flips approved_for_live: true. This is the deploy-safe gate — the 7 AM cron
    # cannot fire a single owner send while the flag is false. Checked FIRST so an
    # un-approved template short-circuits before any param/recipient work.
    if entry.approved_for_live is not True:
        logger.warning(
            "trial_sweep: SKIP owner-notify tenant=%s template=%s lang=%s — NOT "
            "approved_for_live (fail-closed default; flip approved_for_live: true in "
            "twilio_templates.yaml once Fazal approves); nothing sent.",
            tenant_id, template_name, language,
        )
        return

    # GATE 2 (VULN4): audience enforcement — the owner-notify seam sends owner-audience
    # templates ONLY. A customer-audience template must never land on an owner number.
    if entry.audience != "owner":
        logger.warning(
            "trial_sweep: SKIP owner-notify tenant=%s template=%s lang=%s — audience=%r "
            "is not 'owner'; the owner-notify seam sends owner-audience templates only; "
            "nothing sent.",
            tenant_id, template_name, language, entry.audience,
        )
        return

    if entry.content_sid is None:
        logger.warning(
            "trial_sweep: SKIP owner-notify tenant=%s template=%s lang=%s — registry "
            "SID is a pending-approval stub (content_sid=None, NEEDS-FAZAL); nothing sent.",
            tenant_id, template_name, language,
        )
        return

    # GATE 3 (VULN1): validate_params — fail-closed if ANY declared positional ({{1}},
    # {{2}}, …) is missing or empty. An absent/blank positional makes Twilio render the
    # template's SAMPLE value (the VT-400 "Hi Raj Cafe" bug); a template that would
    # render a sample is NEVER sent. Stricter than templates_registry.validate_params
    # (which only checks key-set equality) — we also reject empty values.
    missing_or_empty = [
        var for var in entry.variables
        if params.get(var) in (None, "")
    ]
    if missing_or_empty:
        logger.warning(
            "trial_sweep: SKIP owner-notify tenant=%s template=%s lang=%s — missing/empty "
            "required params %s (a blank positional renders the Twilio SAMPLE — VT-400); "
            "nothing sent.",
            tenant_id, template_name, language, sorted(missing_or_empty),
        )
        return

    # 2. Resolve the owner's reachable WhatsApp recipient (the number the owner signed
    #    up with / is reachable on — same channel the welcome lands on). Skip if unset.
    recipient = get_tenant_whatsapp_number(tenant_id)
    if not recipient:
        logger.warning(
            "trial_sweep: SKIP owner-notify tenant=%s template=%s — tenant has no "
            "whatsapp_number; nothing sent.",
            tenant_id, template_name,
        )
        return

    # 3. Send via the VT-393 owner-utility seam. NEVER crash the sweep on a send error.
    send = send_fn
    if send is None:
        from orchestrator.owner_surface.owner_send import send_owner_template

        send = send_owner_template
    try:
        result = send(
            tenant_id, template_name, language, params, recipient_phone=recipient,
        )
    except Exception:  # noqa: BLE001 — a send failure must not abort the daily sweep
        logger.exception(
            "trial_sweep: owner-notify FAILED tenant=%s template=%s lang=%s; sweep continues",
            tenant_id, template_name, language,
        )
        return
    if getattr(result, "success", False):
        logger.info(
            "trial_sweep: owner-notify SENT tenant=%s template=%s lang=%s",
            tenant_id, template_name, language,
        )
    else:
        logger.warning(
            "trial_sweep: owner-notify NOT sent tenant=%s template=%s lang=%s (error_code=%s)",
            tenant_id, template_name, language,
            getattr(result, "error_code", "unknown"),
        )


def _compose_trial_subscribe_link(tenant_id: UUID) -> dict[str, Any] | None:
    """VT-359: compose the trial-end ``trial_subscribe_link`` params — owner_name + the VT-332
    deep-link carrying a freshly-minted single-use token (7-day TTL). Returns None if minting can't
    proceed (OWNER_JWT_SECRET unset / dormant) so the caller skips the send; the actual owner-WABA
    send is gate-live at the notify seam regardless (this only COMPOSES, per the dispatch)."""
    try:
        import os

        from orchestrator.billing.trial_end_token import (
            build_subscribe_deep_link,
            mint_trial_end_token,
        )
        from orchestrator.graph import get_pool

        token, _jti = mint_trial_end_token(str(tenant_id))
        # OWNER_PORTAL_URL already includes /team; build_subscribe_deep_link appends /team/subscribe,
        # so strip the trailing /team to avoid a doubled path.
        base = os.environ.get("OWNER_PORTAL_URL", "https://viabe.ai/team").removesuffix("/team")
        link = build_subscribe_deep_link(base, "", token)  # plan_tier empty — server-priced (VT-332 F3)
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT business_name FROM tenants WHERE id = %s", (str(tenant_id),)
            ).fetchone()
        owner_name = (dict(row).get("business_name") if row else None) or "there"
        return {"owner_name": owner_name, "subscribe_link": link}
    except Exception:
        logger.exception(
            "trial_sweep: trial_subscribe_link compose skipped tenant=%s (dormant/secret unset?)",
            tenant_id,
        )
        return None


def _resolve_owner_name(tenant_id: UUID) -> str | None:
    """VT-426 hardening — resolve the tenant's business/owner display name for the
    ``trial_ending`` template's ``owner_name`` ({{1}}) positional.

    Returns the ``tenants.business_name`` (the same field ``_compose_trial_subscribe_link``
    uses) or ``None`` on any read failure / empty value. Returning ``None`` lets the
    caller fail-CLOSED (skip the send) rather than send a template with a blank {{1}}
    that Twilio would render as the SAMPLE value (the VT-400 bug). Best-effort: ANY
    DB/import error → ``None`` (a name-read hiccup must never crash the daily sweep)."""
    try:
        from orchestrator.graph import get_pool

        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT business_name FROM tenants WHERE id = %s", (str(tenant_id),)
            ).fetchone()
        name = (dict(row).get("business_name") if row else None) or None
        return name
    except Exception:  # noqa: BLE001 — name read is best-effort; None → fail-closed skip
        logger.warning(
            "trial_sweep: owner_name resolve failed tenant=%s; owner-notify will skip",
            tenant_id,
        )
        return None


def _preferred_language(tenant_id: UUID) -> str:
    """VT-426 (Row D) — resolve the tenant's preferred WhatsApp language for the owner
    notify, defaulting to ``"en"``.

    Delegates to ``runner._load_preferred_language`` (PR-3, the per-tenant
    ``preferred_language ?? language_preference`` RLS read), which returns ``None`` on
    any read failure. We coerce that ``None`` to ``"en"`` here so the registry lookup
    always has a concrete variant to resolve. Lazy-imported (runner pulls in DBOS + the
    graph) to keep this zero-LLM sweep light; ANY import/read error → ``"en"`` (a
    language-read hiccup must never break the daily sweep)."""
    try:
        from orchestrator.runner import _load_preferred_language

        return _load_preferred_language(str(tenant_id)) or "en"
    except Exception:  # noqa: BLE001 — language read is best-effort; default to "en"
        logger.warning(
            "trial_sweep: preferred_language resolve failed tenant=%s; defaulting to 'en'",
            tenant_id,
        )
        return "en"


def _paused(tenant_id: UUID) -> bool:
    """VT-374 per-tenant pause check (kind 'trial_sweep'). SKIP semantics, not a blocking
    hold — a hold would stall every tenant behind one paused row in a daily sweep; the
    next sweep re-evaluates after release (expire/warn verdicts are recomputed from state,
    so nothing is lost). check_pause never raises (F9 two-tier)."""
    from orchestrator.run_control import check_pause

    return check_pause(tenant_id, "trial_sweep")


def _scan_active_trials(now: datetime) -> list[UUID]:
    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id FROM tenants WHERE phase = 'trial' "
            "AND paid_conversion_at IS NULL"
        )
        return [UUID(str(r["id"])) for r in cur.fetchall()]


def _apply_trial_transition(tenant_id: UUID, event: str) -> None:
    """Load the tenant's current phase + trial start, build a SubscriberState, and
    call apply_transition (the SOLE phase mutator). Best-effort re. DBOS context:
    log + continue under a sync canary."""
    from orchestrator.graph import get_pool
    from orchestrator.state import new_subscriber_state
    from orchestrator.transitions import apply_transition
    from psycopg.rows import dict_row

    try:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT phase, trial_started_at FROM tenants WHERE id = %s",
                (str(tenant_id),),
            )
            row = cur.fetchone()
        if row is None or row["phase"] != "trial":
            return
        state = new_subscriber_state(tenant_id, phase=row["phase"])
        state["trial_started_at"] = row["trial_started_at"]
        apply_transition(state, event, {"reason": "vt90_trial_sweep"})
    except Exception:  # noqa: BLE001 — phase mutate is best-effort under the sweep
        logger.exception(
            "trial_sweep: %s transition failed tenant=%s; sweep continues",
            event, tenant_id,
        )


def run_trial_evaluation_body(
    now: datetime | None = None, *, notify_fn: NotifyFn | None = None,
) -> list[Any]:
    """Daily trial sweep body. Returns the verdicts acted on. NO LLM."""
    from orchestrator.billing.trial_evaluator import evaluate_trial

    now = now or datetime.now(timezone.utc)
    notify = notify_fn or _default_notify
    acted: list[Any] = []
    for tid in _scan_active_trials(now):
        # VT-374 (trial_sweep, evaluate_tenant) seam — per-tenant pause check at loop top.
        if _paused(tid):
            logger.info(
                "trial_sweep: tenant=%s paused by run-control — skipped this sweep", tid
            )
            continue
        try:
            v = evaluate_trial(tid, now)
        except Exception:  # noqa: BLE001
            logger.exception("trial_sweep: evaluate failed tenant=%s; continue", tid)
            continue
        if v.decision == "none":
            continue
        acted.append(v)
        # VT-426 (Row D): per-tenant language — the owner gets the template variant in
        # their preferred language, not a hardcoded "en". Best-effort read (None on any
        # DB hiccup) → fall back to "en"; a language-read miss never breaks the sweep.
        language = _preferred_language(tid)
        # VT-426 hardening (VULN1): build the trial_ending params with BOTH declared
        # positionals — owner_name ({{1}}) + trial_end_date ({{2}}). owner_name resolves
        # to the business name; if it's unavailable it stays None and the owner-notify
        # GATE 3 fail-closes (skips) rather than sending a blank {{1}} that Twilio would
        # render as the SAMPLE ("Hi Raj Cafe", VT-400).
        params = {
            "owner_name": _resolve_owner_name(tid),
            "trial_end_date": v.trial_end.date().isoformat() if v.trial_end else "",
        }
        if v.decision == "expire":
            _apply_trial_transition(tid, "trial_expired")
            # VT-359: trial-end conversion nudge — the VT-332 subscribe-link send, fired ONCE at
            # trial-end. Composed here (SID + deep-link + single-use token); the actual owner-WABA
            # send is wired to the registry-driven owner notify (VT-426), fail-safe-skipping while
            # the SID is a pending stub. The owner can still subscribe from `lapsed` via this link.
            link_params = _compose_trial_subscribe_link(tid)
            if link_params is not None:
                notify(tid, "trial_subscribe_link", language, link_params)
        elif v.decision == "warn":
            notify(tid, "trial_ending", language, params)
    return acted


__all__ = ["NotifyFn", "_owner_notify", "run_trial_evaluation_body"]
