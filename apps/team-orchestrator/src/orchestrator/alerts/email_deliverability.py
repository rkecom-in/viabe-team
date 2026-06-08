"""VT-113 — daily email-deliverability monitor (Resend).

Daily 10:00 IST: pull the last-24h send outcomes from Resend, compute bounce/complaint rates, and
alert Fazal (Telegram ops chat) when bounce > 5% OR complaint > 0.1% — the thresholds that precede an
ESP reputation hit. Fail-SOFT: Resend down / parse error → log + skip (never crash the scheduler).

ENDPOINT (CONFIRMED 2026-06-09 by the live canary): GET /emails → {"object":"list", "has_more": bool,
"data": [{id, created_at, last_event, to, from, subject, ...}]}, newest-first, max 100 rows/page,
cursor via ?after=<last_row_id>. Resend exposes NO server-side date filter, so the rolling 24h window
is computed CLIENT-SIDE: paginate (?limit=100&after=...) while has_more AND the page still holds rows
with created_at >= now-24h (newest-first → short-circuit once a page is fully older than the window),
counting bounced/complained over exactly the in-window rows. Page-capped (_MAX_PAGES) so a high-volume
account can't spin the daily job; a cap hit sets stats.capped (counts may undercount → said in the alert).

PII guardrail (the full-access key now returns recipient data): this module reads ONLY last_event +
created_at per row (+ id as the opaque pagination cursor). It NEVER stores, returns, or logs to/from/
subject/raw rows — the result is integer counts only, the alert carries counts/rates only (CL-390).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

_BOUNCE_THRESHOLD = 0.05  # 5%
_COMPLAINT_THRESHOLD = 0.001  # 0.1%
_RESEND_BASE = "https://api.resend.com"
_TIMEOUT_S = 15.0
_PAGE_LIMIT = 100  # Resend max rows/page
_MAX_PAGES = 20  # defensive cap (≤2000 rows) so a high-volume account can't spin the daily job
_WINDOW_HOURS = 24

GetFn = Callable[[str, str], dict[str, Any]]  # (path, api_key) -> json


@dataclass(frozen=True)
class DeliverabilityStats:
    ok: bool
    sent: int = 0
    bounced: int = 0
    complained: int = 0
    capped: bool = False  # True if the _MAX_PAGES cap was hit → counts may undercount

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


def _parse_created_at(value: Any) -> datetime | None:
    """Resend created_at (e.g. '2026-06-01 03:30:09.433947+00') → aware datetime; None if unparseable.
    Normalizes the space separator and a 2-digit tz offset (+00 → +00:00) for datetime.fromisoformat."""
    s = str(value or "").strip()
    if not s:
        return None
    s = s.replace(" ", "T", 1)
    s = re.sub(r"([+-]\d{2})$", r"\1:00", s)  # +00 → +00:00
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fetch_resend_stats(
    *, get_fn: GetFn | None = None, now: datetime | None = None
) -> DeliverabilityStats:
    """Rolling-24h send outcomes from Resend (bounce/complaint counts). Fail-closed (ok=False) on
    missing key / vendor / parse. True 24h via a CLIENT-SIDE created_at filter + ?after cursor
    pagination, page-capped (sets capped on a cap-hit). Result-only: counts ONLY — recipient PII
    (to/from/subject/raw rows) is never read into the result, returned, or logged (CL-390)."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        logger.warning("email_deliverability: RESEND_API_KEY absent — skip")
        return DeliverabilityStats(ok=False)
    get = get_fn or _default_get
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=_WINDOW_HOURS)
    sent = bounced = complained = 0
    capped = False
    try:
        after: str | None = None
        pages = 0
        while True:
            if pages >= _MAX_PAGES:
                capped = True
                break
            path = f"/emails?limit={_PAGE_LIMIT}" + (f"&after={after}" if after else "")
            raw = get(path, api_key)
            pages += 1
            rows = raw.get("data", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
            if not rows:
                break
            for r in rows:
                ts = _parse_created_at((r or {}).get("created_at"))
                if ts is None or ts < cutoff:
                    continue  # outside the 24h window (or untimestamped) → not counted
                sent += 1
                ev = str((r or {}).get("last_event", "")).lower()
                if ev == "bounced":
                    bounced += 1
                elif ev == "complained":
                    complained += 1
            # newest-first: once a page's OLDEST row predates the window, no later page is in-window
            oldest = _parse_created_at((rows[-1] or {}).get("created_at"))
            if oldest is not None and oldest < cutoff:
                break
            if not (isinstance(raw, dict) and raw.get("has_more")):
                break
            after = (rows[-1] or {}).get("id")  # opaque cursor only — not stored/logged as content
            if not after:
                break
        if capped:
            logger.warning(
                "email_deliverability: hit the %d-page cap over the 24h window — counts may undercount",
                _MAX_PAGES,
            )
        return DeliverabilityStats(
            ok=True, sent=sent, bounced=bounced, complained=complained, capped=capped
        )
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

        cap_note = " ⚠️ page-cap hit — counts may UNDERCOUNT." if stats.capped else ""
        _alert_fazal(
            f"📧 Email deliverability ALERT (24h): sent={stats.sent} "
            f"bounce={stats.bounce_rate:.1%} (>{_BOUNCE_THRESHOLD:.0%}? {stats.bounce_rate > _BOUNCE_THRESHOLD}) "
            f"complaint={stats.complaint_rate:.2%} (>{_COMPLAINT_THRESHOLD:.1%}? {stats.complaint_rate > _COMPLAINT_THRESHOLD})."
            f"{cap_note} Check Resend + the resend-cutover runbook."
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
