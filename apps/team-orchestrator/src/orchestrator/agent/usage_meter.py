"""VT-619 — per-tenant × per-agent LLM token/API-call METERING + config-driven LIMITS.

The Team-Manager is NOT a separate billed agent: every LLM call is TAGGED with the agent it
SERVES. A manager turn that routes to (or serves) sales_recovery bills ``sales_recovery``; a
specialist execution turn bills that specialist. Enforcement meters on RAW counts (api_calls +
tokens_in + tokens_out per tenant/agent/month) — rate-independent; ₹ is derived downstream for ops
display only.

Two seams feed ``meter_llm_call``:
  * the langchain ChatAnthropic callback (manager + integration + onboarding_conductor turns), and
  * the Anthropic Messages-SDK callback (sales_recovery's executor — Messages SDK, not ChatAnthropic).
These are DISJOINT LLM-call populations, so a call is metered exactly once (no double-count).

Best-effort discipline (CL-122): metering MUST NEVER abort a live owner turn. Every public entry
wraps its body in a swallow — a metering blip degrades to a logged warning, never an exception into
the turn. Reads (``budget_status``) fail OPEN (never block a paying owner on a metering read error).

Import hygiene: every orchestrator import is LAZY (inside a function) so this module imports with no
langgraph/dbos/langchain dependency — the maps derive from the ROSTER at first use (a future
specialist spec auto-registers; nothing is hardcoded).
"""

from __future__ import annotations

