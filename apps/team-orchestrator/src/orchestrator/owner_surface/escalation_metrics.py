"""VT-282 — VTR escalation-rate instrumentation + decay metric.

CL-426: VTR independence is a MEASURED THRESHOLD (confidence + escalation-rate DECAY), not a date —
the decay curve is the signal for when an agent can run on a lighter human net. **Flat decay is a
product bug** ("1 VTR : hundreds" is contingent on decay, not a constant), so this surfaces the
trend per business + per category so a flat/rising curve is visible from day one.

Pure metrics: aggregates `escalations` (counts + timestamps + kind/severity) — NO customer PII (the
table has none; CL-390), NO LLM (Pillar 1), service-role read. Category = `kind` for now; once the
VT-279 `route` column is on main, group additionally by route (knowledge-gap vs authority) — TODO
noted at the GROUP BY.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from orchestrator.graph import get_pool


def escalation_rate_by_category(
    *, tenant_id: str | None = None, window_days: int = 7, now: dt.datetime | None = None
) -> list[dict[str, Any]]:
    """Escalation COUNT per (tenant, kind) in the trailing ``window_days``. now=None → SQL now()."""
    where_tenant = "AND tenant_id = %(tenant)s" if tenant_id else ""
    sql = (
        "SELECT tenant_id, kind, count(*) AS n "
        "FROM escalations "
        "WHERE opened_at > COALESCE(%(now)s::timestamptz, now()) - make_interval(days => %(w)s) "
        f"{where_tenant} "
        "GROUP BY tenant_id, kind ORDER BY tenant_id, kind"
    )
    params: dict[str, Any] = {"now": now, "w": window_days, "tenant": tenant_id}
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        # The privileged pool uses dict_row (graph.py) — access by column name.
        return [
            {"tenant_id": str(r["tenant_id"]), "kind": r["kind"], "count": int(r["n"])}
            for r in cur.fetchall()
        ]


def _trend(recent: int, prior: int) -> str:
    """Decay classification. CL-426: 'declining' is healthy (the agent is learning); 'flat' or
    'rising' is the product-bug signal. No prior baseline + recent activity → 'rising' (no decay yet)."""
    if prior == 0:
        return "rising" if recent > 0 else "flat"
    if recent < prior * 0.8:
        return "declining"
    if recent > prior * 1.2:
        return "rising"
    return "flat"


def escalation_decay(
    *,
    tenant_id: str | None = None,
    recent_days: int = 7,
    prior_days: int = 7,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Per (tenant, kind): the recent-window count vs the immediately-prior window, with a decay
    trend. `healthy` is True ONLY when declining (CL-426 — flat/rising flags an agent that isn't
    learning). now=None → SQL now()."""
    where_tenant = "AND tenant_id = %(tenant)s" if tenant_id else ""
    total = recent_days + prior_days
    sql = (
        "SELECT tenant_id, kind, "
        "  count(*) FILTER (WHERE opened_at > t_now - make_interval(days => %(r)s)) AS recent, "
        "  count(*) FILTER (WHERE opened_at <= t_now - make_interval(days => %(r)s) "
        "                     AND opened_at > t_now - make_interval(days => %(tot)s)) AS prior "
        "FROM escalations, (SELECT COALESCE(%(now)s::timestamptz, now()) AS t_now) b "
        "WHERE opened_at > t_now - make_interval(days => %(tot)s) "
        f"{where_tenant} "
        "GROUP BY tenant_id, kind ORDER BY tenant_id, kind"
    )
    params: dict[str, Any] = {
        "now": now, "r": recent_days, "tot": total, "tenant": tenant_id,
    }
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        recent, prior = int(r["recent"]), int(r["prior"])  # dict_row pool (graph.py)
        trend = _trend(recent, prior)
        out.append(
            {
                "tenant_id": str(r["tenant_id"]),
                "kind": r["kind"],
                "recent": recent,
                "prior": prior,
                "trend": trend,
                "healthy": trend == "declining",
            }
        )
    return out
