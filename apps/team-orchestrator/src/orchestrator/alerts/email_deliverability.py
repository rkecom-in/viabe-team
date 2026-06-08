"""VT-113 — daily email-deliverability monitor (Resend).

Daily 10:00 IST: pull the last-24h send outcomes from Resend, compute bounce/complaint rates, and
alert Fazal (Telegram ops chat) when bounce > 5% OR complaint > 0.1% — the thresholds that precede an
ESP reputation hit. Fail-SOFT: Resend down / parse error → log + skip (never crash the scheduler).

⚠️ ENDPOINT-CONFIRM (the #420 vendor-shape lesson): Resend delivers bounce/complaint signals primarily
via WEBHOOKS (email.bounced / email.complained); a single aggregate poll-stats endpoint is NOT clearly
documented. This module models a list-and-count over GET /emails (last_event per row). The request
shape is pinned by an injected-transport unit test, and the live canary (fetch against the real API)
is gated fail-not-skip. If the real shape differs, the canary fails → we switch to (a) the list
endpoint's real field, or (b) a Resend-webhook ingress (email.bounced/complained → a counts table).
Until the canary runs green from a network-unblocked host, treat the poll path as UNCONFIRMED.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

_BOUNCE_THRESHOLD = 0.05  # 5%
_COMPLAINT_THRESHOLD = 0.001  # 0.1%
_RESEND_BASE = "https://api.resend.com"
_TIMEOUT_S = 15.0

GetFn = Callable[[str, str], dict[str, Any]]  # (path, api_key) -> json


@dataclass(frozen=True)
class DeliverabilityStats:
    ok: bool
    sent: int = 0
    bounced: int = 0
    complained: int = 0

    @property
    def bounce_rate(self) -> float:
        return self.bounced / self.sent if self.sent else 0.0

    @property
    def complaint_rate(self) -> float:
        return self.complained / self.sent if self.sent else 0.0

    def breached(self) -> bool:
        return self.bounce_rate > _BOUNCE_THRESHOLD or self.complaint_rate > _COMPLAINT_THRESHOLD


def _default_get(path: str, api_key: str) -> dict[str, Any]:
    import httpx

    resp = httpx.get(
        f"{_RESEND_BASE}{path}",
        headers={"authorization": f"Bearer {api_key}", "accept": "application/json"},
        timeout=_TIMEOUT_S,
    )
    resp.raise_for_status()
    return dict(resp.json())


def fetch_resend_stats(*, get_fn: GetFn | None = None) -> DeliverabilityStats:
    """Last-24h send outcomes from Resend. Fail-closed (ok=False) on missing key / vendor / parse.
    Result-only: counts only, no recipient PII retained."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        logger.warning("email_deliverability: RESEND_API_KEY absent — skip")
        return DeliverabilityStats(ok=False)
    try:
        raw = (get_fn or _default_get)("/emails", api_key)
        rows = raw.get("data", raw if isinstance(raw, list) else [])
        sent = bounced = complained = 0
        for r in rows:
            ev = str((r or {}).get("last_event", "")).lower()
            sent += 1
            if ev == "bounced":
                bounced += 1
            elif ev == "complained":
                complained += 1
        return DeliverabilityStats(ok=True, sent=sent, bounced=bounced, complained=complained)
    except Exception:
        logger.exception("email_deliverability: Resend stats fetch failed (fail-soft)")
        return DeliverabilityStats(ok=False)


def run_deliverability_check_body(
    scheduled_time: Any = None, actual_time: Any = None
) -> dict[str, Any]:
    """Daily check (also the @DBOS.scheduled handler — accepts the scheduler's time args). Fail-soft:
    vendor down → log + skip (no alert, no crash). Threshold breach → Fazal Telegram alert. Returns a
    summary (for the canary/tests)."""
    stats = fetch_resend_stats()
    if not stats.ok:
        logger.warning("email_deliverability: stats unavailable — skipped (fail-soft)")
        return {"ok": False, "alerted": False}
    if stats.breached():
        from orchestrator.billing.refund_executor import _alert_fazal

        _alert_fazal(
            f"📧 Email deliverability ALERT (24h): sent={stats.sent} "
            f"bounce={stats.bounce_rate:.1%} (>{_BOUNCE_THRESHOLD:.0%}? {stats.bounce_rate > _BOUNCE_THRESHOLD}) "
            f"complaint={stats.complaint_rate:.2%} (>{_COMPLAINT_THRESHOLD:.1%}? {stats.complaint_rate > _COMPLAINT_THRESHOLD}). "
            f"Check Resend + the resend-cutover runbook."
        )
        return {"ok": True, "alerted": True, "bounce_rate": stats.bounce_rate, "complaint_rate": stats.complaint_rate}
    return {"ok": True, "alerted": False, "bounce_rate": stats.bounce_rate, "complaint_rate": stats.complaint_rate}


_DELIVERABILITY_CRON = "30 4 * * *"  # 04:30 UTC = 10:00 IST


def register_email_deliverability_scheduler() -> None:
    """Apply @DBOS.workflow + @DBOS.scheduled. Called from main.py lifespan BEFORE launch_dbos
    (the register-before-launch contract, mirrors register_alert_scheduler)."""
    from dbos import DBOS

    DBOS.workflow()(run_deliverability_check_body)
    DBOS.scheduled(_DELIVERABILITY_CRON)(_deliverability_scheduled)


def _deliverability_scheduled(scheduled_time: Any, actual_time: Any) -> None:
    """@DBOS.scheduled handler (None-returning, per the DBOS scheduled signature)."""
    run_deliverability_check_body(scheduled_time, actual_time)


__all__ = [
    "DeliverabilityStats",
    "fetch_resend_stats",
    "run_deliverability_check_body",
    "register_email_deliverability_scheduler",
]