import functools
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Fallback agent when a turn cannot be attributed to a specific billed agent (see
# ``tenant_primary_agent``). The DEFAULT caps row is a caps-lookup fallback only — never a
# usage ``agent`` value.
_DEFAULT_CAPS_KEY = "DEFAULT"


def _field(row: Any, key: str, idx: int) -> Any:
    """Read a column from a psycopg row that may be a dict (dict_row pool) or a tuple."""
    if row is None:
        return None
    return row[key] if isinstance(row, dict) else row[idx]


@functools.lru_cache(maxsize=1)
def _billed_maps() -> tuple[dict[str, str], dict[str, str]]:
    """Derive (node -> billed slug, spawn_tool -> billed slug) from the ROSTER.

    billed slug = ``agent_name.removesuffix('_agent')`` — 'sales_recovery_agent' -> 'sales_recovery',
    'integration_agent' -> 'integration', 'onboarding_conductor' -> 'onboarding_conductor'. A new
    SpecialistSpec appended to ROSTER auto-registers here with no edit. Lazy + cached so importing
    this module pulls no roster/langchain deps until a meter actually fires.
    """
    from orchestrator.agent.roster import ROSTER

    node_to_billed = {spec.agent_name: spec.agent_name.removesuffix("_agent") for spec in ROSTER}
    spawn_to_billed = {
        spec.spawn_tool_name: spec.agent_name.removesuffix("_agent") for spec in ROSTER
    }
    return node_to_billed, spawn_to_billed


def billed_agent_for_node(node: str | None) -> str | None:
    """The billed slug for a graph node (specialist turn), or None for the manager/unknown node."""
    if not node:
        return None
    try:
        return _billed_maps()[0].get(node)
    except Exception:  # noqa: BLE001 — attribution is best-effort; a miss falls through
        logger.warning("VT-619 billed_agent_for_node failed", exc_info=True)
        return None


def _billed_from_ns(checkpoint_ns: str | None) -> str | None:
    """Resolve a specialist slug from a langgraph_checkpoint_ns path.

    A specialist added as a SUB-GRAPH node reports ``langgraph_node`` = the INNER node name (e.g.
    'inner_llm'), NOT the roster agent_name — but the parent agent_name IS a segment of the
    checkpoint namespace, e.g. ``integration_agent:<uuid>|inner_llm:<uuid>`` (verified empirically).
    So an execution turn inside a specialist sub-graph is attributed by matching any ns segment's
    node-name against the roster (the manager node's ns is just 'orchestrator_agent:<uuid>', which
    matches nothing here → falls through to the spawn-scan / fallback).
    """
    if not checkpoint_ns:
        return None
    try:
        node_to_billed, _ = _billed_maps()
    except Exception:  # noqa: BLE001
        return None
    for part in checkpoint_ns.split("|"):
        name = part.split(":", 1)[0]
        if name in node_to_billed:
            return node_to_billed[name]
    return None


# --------------------------------------------------------------------------- #
# Attribution: which agent does THIS LLM call serve?
# --------------------------------------------------------------------------- #


def _scan_spawn_target(response: Any) -> str | None:
    """Scan a langchain LLMResult for a spawn_* tool_call → its billed slug (first match wins).

    A manager turn that ROUTES to a specialist fires that specialist's spawn tool on THIS turn; the
    turn is billed to the route target (design: a manager turn serving sales_recovery bills
    sales_recovery). Reads ``response.generations[0][0].message.tool_calls`` (langchain's normalized
    list[dict] with 'name') first, then falls back to ``.additional_kwargs['tool_calls']`` (the raw
    provider shape). Every attribute access is guarded — response shape varies across versions.
    """
    try:
        _, spawn_to_billed = _billed_maps()
    except Exception:  # noqa: BLE001
        return None
    try:
        gens = getattr(response, "generations", None)
        if not gens or not gens[0]:
            return None
        msg = getattr(gens[0][0], "message", None)
        if msg is None:
            return None
        for tc in getattr(msg, "tool_calls", None) or []:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            billed = spawn_to_billed.get(name or "")
            if billed:
                return billed
        ak = getattr(msg, "additional_kwargs", None) or {}
        for tc in ak.get("tool_calls") or []:
            name = None
            if isinstance(tc, dict):
                name = tc.get("name") or (tc.get("function") or {}).get("name")
            billed = spawn_to_billed.get(name or "")
            if billed:
                return billed
    except Exception:  # noqa: BLE001 — attribution best-effort; on any miss fall through
        return None
    return None


def resolve_billed_agent(
    node: str | None, response: Any, tenant_id: Any, checkpoint_ns: str | None = None
) -> str:
    """Resolve which billed agent THIS LLM call serves.

    1. ``node`` is a specialist graph node → that slug (a specialist execution turn). A specialist
       run as a SUB-GRAPH reports the inner node name in ``node`` but its agent_name in
       ``checkpoint_ns`` — both are checked (see ``_billed_from_ns``).
    2. else (manager / orchestrator / None): scan this turn's tool_calls for a spawn_* → the route
       target's slug (a manager turn that routes to a specialist bills the specialist).
    3. else: the tenant's primary billed agent (fallback — a pure conversational/status turn).

    ``checkpoint_ns`` (VT-619, additive) is the langgraph checkpoint namespace stashed at LLM start;
    it makes sub-graph specialist turns attributable (langgraph_node alone is the inner node name).
    """
    billed = billed_agent_for_node(node)
    if billed is not None:
        return billed
    billed = _billed_from_ns(checkpoint_ns)
    if billed is not None:
        return billed
    target = _scan_spawn_target(response)
    if target is not None:
        return target
    return tenant_primary_agent(tenant_id)


def tenant_primary_agent(tenant_id: Any, conn: Any = None) -> str:
    """The tenant's primary BILLED agent — the fallback attribution for an un-routed manager turn.

    tenants has NO primary_agent column (verified), and sales_recovery is the ONLY billed specialist
    today, so this returns 'sales_recovery'. Kept as a function so the call sites don't change when
    multiple billed agents exist.

    TODO(VT-619): when >1 billed specialist exists, resolve to the tenant's single ENABLED billed
    agent (or an explicit primary) — likely via a tenants.primary_agent column or a per-tenant
    enablement read on ``conn``.
    """
    return "sales_recovery"


# --------------------------------------------------------------------------- #
# Caps
# --------------------------------------------------------------------------- #


def effective_caps(
    agent: str, usage_row: Any, conn: Any = None
) -> tuple[int, int, int, int, bool]:
    """Return (max_api_calls, max_tokens_in, max_tokens_out, soft_pct, enabled) for ``agent``.

    Reads the base envelope from ``agent_cost_limits`` (falling back to the 'DEFAULT' row when the
    agent has no explicit caps row) and ADDS the per-period ``topup_*`` from ``usage_row`` (a top-up
    raises the ceiling for this period). ``conn`` is the caller's RLS-scoped connection (every caller
    — budget_status / _maybe_soft_notify — already holds one and passes it, so the read reuses that
    single connection). On a total miss (no row + no DEFAULT) fails OPEN with an effectively-infinite
    envelope + enabled=False so nothing ever blocks.
    """

    def _run(c: Any) -> tuple[int, int, int, int, bool]:
        row = c.execute(
            "SELECT max_api_calls, max_tokens_in, max_tokens_out, soft_pct, enabled "
            "FROM agent_cost_limits WHERE agent = %s",
            (agent,),
        ).fetchone()
        if row is None:
            row = c.execute(
                "SELECT max_api_calls, max_tokens_in, max_tokens_out, soft_pct, enabled "
                "FROM agent_cost_limits WHERE agent = %s",
                (_DEFAULT_CAPS_KEY,),
            ).fetchone()
        if row is None:
            # No caps configured at all — fail open (never block).
            return (2**31 - 1, 2**62, 2**62, 80, False)
        base_calls = int(_field(row, "max_api_calls", 0) or 0)
        base_in = int(_field(row, "max_tokens_in", 1) or 0)
        base_out = int(_field(row, "max_tokens_out", 2) or 0)
        soft_pct = int(_field(row, "soft_pct", 3) or 80)
        enabled = bool(_field(row, "enabled", 4))
        top_calls = int(_field(usage_row, "topup_api_calls", 3) or 0)
        top_in = int(_field(usage_row, "topup_tokens_in", 4) or 0)
        top_out = int(_field(usage_row, "topup_tokens_out", 5) or 0)
        return (
            base_calls + top_calls,
            base_in + top_in,
            base_out + top_out,
            soft_pct,
            enabled,
        )

    if conn is None:
        # Every real caller passes its RLS-scoped conn; a None here is a programming error, not a
        # runtime condition. Kept explicit so a caller can't silently open a nested connection while
        # already holding one (which would deadlock the single-connection pool path).
        raise RuntimeError("effective_caps requires an open conn")
    return _run(conn)


# The topup_* columns share a row shape between the UPSERT RETURNING and the usage SELECT; the
# _field idx map below documents that shared layout (see _USAGE_COLS).
_USAGE_COLS = (
    "api_calls",
    "tokens_in",
    "tokens_out",
    "topup_api_calls",
    "topup_tokens_in",
    "topup_tokens_out",
)


def _zero_usage() -> dict[str, Any]:
    return {c: 0 for c in _USAGE_COLS}


def budget_status(tenant_id: Any, agent: str, conn: Any = None) -> dict[str, Any]:
    """The current-month budget posture for (tenant, agent).

    Returns ``{pct_calls, pct_in, pct_out, over_soft, over_hard, enabled}``. ``over_hard`` = enabled
    AND any raw counter >= its effective cap (base + topup). ``over_soft`` = enabled AND any counter
    >= soft_pct% of its effective cap AND NOT over_hard. FAILS OPEN on ANY read error (never block a
    paying owner on a metering blip): returns over_soft/over_hard/enabled all False.
    """

    def _run(c: Any) -> dict[str, Any]:
        row = c.execute(
            "SELECT api_calls, tokens_in, tokens_out, "
            "       topup_api_calls, topup_tokens_in, topup_tokens_out "
            "FROM tenant_agent_usage "
            "WHERE tenant_id = %s AND agent = %s "
            "  AND period_month = date_trunc('month', now())::date",
            (str(tenant_id), agent),
        ).fetchone()
        usage = row if row is not None else _zero_usage()
        max_calls, max_in, max_out, soft_pct, enabled = effective_caps(agent, usage, conn=c)
        calls = int(_field(usage, "api_calls", 0) or 0)
        t_in = int(_field(usage, "tokens_in", 1) or 0)
        t_out = int(_field(usage, "tokens_out", 2) or 0)
        over_hard = enabled and (
            calls >= max_calls or t_in >= max_in or t_out >= max_out
        )
        soft_calls = max_calls * soft_pct / 100.0
        soft_in = max_in * soft_pct / 100.0
        soft_out = max_out * soft_pct / 100.0
        over_soft = (
            enabled
            and not over_hard
            and (calls >= soft_calls or t_in >= soft_in or t_out >= soft_out)
        )
        return {
            "pct_calls": (calls / max_calls * 100.0) if max_calls else 0.0,
            "pct_in": (t_in / max_in * 100.0) if max_in else 0.0,
            "pct_out": (t_out / max_out * 100.0) if max_out else 0.0,
            "over_soft": bool(over_soft),
            "over_hard": bool(over_hard),
            "enabled": bool(enabled),
        }

    try:
        if conn is not None:
            return _run(conn)
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as c:
            return _run(c)
    except Exception:  # noqa: BLE001 — fail OPEN (never block a paying owner on a read blip)
        logger.warning("VT-619 budget_status read failed; failing open", exc_info=True)
        return {
            "pct_calls": 0.0,
            "pct_in": 0.0,
            "pct_out": 0.0,
            "over_soft": False,
            "over_hard": False,
            "enabled": False,
        }


# --------------------------------------------------------------------------- #
# The meter (write path)
# --------------------------------------------------------------------------- #

_UPSERT_SQL = (
    "INSERT INTO tenant_agent_usage "
    "  (tenant_id, agent, period_month, api_calls, tokens_in, tokens_out, updated_at) "
    "VALUES (%s, %s, date_trunc('month', now())::date, 1, %s, %s, now()) "
    "ON CONFLICT (tenant_id, agent, period_month) DO UPDATE SET "
    "  api_calls  = tenant_agent_usage.api_calls  + 1, "
    "  tokens_in  = tenant_agent_usage.tokens_in  + EXCLUDED.tokens_in, "
    "  tokens_out = tenant_agent_usage.tokens_out + EXCLUDED.tokens_out, "
    "  updated_at = now() "
    "RETURNING api_calls, tokens_in, tokens_out, "
    "          topup_api_calls, topup_tokens_in, topup_tokens_out, soft_notified_at"
)


def meter_llm_call(
    *,
    tenant_id: Any,
    agent: str | None,
    tokens_in: int,
    tokens_out: int,
    conn: Any = None,
) -> None:
    """Best-effort UPSERT of one LLM call's raw usage into (tenant, agent, current month).

    api_calls += 1, tokens_in/out += this call. If ``agent`` is falsy → skip (nothing to attribute).
    If ``conn`` is None a tenant_connection is self-opened (mirrors incident_store's best-effort
    writers). The ENTIRE body is swallowed (CL-122): metering must NEVER break a turn. After the
    UPSERT, the soft-notify check runs on the RETURNING counters (also inside the swallow).
    """
    if not agent:
        return

    def _run(c: Any) -> None:
        row = c.execute(
            _UPSERT_SQL,
            (str(tenant_id), agent, int(tokens_in or 0), int(tokens_out or 0)),
        ).fetchone()
        if row is None:
            return
        _maybe_soft_notify(c, tenant_id=tenant_id, agent=agent, usage_row=row)

    try:
        if conn is not None:
            _run(conn)
            return
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as c:
            _run(c)
    except Exception:  # noqa: BLE001 — CL-122: metering never breaks a turn
        logger.warning("VT-619 meter_llm_call swallowed (best-effort)", exc_info=True)


def _maybe_soft_notify(c: Any, *, tenant_id: Any, agent: str, usage_row: Any) -> None:
    """Emit the ONE soft-threshold incident for this (tenant, agent, period), if just crossed.

    Guards: already-notified (``soft_notified_at`` set) → skip; agent unbilled (enabled=false) →
    skip; not over_soft → skip. The stamp UPDATE is ``WHERE soft_notified_at IS NULL RETURNING`` so
    it is atomic + once-per-period even under concurrent callbacks (only one UPDATE returns a row →
    only one incident). Fully guarded — a notify failure never touches the metered write above.
    """
    if _field(usage_row, "soft_notified_at", 6) is not None:
        return
    max_calls, max_in, max_out, soft_pct, enabled = effective_caps(agent, usage_row, conn=c)
    if not enabled:
        return
    calls = int(_field(usage_row, "api_calls", 0) or 0)
    t_in = int(_field(usage_row, "tokens_in", 1) or 0)
    t_out = int(_field(usage_row, "tokens_out", 2) or 0)
    over_hard = calls >= max_calls or t_in >= max_in or t_out >= max_out
    if over_hard:
        # The hard incident is emitted at the enforcement gate (customer_send_choke) via
        # hard_notified_at — not here. Nothing to soft-notify once already hard.
        return
    over_soft = (
        calls >= max_calls * soft_pct / 100.0
        or t_in >= max_in * soft_pct / 100.0
        or t_out >= max_out * soft_pct / 100.0
    )
    if not over_soft:
        return
    stamped = c.execute(
        "UPDATE tenant_agent_usage SET soft_notified_at = now() "
        "WHERE tenant_id = %s AND agent = %s "
        "  AND period_month = date_trunc('month', now())::date "
        "  AND soft_notified_at IS NULL RETURNING agent",
        (str(tenant_id), agent),
    ).fetchone()
    if stamped is None:
        return  # a concurrent callback already stamped + notified
    pct = max(
        (calls / max_calls * 100.0) if max_calls else 0.0,
        (t_in / max_in * 100.0) if max_in else 0.0,
        (t_out / max_out * 100.0) if max_out else 0.0,
    )
    from orchestrator.observability.incident_store import create_incident

    create_incident(
        tenant_id,
        incident_kind="limit_exhausted",
        severity="warning",
        detail={"agent": agent, "phase": "soft", "pct": round(pct, 1)},
        conn=c,
    )


# --------------------------------------------------------------------------- #
# Ops read (owner-dashboard / ops console)
# --------------------------------------------------------------------------- #


def get_agent_usage_breakdown(tenant_id: Any) -> list[dict[str, Any]]:
    """Per-agent current-month usage vs caps for the ops/owner surface.

    Driven off ``agent_cost_limits`` (every configured agent, LEFT JOIN this month's usage) so an
    agent with a limits row shows even at zero usage. Effective caps COALESCE the per-period topup.
    ``billed = enabled`` — the tracked-UNBILLED setup agents (integration / onboarding_conductor,
    enabled=false) come through so the ops page can surface them SEPARATELY from the billed ones.
    """
    from orchestrator.db import tenant_connection

    try:
        with tenant_connection(tenant_id) as c:
            rows = c.execute(
                "SELECT l.agent, "
                "       COALESCE(u.api_calls, 0)  AS api_calls, "
                "       COALESCE(u.tokens_in, 0)  AS tokens_in, "
                "       COALESCE(u.tokens_out, 0) AS tokens_out, "
                "       l.max_api_calls  + COALESCE(u.topup_api_calls, 0)  AS max_api_calls, "
                "       l.max_tokens_in  + COALESCE(u.topup_tokens_in, 0)  AS max_tokens_in, "
                "       l.max_tokens_out + COALESCE(u.topup_tokens_out, 0) AS max_tokens_out, "
                "       l.enabled "
                "FROM agent_cost_limits l "
                "LEFT JOIN tenant_agent_usage u "
                "  ON u.agent = l.agent AND u.tenant_id = %s "
                "  AND u.period_month = date_trunc('month', now())::date "
                "ORDER BY l.agent",
                (str(tenant_id),),
            ).fetchall()
    except Exception:  # noqa: BLE001 — read surface: degrade to empty, never raise into the API
        logger.warning("VT-619 get_agent_usage_breakdown read failed", exc_info=True)
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        api_calls = int(_field(r, "api_calls", 1) or 0)
        t_in = int(_field(r, "tokens_in", 2) or 0)
        t_out = int(_field(r, "tokens_out", 3) or 0)
        max_calls = int(_field(r, "max_api_calls", 4) or 0)
        max_in = int(_field(r, "max_tokens_in", 5) or 0)
        max_out = int(_field(r, "max_tokens_out", 6) or 0)
        enabled = bool(_field(r, "enabled", 7))
        out.append(
            {
                "agent": _field(r, "agent", 0),
                "api_calls": api_calls,
                "tokens_in": t_in,
                "tokens_out": t_out,
                "max_api_calls": max_calls,
                "max_tokens_in": max_in,
                "max_tokens_out": max_out,
                "pct_calls": (api_calls / max_calls * 100.0) if max_calls else 0.0,
                "pct_in": (t_in / max_in * 100.0) if max_in else 0.0,
                "pct_out": (t_out / max_out * 100.0) if max_out else 0.0,
                "enabled": enabled,
                "billed": enabled,
            }
        )
    return out


__all__ = [
    "billed_agent_for_node",
    "budget_status",
    "effective_caps",
    "get_agent_usage_breakdown",
    "meter_llm_call",
    "resolve_billed_agent",
    "tenant_primary_agent",
]
