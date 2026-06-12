"""VT-280 — Orchestrator → VTR daily digest ("what the agent did / needs the VTR").

CL-426 closes here: the digest is the VTR's window into agent activity. CL-425 goes DB-ENFORCED on
this path — it reads ONLY through `vtr_admin_connection()` (SET ROLE app_vtr_admin_role) + the
VT-281 de-identified views, so a raw-PII read is permission-denied, not merely masked (the admin
role too has ZERO raw-table grants). The VTR (Fazal = VTR#1) sees route='vtr' (knowledge-gap)
escalations by kind/severity + the escalation-rate decay trend (VT-282 logic over the view) —
never customer names/phones (those rows are owner-routed and, even if surfaced, the view exposes
no PII column).

VT-377 (mig-134): the vtr_* views are now assignment-scoped per operator. This digest is the
FLEET-WIDE Fazal=VTR#1 surface with no operator JWT in scope (scheduled DBOS trigger), so it reads
as the ADMIN tier — the mig-134 predicate's `current_user = 'app_vtr_admin_role'` leg keeps
all-tenants (role IS the mechanism, Cowork ruling 20260612T011000Z). A plain `vtr_connection()`
read here would fail closed to zero rows.

NO LLM (Pillar 1). Sent via the established sync `_alert_fazal` Telegram path (VTR#1 = the ops
chat). Daily DBOS trigger; the event-path (immediate digest on a high-severity vtr escalation) is a
documented phase-2 extension.
"""

from __future__ import annotations

import datetime as dt
import logging

from psycopg.rows import dict_row

from orchestrator.owner_surface.escalation_metrics import _trend
from orchestrator.privacy.vtr import vtr_admin_connection

logger = logging.getLogger(__name__)

_RECENT_DAYS = 7
_PRIOR_DAYS = 7


def build_vtr_digest(now: dt.datetime | None = None) -> str:
    """Compose the PII-free VTR digest text from the de-identified view ONLY (via
    app_vtr_admin_role — the fleet-wide VTR#1 tier; see module docstring, VT-377).

    Reads `vtr_escalations` through `vtr_admin_connection` — open route='vtr' counts by
    kind/severity + the recent-vs-prior decay trend. No raw table, no PII. now=None → SQL now()."""
    by_kind: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    trend = "flat"
    with vtr_admin_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        # Open knowledge-gap (route='vtr') escalations, grouped — the VTR's actionable queue.
        cur.execute(
            "SELECT kind, severity, count(*) AS n FROM vtr_escalations "
            "WHERE route = 'vtr' AND status = 'open' GROUP BY kind, severity"
        )
        for r in cur.fetchall():
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + int(r["n"])
            by_severity[r["severity"]] = by_severity.get(r["severity"], 0) + int(r["n"])
        # Decay: recent vs prior window of vtr-routed escalations (CL-426 — flat/rising is the
        # product-bug signal). Computed over the view (opened_at), still app_vtr_role-scoped.
        cur.execute(
            "SELECT "
            "  count(*) FILTER (WHERE opened_at > COALESCE(%(now)s::timestamptz, now()) "
            "                     - make_interval(days => %(r)s)) AS recent, "
            "  count(*) FILTER (WHERE opened_at <= COALESCE(%(now)s::timestamptz, now()) "
            "                       - make_interval(days => %(r)s) "
            "                     AND opened_at > COALESCE(%(now)s::timestamptz, now()) "
            "                       - make_interval(days => %(tot)s)) AS prior "
            "FROM vtr_escalations WHERE route = 'vtr'",
            {"now": now, "r": _RECENT_DAYS, "tot": _RECENT_DAYS + _PRIOR_DAYS},
        )
        row = cur.fetchone() or {"recent": 0, "prior": 0}  # count(*) always returns a row; defensive
        recent, prior = int(row["recent"]), int(row["prior"])
        trend = _trend(recent, prior)

    total = sum(by_kind.values())
    kinds = ", ".join(f"{k}:{n}" for k, n in sorted(by_kind.items())) or "none"
    sev = ", ".join(f"{s}:{n}" for s, n in sorted(by_severity.items())) or "none"
    health = "✅ declining" if trend == "declining" else f"⚠️ {trend}"
    return (
        f"🧭 VTR digest — {total} open knowledge-gap escalation(s)\n"
        f"by kind: {kinds}\n"
        f"by severity: {sev}\n"
        f"7d-vs-prior trend: {health} (recent={recent}, prior={prior})\n"
        f"— de-identified (VT-281); identity-needing items go to the owner (VT-279)."
    )


def run_vtr_digest_body(now: dt.datetime | None = None, *, send: bool = True) -> str:
    """Build + (best-effort) send the digest to the VTR-Telegram surface. Returns the text.

    Reuses the sync `_alert_fazal` ops-chat path (VTR#1 = Fazal). NO LLM, NO PII."""
    text = build_vtr_digest(now)
    if send:
        try:
            from orchestrator.alerts.clients import alert_fazal as _alert_fazal

            _alert_fazal(text)
        except Exception:
            logger.exception("VT-280 VTR digest send failed")
    logger.info("VT-280 VTR digest composed (%d chars)", len(text))
    return text
