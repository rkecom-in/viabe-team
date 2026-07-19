"""VT-368 Gap-4 — paced bilingual WhatsApp delivery of one business-plan version.

Composition (K scales with the plan — a thin 2-month plan = fewer parts):

  part 0      — the summary headline (``summary_json.text`` / ``text_hi``);
  parts 1..K  — ONE message per DISTINCT month present in the roadmap (that month's
                objectives in seq order + any owner_action prompt, EN/HI);
  final part  — the Gap-6 entry hint ("Reply to adjust any step." / Hindi mirror).

Locale is resolved ONCE per delivery (``owner_surface.freeform_acks.resolve_owner_locale``);
the recipient is the owner's own ``tenants.whatsapp_number`` (RLS'd read).

Idempotent replay: the ``delivered_parts`` bitmap (``store.mark_part_delivered``) is the
resume cursor — a re-invocation skips already-set bits and sends ONLY the unsent parts.
Best-effort per part (mirrors ``journey._send``): one failing send is logged + skipped so
its bit stays 0 for the next replay; ``deliver_plan`` NEVER raises.

Pacing is INTRA-SESSION: ``sleep_fn(2.0)`` between consecutive send attempts — a digestible
burst, NOT a multi-day drip. ``sleep_fn`` is injectable for tests.

Plain function by design — the Gap-4 generator calls it directly today; the parent wires the
DBOS decoration/seams later. Body text is plan content only — no third-party PII (CL-390);
the recipient phone is never logged here (the send util logs only the hashed token).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from orchestrator.business_plan import store

logger = logging.getLogger(__name__)

PART_GAP_SECONDS = 2.0

# VT-576: inline citation markers ([F1], [F12]) are GROUNDING RECEIPTS the generator prompt requires
# and the stored artifact KEEPS (readers re-verify citations against the frozen fact bundle). They must
# NEVER reach the owner's WhatsApp — the live drill leaked "... [F1][F5]" into the business summary. We
# strip them at RENDER time only (here); ``store.write_new_version`` persists the cited text untouched.
_CITATION_RE = re.compile(r"\s*\[\s*F\d+\s*\]")


def _strip_citations(text: str) -> str:
    """Remove ``[F#]`` receipts from owner-facing text, then tidy any doubled spaces the removal left."""
    return re.sub(r"\s{2,}", " ", _CITATION_RE.sub("", text or "")).strip()

_SUPPORTED = ("en", "hi")
_FALLBACK_HEADLINE = {
    "en": "Your business plan is ready.",
    "hi": "आपका बिज़नेस प्लान तैयार है।",
}
_MONTH_HEADER = {"en": "Month {m}", "hi": "महीना {m}"}
_ACTION_PREFIX = {"en": "Your action: ", "hi": "आपका कदम: "}
_ADJUST_HINT = {
    "en": "Reply to adjust any step.",
    "hi": "किसी भी कदम को बदलने के लिए जवाब दें।",
}


def compose_parts(plan: store.BusinessPlan, locale: str) -> list[str]:
    """The ordered message parts for one plan version in ONE resolved locale.

    Pure composition (no I/O) — part count = 1 (summary) + #distinct-months + 1 (hint).
    Months ascend; items within a month follow ``seq``. Hindi falls back per-field to the
    English value (a missing ``text_hi`` / ``owner_action_hi`` must not drop content).
    """
    lang = locale if locale in _SUPPORTED else "en"
    summary = plan.summary or {}
    headline = (
        (summary.get("text_hi") if lang == "hi" else None)
        or summary.get("text")
        or _FALLBACK_HEADLINE[lang]
    )
    # VT-576: strip [F#] citation receipts from every owner-facing string (headline, objective, action).
    parts: list[str] = [_strip_citations(str(headline))]

    items = [i for i in (plan.roadmap or []) if isinstance(i, dict)]
    months = sorted({int(i["month"]) for i in items if i.get("month")})
    for m in months:
        lines = [_MONTH_HEADER[lang].format(m=m)]
        month_items = sorted(
            (i for i in items if int(i.get("month", 0)) == m),
            key=lambda i: int(i.get("seq", 0)),
        )
        for item in month_items:
            lines.append(f"• {_strip_citations(str(item.get('objective', '')))}")
            if item.get("owner_action_needed"):
                action = (
                    item.get("owner_action_hi") if lang == "hi" else None
                ) or item.get("owner_action")
                if action:
                    lines.append(f"→ {_ACTION_PREFIX[lang]}{_strip_citations(str(action))}")
        parts.append("\n".join(lines))

    parts.append(_ADJUST_HINT[lang])
    return parts


def deliver_plan(
    tenant_id: UUID | str,
    version: int,
    *,
    sleep_fn: Callable[[float], Any] = time.sleep,
) -> None:
    """Send one plan version to the owner as a paced multi-part WhatsApp burst.

    Delivery only ever targets the LATEST version — if ``version`` is no longer the
    latest (an edit/regenerate raced in), log + return; the newer version owns delivery.
    Replay-safe via the ``delivered_parts`` bitmap; per-part best-effort; never raises.
    """
    try:
        plan = store.get_active_plan(tenant_id)
        if plan is None:
            logger.warning("VT-368 delivery: no plan for tenant=%s — nothing to send", tenant_id)
            return
        if plan.version != version:
            logger.warning(
                "VT-368 delivery: requested v%s but latest is v%s tenant=%s — "
                "delivery only targets the latest version; skipping",
                version,
                plan.version,
                tenant_id,
            )
            return

        from orchestrator.utils.twilio_send import get_tenant_whatsapp_number

        recipient = get_tenant_whatsapp_number(plan.tenant_id)
        if not recipient:
            logger.warning(
                "VT-368 delivery: tenant=%s has no whatsapp_number — skipping", tenant_id
            )
            return

        from orchestrator.owner_surface.freeform_acks import resolve_owner_locale

        locale = resolve_owner_locale(tenant_id)  # once per delivery; best-effort → 'en'
        parts = compose_parts(plan, locale)
        last = len(parts) - 1
        delivered = plan.delivered_parts  # the replay cursor read with the plan

        attempted = False
        for i, body in enumerate(parts):
            if delivered >> i & 1:
                continue  # idempotent replay — this part already landed
            if attempted:
                sleep_fn(PART_GAP_SECONDS)
            attempted = True
            try:
                from orchestrator.utils.twilio_send import send_freeform_message

                # VT-611 Package H0 — thread tenant_id so each delivered part lands in the lifetime
                # conversation_log (was bare -> _record_owner_conversation_turn no-op'd).
                send_freeform_message(body, recipient, tenant_id=tenant_id, surface="manager")
            except Exception:  # noqa: BLE001 — best-effort per part; bit stays 0 for replay
                logger.warning(
                    "VT-368 delivery: part %d/%d failed tenant=%s v%s — continuing "
                    "(unset bit will resend on replay)",
                    i,
                    last,
                    tenant_id,
                    version,
                )
                continue
            store.mark_part_delivered(tenant_id, version, i, final=(i == last))
    except Exception:  # noqa: BLE001 — delivery is best-effort; the plan row already exists
        logger.exception(
            "VT-368 delivery: unexpected failure tenant=%s v%s — not raising", tenant_id, version
        )
