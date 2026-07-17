"""The COMMON READ tools — the Manager-scoped operational-data reads a specialist pulls itself.

WHAT THIS IS
------------
ARCHITECTURE.md §1.3 ("READ tools — DB/context reads … always scoped to the Manager's resolved
tenant") + §1.1 ("the specialist pulls operational data itself via Manager-scoped READ tools").
The Manager gathers only the FRAMING data (situation / desired_outcome / context_slice); the
operational datum a specialist needs — the ledger shape, the business profile, the integration
phase — it pulls through these three registered READ tools rather than the Manager pre-fetching
everything (which forces over- or under-fetch, §1.1).

Each is a langchain ``@tool`` wrapping an EXISTING read implementation — it re-expresses a real
reader on the tool contract, it does NOT re-author the business logic:

  - ``read_customer_ledger_summary`` → ``db.wrappers.CustomersWrapper`` counts
    (``count_all`` / ``count_with_sales`` / ``count_lapsed`` at the CL-2026-07-10 45-day
    ``LAPSED_WINDOW_DAYS`` definition — the SAME function the owner-facing status count uses).
  - ``read_business_context``        → ``knowledge.business_context.read_business_context`` (the
    manager's own §7 business-context READ seam, the one ``dispatch.py`` composes over).
  - ``read_integration_state``       → ``onboarding.shopify_onboarding.read_integration_state``
    (the SAME seam the integration agent's own ``read_integration_state`` tool delegates to —
    imported, never duplicated).

THE THREE INVARIANTS every tool here holds
------------------------------------------
1. RESOLVE-FIRST, MODEL-UNTRUSTED (§1.1 / §3 / the VT-293/294/599 IDOR guard): the first line is
   ``resolve_lane_tenant(tenant_id, tool_name=…)`` — the ambient dispatch ``ObservabilityContext``
   WINS; a model-supplied ``tenant_id`` that disagrees is logged + ignored. An unresolvable tenant
   returns the structured ``lane_tenant_error`` dict — NEVER a raise (a raise inside a lane-driven
   tool orphans the tool_use / hangs the run, the VT-599 defect).
2. OWN RLS SCOPE, NEVER A PASSED CONNECTION (§3): a DB tool takes the RESOLVED tenant and opens its
   OWN ``tenant_connection`` (or delegates to a reader that does). It never accepts a ``conn``
   argument and never touches a raw/BYPASSRLS pool — the §3 DB-access rule. ``read_customer_ledger_
   summary`` opens ONE ``tenant_connection(resolved)`` and threads it through the wrapper counts so
   the whole summary is one RLS-scoped, single-checkout read; the other two delegate to readers
   that each open their own ``tenant_connection``. (The §3 DB-access INVERSION — the tool taking a
   Manager-owned session — is explicitly DEFERRED; this is today's sanctioned pattern.)
3. CL-390 PII-SAFE: a READ tool return carries COUNTS / IDS / STATUSES / the owner's own business
   fields ONLY — never a customer name/phone/email. ``read_customer_ledger_summary`` returns pure
   integers; ``read_business_context`` returns the owner's own business data (identity/profile/
   objective — not customer PII) + a boolean for L1 presence (the rendered block is not dumped);
   ``read_integration_state`` returns the phase + connector id + a pending-waypoint envelope that
   by construction carries only connector/field-mapping identifiers, never raw customer PII.

IMPORT SURFACE (deliberate — this is a langchain-carrying module, like ``agent/integration_agent``)
---------------------------------------------------------------------------------------------------
Defining ``@tool`` objects at module load pulls ``langchain_core`` (exactly as the integration
agent's own tool module does), so THIS file is NOT dep-less-smoke safe and is imported LAZILY by
``CommonToolsModule`` (at instance construction) and by any wiring seam — never by
``agent_framework/__init__`` (whose import surface stays inert + dep-less). The ``orchestrator.agent``
re-exports it leans on (``lane_tenant`` / ``tool_guardrail``) are light: ``agent/__init__`` is a PEP
562 lazy shim, so those imports pull no model build. The DB/knowledge/onboarding readers are
LAZY-imported INSIDE each tool (psycopg / KG chains kept off even this module's import surface),
mirroring the lane-tool discipline.

INERT: nothing live reads through these yet. Wiring the Manager / specialist live paths to
``COMMON_READ_TOOLS`` is a deliberate follow-on, not done here.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from orchestrator.agent.lane_tenant import lane_tenant_error, resolve_lane_tenant
from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

logger = logging.getLogger("orchestrator.agent_framework.tools_common")


@tool
def read_customer_ledger_summary(tenant_id: str) -> dict[str, Any]:
    """Read a COUNTS-ONLY summary of this tenant's customer ledger — no names, no phones (CL-390).

    Returns aggregate integers a specialist (or the Manager) uses to frame a decision without
    pulling any customer PII:
      - ``total_customers``       — every customer row for the tenant.
      - ``customers_with_sales``  — customers with >=1 'sale' ledger entry (the ACTIVE base). This
        distinguishes an EMPTY ledger (no sales data at all) from a real "0 lapsed of N", so a
        ``lapsed_count`` of 0 is never mis-read as "everyone bought recently" (VT-632).
      - ``lapsed_count``          — customers who USED to buy but have had no 'sale' in the last
        ``lapsed_window_days`` (Fazal's canonical 45-day ``LAPSED_WINDOW_DAYS`` definition,
        CL-2026-07-10). This is the SAME ``count_lapsed`` the owner-facing status metric AND the
        Sales-Recovery send cohort use — the number the owner hears IS the set a campaign targets.
      - ``lapsed_window_days``    — the window used (grounds the ``lapsed_count`` claim).

    Tenant is resolved from the ambient run context (the model-supplied ``tenant_id`` is untrusted
    and only used as a fallback when there is no ambient context); an unresolvable tenant returns a
    structured error dict, never a raise.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="read_customer_ledger_summary")
    if resolved is None:
        return lane_tenant_error("read_customer_ledger_summary")

    # Lazy: the wrapper + connection pull psycopg — kept off this module's import surface.
    from orchestrator.db import tenant_connection
    from orchestrator.db.wrappers import LAPSED_WINDOW_DAYS, CustomersWrapper

    customers = CustomersWrapper()
    try:
        # ONE RLS-scoped connection for the whole summary (§3: the tool opens its OWN
        # tenant_connection for the resolved tenant; the wrapper counts run on it so the summary is
        # a single consistent, single-checkout read — the conn= path is atomic composition, VT-306).
        with tenant_connection(resolved) as conn:
            total = customers.count_all(resolved, conn=conn)
            with_sales = customers.count_with_sales(resolved, conn=conn)
            lapsed = customers.count_lapsed(resolved, days=LAPSED_WINDOW_DAYS, conn=conn)
    except Exception as exc:  # noqa: BLE001 — a lane tool must never RAISE (would orphan the tool_use)
        logger.warning(
            "read_customer_ledger_summary: ledger read failed (tenant=%s): %s", resolved, exc
        )
        return {"status": "error", "error": "read_customer_ledger_summary: ledger read failed"}

    return {
        "total_customers": total,
        "customers_with_sales": with_sales,
        "lapsed_count": lapsed,
        "lapsed_window_days": LAPSED_WINDOW_DAYS,
    }


