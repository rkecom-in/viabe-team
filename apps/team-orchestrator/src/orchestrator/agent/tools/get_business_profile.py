"""VT-41 — get_business_profile standalone tool.

Deterministic per-tenant business-profile lookup. Pydantic IO is the
binding contract; standalone callable. NOT wired to an Agent yet
(VT-4 SDK skeleton still Backlog).

Substrate map:
- business_name, business_archetype ← tenants table (business_name,
  business_type)
- locale ← tenants.preferred_language ?? language_preference
- owner_name, working_hours ← columns not in main yet; null gracefully
- integration_summary ← tenant_connector_status.connector_id rows
  (deduped, sorted) — empty list if no connectors
- owner_curated_context ← L1 substrate (VT-195/VT-225), absent in
  main; returns null gracefully. Forward-target schema gap surfaces
  as UndefinedTable → treated as null.

NO PII (CL-390): tenant_id + counts only.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class GetBusinessProfileInput(BaseModel):
    """Tenant id only."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., min_length=1)


class GetBusinessProfileOutput(BaseModel):
    """Resolved business profile shape per brief contract."""

    model_config = ConfigDict(frozen=True)

    business_name: str
    business_archetype: str | None
    owner_name: str | None
    locale: str
    working_hours: str | None
    integration_summary: list[str]
    owner_curated_context: str | None


def _safe_query_undefined(cur: Any, sql: str, params: tuple) -> Any | None:
    """Run query; return cursor result OR None if table absent.

    psycopg raises UndefinedTable; matched by type name to keep this
    module psycopg-free at load time.
    """
    try:
        cur.execute(sql, params)
        return cur
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ != "UndefinedTable":
            raise
        return None


def get_business_profile(
    payload: GetBusinessProfileInput,
    *,
    pool: Any | None = None,
) -> GetBusinessProfileOutput | None:
    """Read business profile for `tenant_id`. Returns None when the
    tenant row is missing (graceful negative outcome — caller treats
    as "tenant not in registry").

    RLS: SET LOCAL app.current_tenant for the duration of the SELECT.
    """
    if pool is None:
        from orchestrator.graph import get_pool

        pool = get_pool()

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SET LOCAL app.current_tenant = %s", (payload.tenant_id,),
            )

            cur.execute(
                """
                SELECT business_name, business_type, preferred_language,
                       language_preference
                FROM tenants
                WHERE id = %s
                LIMIT 1
                """,
                (payload.tenant_id,),
            )
            row = cur.fetchone()
            if row is None:
                logger.info(
                    "get_business_profile: no tenant row (tenant=%s)",
                    payload.tenant_id,
                )
                return None

            def _col(r: Any, key: str, idx: int) -> Any:
                return r[key] if isinstance(r, dict) else r[idx]

            business_name = str(_col(row, "business_name", 0))
            business_archetype = _col(row, "business_type", 1)
            preferred = _col(row, "preferred_language", 2)
            language_pref = _col(row, "language_preference", 3)
            locale = str(preferred or language_pref or "en")

            integrations: list[str] = []
            connector_cur = _safe_query_undefined(
                cur,
                """
                SELECT DISTINCT connector_id
                FROM tenant_connector_status
                WHERE tenant_id = %s
                ORDER BY connector_id
                """,
                (payload.tenant_id,),
            )
            if connector_cur is not None:
                integrations = [
                    str(_col(r, "connector_id", 0))
                    for r in connector_cur.fetchall()
                ]

            owner_curated_context: str | None = None
            # L1 substrate not in main yet (VT-195/VT-225). Probe
            # forward-target table; absent → null gracefully.
            l1_cur = _safe_query_undefined(
                cur,
                """
                SELECT owner_curated_context
                FROM tenant_l1_profile
                WHERE tenant_id = %s
                LIMIT 1
                """,
                (payload.tenant_id,),
            )
            if l1_cur is not None:
                l1_row = l1_cur.fetchone()
                if l1_row is not None:
                    val = _col(l1_row, "owner_curated_context", 0)
                    owner_curated_context = (
                        str(val) if val is not None else None
                    )

            logger.info(
                "get_business_profile: tenant=%s integrations=%d "
                "owner_ctx_present=%s",
                payload.tenant_id, len(integrations),
                owner_curated_context is not None,
            )
            return GetBusinessProfileOutput(
                business_name=business_name,
                business_archetype=(
                    str(business_archetype)
                    if business_archetype is not None else None
                ),
                owner_name=None,  # column absent
                locale=locale,
                working_hours=None,  # column absent
                integration_summary=integrations,
                owner_curated_context=owner_curated_context,
            )


__all__ = [
    "GetBusinessProfileInput",
    "GetBusinessProfileOutput",
    "get_business_profile",
]
