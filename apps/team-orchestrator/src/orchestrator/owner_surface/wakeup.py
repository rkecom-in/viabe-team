"""VT-683 P3 — the daily wake-up (``team_wakeup2``) send helper + eligibility.

ONE place that builds the wake-up template params, resolves the owner's registry language variant,
validates the signature, and dispatches through the ledgered owner-template seam
(``owner_send.send_owner_template`` → ``twilio_send.send_template_message``). Two callers share it:

  1. the daily wake-up sweep (``scheduled_triggers.run_wakeup_sweep_body``) — fires 10:30 IST,
  2. the manager stale-resume call site (``manager.stale_resume.reengage_stale_task``) — Fazal
     point B (2026-07-22): ``team_reengage`` MERGED into the wake-up, one re-engage surface.

``team_wakeup2`` is one of the THREE whitelisted owner templates (OTP / welcome4 / wakeup2 — Fazal
ruling 2026-07-18); everything else rides the 24h session. Its two vars are ``owner_name`` +
``pending_count`` — the honest count of still-queued owner-comms items (the loop only wakes when the
queue is non-empty, so the count is truthful by construction).

Imports of the heavier send/registry surfaces are LAZY (inside the functions) so importing this
module is cheap — the ``SendResult`` return type is a string annotation under
``from __future__ import annotations``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover — type-only import, never at runtime
    from orchestrator.utils.twilio_send import SendResult

logger = logging.getLogger("orchestrator.owner_surface.wakeup")

#: The daily wake-up template (VT-683 P3). Registry-resolved, never a hard-coded SID.
WAKEUP_TEMPLATE = "team_wakeup2"

#: ≤1/day bound: a tenant woken less than this ago is skipped by the next sweep. The cron fires
#: once daily at 10:30 IST; a 20h floor gives exactly ≤1/day while being robust to a manual
#: re-drive / canary within the same day. Durable via ``tenants.last_wakeup_at`` (mig 181).
WAKEUP_MIN_INTERVAL = timedelta(hours=20)

#: Owner locale (``owner_locale.SUPPORTED_OWNER_LANGS`` = en|hinglish|hi) → the wake-up template's
#: registry language variant. 'hinglish' → the APPROVED 'hing' variant (team_wakeup2 registers one,
#: unlike the D1 general rule where hinglish falls back to en for not-yet-approved variants); 'hi' →
#: Devanagari; everything else → 'en'. Resolution is guarded (an absent variant → 'en').
_LOCALE_TO_WAKEUP_LANG = {"hi": "hi", "hinglish": "hing", "en": "en"}


def wakeup_language(tenant_id: UUID | str) -> str:
    """Resolve the wake-up template's registry language variant from the owner's locale.

    'hinglish' → 'hing' IFF the registry resolves that variant for the wake-up template (it does —
    team_wakeup2 has an approved hing SID); otherwise a safe 'en' fallback (never fail the send on a
    missing variant). Best-effort: the underlying locale read already defaults to 'en'.
    """
    from orchestrator.owner_surface.owner_locale import resolve_owner_locale
    from orchestrator.templates_registry import (
        UnknownLanguageVariantError,
        UnknownTemplateError,
        resolve,
    )

    locale = resolve_owner_locale(tenant_id)  # en | hinglish | hi
    lang = _LOCALE_TO_WAKEUP_LANG.get(locale, "en")
    if lang == "en":
        return "en"
    try:
        resolve(WAKEUP_TEMPLATE, lang)  # verify the variant exists before routing to it
        return lang
    except (UnknownLanguageVariantError, UnknownTemplateError):
        return "en"


def queued_comms_count(tenant_id: UUID | str) -> int:
    """Count of still-'queued' owner-comms items for the tenant — the honest wake-up pending_count.

    Fail-soft: any read error → 0 (a count hiccup must never break the wake-up; the sweep then skips
    this tenant, ``reengage`` floors to 1). Tenant-scoped read (RLS via tenant_connection)."""
    try:
        from orchestrator.db.tenant_connection import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM owner_comms_queue "
                "WHERE tenant_id = %s AND status = 'queued'",
                (str(tenant_id),),
            ).fetchone()
    except Exception:  # noqa: BLE001 — count is best-effort; a read hiccup must not crash the wake-up
        logger.warning("wakeup: queued-count read failed tenant=%s -> 0", tenant_id)
        return 0
    n = (row["n"] if isinstance(row, dict) else row[0]) if row else 0
    return int(n or 0)


def owner_contact(tenant_id: UUID | str) -> tuple[str | None, str]:
    """``(owner_phone_or_none, owner_name)`` for the wake-up send.

    ``owner_phone`` falls back to ``whatsapp_number`` (mirrors task_outcome / owner_comms_sweep);
    ``owner_name`` is the ``business_name`` display name (or '' — the template renders a blank {{1}},
    the same shape ``reengage`` already sends). Best-effort: any read error → (None, '') so the
    caller fail-closed-skips rather than crashing the sweep."""
    try:
        from orchestrator.db.tenant_connection import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT owner_phone, whatsapp_number, business_name FROM tenants WHERE id = %s",
                (str(tenant_id),),
            ).fetchone()
    except Exception:  # noqa: BLE001 — contact read is best-effort; None → fail-closed skip
        logger.warning("wakeup: owner-contact read failed tenant=%s", tenant_id)
        return None, ""
    if row is None:
        return None, ""
    if isinstance(row, dict):
        phone = row.get("owner_phone") or row.get("whatsapp_number")
        name = row.get("business_name") or ""
    else:
        phone = row[0] or row[1]
        name = row[2] or ""
    return (str(phone) if phone else None), str(name)


def send_wakeup(
    tenant_id: UUID | str,
    *,
    owner_phone: str,
    owner_name: str = "",
    pending_count: int,
) -> "SendResult | None":
    """Send ``team_wakeup2`` in the owner's language via the ledgered owner-template seam.

    ``pending_count`` is floored to 1 (a re-engage is always about at least one pending matter; the
    template copy reads "{{2}} item(s) waiting"). Returns the ``SendResult`` on a real send attempt
    (success OR a reported failure — Pillar 7 honesty, never swallowed); returns ``None`` (raising
    nothing) when the template is unconfigured / missing the language variant / signature-mismatched
    — the caller must treat that as a fail-closed skip / VTR incident, NEVER a freeform send (which
    would 63016 outside the window). Delivery is ledgered by ``send_owner_template`` (VT-524)."""
    from orchestrator.owner_surface.owner_send import send_owner_template
    from orchestrator.templates_registry import (
        TemplateRegistryError,
        UnknownLanguageVariantError,
        UnknownTemplateError,
        VariableSignatureMismatchError,
        validate_params,
    )

    language = wakeup_language(tenant_id)
    count = max(1, int(pending_count))
    params = {"owner_name": owner_name or "", "pending_count": str(count)}
    try:
        validate_params(WAKEUP_TEMPLATE, language, params)
        return send_owner_template(
            UUID(str(tenant_id)),
            WAKEUP_TEMPLATE,
            language,
            params,
            recipient_phone=owner_phone,
        )
    except (
        UnknownTemplateError,
        UnknownLanguageVariantError,
        VariableSignatureMismatchError,
        TemplateRegistryError,
    ) as exc:
        logger.warning(
            "wakeup: %s not configured for language=%s (fail-closed, no send) tenant=%s: %s",
            WAKEUP_TEMPLATE, language, tenant_id, exc,
        )
        return None


def _is_due(last: datetime | None, now: datetime, min_interval: timedelta) -> bool:
    """Pure predicate: never woken (``last`` None) → due; else due iff at least ``min_interval``
    has elapsed since the last wake-up (the ≤1/day bound)."""
    if last is None:
        return True
    return (now - last) >= min_interval


def wakeup_due(
    tenant_id: UUID | str,
    *,
    now: datetime | None = None,
    min_interval: timedelta = WAKEUP_MIN_INTERVAL,
) -> bool:
    """DB-backed ≤1/day gate: is the tenant eligible for a wake-up now?

    Reads ``tenants.last_wakeup_at`` (mig 181). Fail-CLOSED (False) on any read error — a tenant we
    can't prove is due must not be re-woken."""
    now = now or datetime.now(timezone.utc)
    try:
        from orchestrator.db.tenant_connection import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT last_wakeup_at FROM tenants WHERE id = %s", (str(tenant_id),)
            ).fetchone()
    except Exception:  # noqa: BLE001 — an unreadable due-state is a not-due state (never re-wake)
        logger.warning("wakeup: due-check read failed tenant=%s (fail-closed)", tenant_id)
        return False
    if row is None:
        return False
    last = row["last_wakeup_at"] if isinstance(row, dict) else row[0]
    return _is_due(last if isinstance(last, datetime) else None, now, min_interval)


def mark_wakeup_sent(tenant_id: UUID | str, *, now: datetime | None = None) -> None:
    """Record a wake-up send (the ≤1/day durable bookkeeping). Best-effort — a failed mark risks at
    most one extra wake-up next slot, never a crash. Tenant-scoped write (RLS via tenant_connection,
    the same path ``record_observed_language`` uses to UPDATE its own tenants row)."""
    now = now or datetime.now(timezone.utc)
    try:
        from orchestrator.db.tenant_connection import tenant_connection

        with tenant_connection(tenant_id) as conn:
            conn.execute(
                "UPDATE tenants SET last_wakeup_at = %s WHERE id = %s", (now, str(tenant_id))
            )
    except Exception:  # noqa: BLE001 — mark is best-effort; the ≤1/day guard tolerates one miss
        logger.warning("wakeup: mark-sent write failed tenant=%s", tenant_id)


__all__ = [
    "WAKEUP_MIN_INTERVAL",
    "WAKEUP_TEMPLATE",
    "mark_wakeup_sent",
    "owner_contact",
    "queued_comms_count",
    "send_wakeup",
    "wakeup_due",
    "wakeup_language",
]
