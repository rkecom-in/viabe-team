"""VT-90 — trial-lifecycle daily sweep (the action side of trial_evaluator).

One off-peak daily sweep (mirrors day39): for each active, unpaid trial tenant,
evaluate_trial → act:
  - extend  → apply_transition('trial_extension_granted') (phase→trial_extended,
              count++) + notify `trial_extension_offered` (template 19).
  - exhaust → apply_transition('trial_extension_exhausted') (phase→cancelled) +
              notify `trial_max_reached` (template 20) when the cap drove it.
              (VT-90 does the PHASE transition only; the graceful-exit/goodbye
              MESSAGE belongs to VT-93 — Cowork boundary.)
  - warn    → notify `trial_ending` (day-12). No phase change.

NO LLM (Pillar 1). The owner notify is an INJECTABLE seam — the real owner-WABA
send is gate-live (NEEDS-FAZAL: provision the Meta SIDs); the default stub logs.
Idempotent: extend moves trial_end forward (count++), exhaust → terminal cancelled
(re-scan skips both). apply_transition is the SOLE phase mutator.
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


def _scan_active_trials(now: datetime) -> list[UUID]:
    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id FROM tenants WHERE phase IN ('trial', 'trial_extended') "
            "AND paid_conversion_at IS NULL"
        )
        return [UUID(str(r["id"])) for r in cur.fetchall()]


def _apply_trial_transition(tenant_id: UUID, event: str) -> None:
    """Load the tenant's current phase + trial fields, build a SubscriberState, and
    call apply_transition (the SOLE phase mutator). Best-effort re. DBOS context
    (mirrors _apply_day39_refund_transition): log + continue under a sync canary."""
    from orchestrator.graph import get_pool
    from orchestrator.state import new_subscriber_state
    from orchestrator.transitions import apply_transition
    from psycopg.rows import dict_row

    try:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT phase, trial_started_at, trial_extension_count "
                "FROM tenants WHERE id = %s", (str(tenant_id),),
            )
            row = cur.fetchone()
        if row is None or row["phase"] not in ("trial", "trial_extended"):
            return
        state = new_subscriber_state(tenant_id, phase=row["phase"])
        state["trial_started_at"] = row["trial_started_at"]
        state["trial_extension_count"] = int(row["trial_extension_count"])
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
        try:
            v = evaluate_trial(tid, now)
        except Exception:  # noqa: BLE001
            logger.exception("trial_sweep: evaluate failed tenant=%s; continue", tid)
            continue
        if v.decision == "none":
            continue
        acted.append(v)
        params = {"trial_end_date": v.trial_end.date().isoformat() if v.trial_end else ""}
        if v.decision == "extend":
            _apply_trial_transition(tid, "trial_extension_granted")
            notify(tid, "trial_extension_offered", "en", params)
        elif v.decision == "exhaust":
            _apply_trial_transition(tid, "trial_extension_exhausted")
            if v.extension_count >= int(_max_ext()):
                notify(tid, "trial_max_reached", "en", params)
            # VT-359: trial-end conversion nudge — the VT-332 subscribe-link send, fired ONCE at
            # trial-end (regardless of cap). Composed here (SID + deep-link + single-use token);
            # the actual owner-WABA send STAYS gated at the notify seam (the stub logs until
            # go-live — dispatch: "wire the call, don't flip the gate").
            link_params = _compose_trial_subscribe_link(tid)
            if link_params is not None:
                notify(tid, "trial_subscribe_link", "en", link_params)
        elif v.decision == "warn":
            notify(tid, "trial_ending", "en", params)
    return acted


def _max_ext() -> int:
    from orchestrator.billing.trial_evaluator import _config

    return int(_config()["max_trial_extensions"])


__all__ = ["NotifyFn", "run_trial_evaluation_body"]
