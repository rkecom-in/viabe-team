"""VT-196 — L0 production write wiring.

`write_l0_fragment_workflow` is the post-step entry point for the
orchestrator. Async DBOS workflow so the agent's response path is
never blocked. Consent gate per CL-390: tenant.owner_inputs must be
true. Returns shape {'status': 'written'|'rejected_consent'|'error',
'fragment_id': str?, 'observation_count': int?, 'reason': str?}.

Per-tenant k-anonymity admission gate is BRIEF-DEFERRED — see PR
description / follow-up VT row. The existing VT-126 substrate's
read-side k-anonymity (observation_count >= 10) bounds exposure;
write-side admission requires per-tenant contributor tracking which
needs a schema change brief-locked out of this row.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from orchestrator.graph import get_pool
from orchestrator.observability.l0_memory import (
    PiiInContentError,
    write_l0_fragment,
)

logger = logging.getLogger(__name__)


def _owner_inputs_enabled(tenant_id: UUID) -> bool:
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT owner_inputs FROM tenants WHERE id = %s",
            (str(tenant_id),),
        )
        row = cur.fetchone()
    if row is None:
        return False
    val = row["owner_inputs"] if isinstance(row, dict) else row[0]
    return bool(val)


def write_l0_fragment_workflow(
    *,
    tenant_id: str,
    fragment_type: str,
    cohort_key: str,
    content: dict[str, Any],
) -> dict[str, Any]:
    """Consent-gated L0 fragment write.

    Returns:
      - {'status': 'written', 'fragment_id': ..., 'observation_count': ...}
      - {'status': 'rejected_consent', 'reason': 'owner_inputs disabled'}
      - {'status': 'rejected_pii', 'reason': '...'}
      - {'status': 'error', 'reason': repr(exc)}
    """
    try:
        tenant_uuid = UUID(tenant_id)
    except (ValueError, TypeError):
        return {"status": "error", "reason": f"invalid tenant_id: {tenant_id!r}"}

    if not _owner_inputs_enabled(tenant_uuid):
        return {
            "status": "rejected_consent",
            "reason": "tenant.owner_inputs disabled",
        }

    try:
        # VT-225: pass the contributing tenant so per-tenant k-anon admission
        # (l0_cell_contributors) is recorded atomically with the fragment write.
        result = write_l0_fragment(
            fragment_type=fragment_type,
            cohort_key=cohort_key,
            content=content,
            tenant_id=tenant_uuid,
        )
    except PiiInContentError as exc:
        return {"status": "rejected_pii", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 — workflow path must not crash
        logger.exception(
            "write_l0_fragment_workflow failed (tenant=%s, cohort=%s)",
            tenant_id, cohort_key,
        )
        return {"status": "error", "reason": repr(exc)[:200]}

    return {
        "status": "written",
        "fragment_id": result["fragment_id"],
        "observation_count": result["observation_count"],
        "inserted": result["inserted"],
        "contributor_count": result["contributor_count"],
    }