@tool
def read_business_context(tenant_id: str) -> dict[str, Any]:
    """Read this tenant's business-context summary — the owner's OWN business data, no customer PII.

    Delegates to the manager's §7 business-context READ seam (the same
    ``knowledge.business_context.read_business_context`` that ``dispatch.py`` composes over) and
    returns a summary of its structured fields:
      - ``identity``   — the tenant-row identity the reasoner needs (verified business name,
        business_type, phase, GST status/verified flag). Owner's own data; read-only.
      - ``profile``    — the structured ``business_profile`` attributes (archetype / hours /
        integration map / communication prefs). Owner's own data.
      - ``objective``  — the manager-held cross-turn ``business_objective`` (goals / decisions /
        learnings). Manager/owner-authored business context, NOT customer PII (CL-390).
      - ``has_l1_context`` — whether an L1 system block exists for the tenant (a boolean summary;
        the rendered block itself is not dumped through this read tool).

    Tenant is resolved from the ambient run context (model ``tenant_id`` untrusted); an unresolvable
    tenant returns a structured error dict, never a raise.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="read_business_context")
    if resolved is None:
        return lane_tenant_error("read_business_context")

    # Lazy: the KG read chain is heavy — kept off this module's import surface. The reader is
    # RLS-scoped throughout (every section flows through its own ``tenant_connection``).
    from orchestrator.knowledge.business_context import (
        read_business_context as _read_business_context,
    )

    try:
        bc = _read_business_context(resolved)
    except Exception as exc:  # noqa: BLE001 — never raise inside a lane-driven tool
        logger.warning(
            "read_business_context: business-context read failed (tenant=%s): %s", resolved, exc
        )
        return {"status": "error", "error": "read_business_context: read failed"}

    return {
        "identity": dict(bc.identity),
        "profile": dict(bc.profile),
        "objective": dict(bc.objective),
        "has_l1_context": bc.l1_block is not None,
    }


@tool
def read_integration_state(tenant_id: str) -> dict[str, Any]:
    """Read this tenant's CURRENT integration/onboarding phase + pending waypoint.

    DELEGATES to the SAME reader the integration agent's own ``read_integration_state`` tool uses
    (``onboarding.shopify_onboarding.read_integration_state``) — this is the common-surface
    re-expression of that read, not a duplicate implementation. Returns
    ``{"phase": ..., "current_connector_id": ..., "pending_owner_input": {...} | None}`` or the
    all-``None`` shape when no onboarding has started yet.

    No customer PII: ``pending_owner_input`` only ever carries connector ids, spreadsheet/tab
    identifiers, and confirmed field-mapping labels — never a raw customer phone/email/name.

    Tenant is resolved from the ambient run context (model ``tenant_id`` untrusted); an unresolvable
    tenant returns a structured error dict, never a raise.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="read_integration_state")
    if resolved is None:
        return lane_tenant_error("read_integration_state")

    # Lazy: the onboarding read pulls psycopg — kept off this module's import surface. The reader
    # opens its OWN ``tenant_connection`` (§3): this tool passes it the resolved tenant only.
    from orchestrator.onboarding.shopify_onboarding import (
        read_integration_state as _read_integration_state,
    )

    try:
        state = _read_integration_state(resolved)
    except Exception as exc:  # noqa: BLE001 — never raise inside a lane-driven tool
        logger.warning(
            "read_integration_state: integration-state read failed (tenant=%s): %s", resolved, exc
        )
        return {"status": "error", "error": "read_integration_state: read failed"}

    if state is None:
        return {"phase": None, "current_connector_id": None, "pending_owner_input": None}
    return dict(state)


#: The common READ tools, in a stable order — the surface a Manager/specialist drives to pull
#: operational data (ARCHITECTURE.md §1.1/§1.3). These are the whole point of this module; the
#: Manager holds them on its shelf and a specialist reaches them through the Manager's resolved
#: scope. Kept as a tuple (immutable surface).
COMMON_READ_TOOLS: tuple[Any, ...] = (
    read_customer_ledger_summary,
    read_business_context,
    read_integration_state,
)

# Fail-CLOSED at import: these are READS and MUST pass the deny-list guard (they hold no
# send/ledger-write/accounts/config-write substring). Runs the same ``assert_agent_tools_safe`` a
# module registration would run over this surface — so a future edit that renames one of these into
# a forbidden capability trips at import, not silently at a live wiring seam (VT-268).
assert_agent_tools_safe(COMMON_READ_TOOLS, surface="common_read_tools")


__all__ = [
    "COMMON_READ_TOOLS",
    "read_business_context",
    "read_customer_ledger_summary",
    "read_integration_state",
]
