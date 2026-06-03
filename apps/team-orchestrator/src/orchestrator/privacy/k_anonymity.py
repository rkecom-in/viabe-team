"""VT-74 — k-anonymity admission gate (Pillar 6 build-time invariant).

THE single sanctioned cross-tenant read (Pillar 8, Cowork-approved 20260603T195500Z).
k-anonymity counts TENANTS across the whole workspace, so it cannot run inside one
tenant's RLS scope — it deliberately uses the service-role pool (``get_pool()``,
BYPASSRLS). This is a known, audited exception, NOT a tenant-isolation breach:

- It returns ONLY ``{admitted, tenant_count, eligible_tenant_ids, reason}`` —
  never customer PII, never tenant business data, never row content. Just tenant
  UUIDs + a count.
- ``eligible_tenant_ids`` stay in-process for the L3 construction caller (VT-68).
  They are NEVER logged, persisted, or returned outside that caller (CL-390).
- It does NOT call ``assert_tenant_scoped`` — cross-tenant is the point, so the
  VT-79 Detector-1 ``tenant_isolation_breach`` path must not be invoked (it would
  be a false alarm). The cross-tenant access is sanctioned here and nowhere else.
- It touches ONLY the ``tenants`` table (not a VT-72-watched hot table), so the
  ``no-direct-tenant-db-access`` lint does not flag it; it is allowlisted anyway
  (belt-and-suspenders + future-proofing) with this rationale.

``k_min ≥ 10`` is a LOCKED Type-3 commitment (CL-28, concept doc §10): an
``assert`` blocks any lower value. Raising k_min is always allowed.

The Phase-1 predicate allowlist is structural (business_type, city_tier,
recency_band, signed_up_before). ``city`` / ``locality`` / ``phone`` / any
per-customer field is rejected (``predicate_invalid``) — they would defeat the
anonymity guarantee. ``signed_up_before`` is required: it IS the 180-day
quarantine (VT-69) baked into the predicate.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, ValidationError

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)

# CL-28 / concept doc §10 — k≥10 is a LOCKED Type-3 commitment. Lowering is
# forbidden; the assert in check_admission enforces it.
K_MIN_FLOOR = 10

# Safety bound on the returned tenant set. Admission only needs >= k_min; this
# caps the in-memory list for a pathologically broad predicate (Type-2 governance
# if a real cohort ever exceeds it).
_TENANT_QUERY_CAP = 10_000

AdmissionReason = Literal["admitted", "below_k_min", "predicate_invalid"]


class CohortPredicate(BaseModel):
    """Structured cohort filter. ``extra='forbid'`` rejects any field outside the
    Phase-1 allowlist (city / locality / phone / per-customer) — adding a field is
    Type-2 governance. All four fields required; ``signed_up_before`` is the
    quarantine boundary.

    NOTE: ``recency_band`` is a PASS-THROUGH for VT-68's downstream per-customer
    aggregation — it does NOT filter tenant eligibility (a tenant isn't "dormant";
    customers are). Tenant-eligibility SQL keys on business_type + city_tier +
    signed_up_before only (Cowork ruling 20260603T195500Z, flag 1).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    business_type: str
    city_tier: str
    recency_band: str
    signed_up_before: datetime


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    admitted: bool
    tenant_count: int
    reason: AdmissionReason
    # In-process only — NEVER logged/persisted/returned outside the L3 caller.
    eligible_tenant_ids: list[UUID] = field(default_factory=list)


def _predicate_hash(p: CohortPredicate) -> str:
    """Stable, PII-free hash of the predicate for the audit log."""
    canon = f"{p.business_type}|{p.city_tier}|{p.recency_band}|{p.signed_up_before.isoformat()}"
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _audit(run_id: UUID, predicate_hash: str, k_min: int, tenant_count: int, admitted: bool) -> None:
    """Append a workspace-level (tenant_id NULL) k_anonymity_check row to
    pipeline_log via the service role. Carries the hash/count/decision ONLY —
    NEVER eligible_tenant_ids (CL-390). Best-effort: never fails the gate."""
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO pipeline_log (run_id, tenant_id, event_type, severity, component, payload) "
                "VALUES (%s, NULL, 'k_anonymity_check', 'info', 'k_anonymity', %s)",
                (
                    str(run_id),
                    Jsonb({
                        "predicate_hash": predicate_hash,
                        "k_min": k_min,
                        "tenant_count": tenant_count,
                        "admitted": admitted,
                    }),
                ),
            )
    except Exception:  # noqa: BLE001 — audit is best-effort; never block admission
        logger.exception("VT-74 k_anonymity audit log failed (hash=%s)", predicate_hash)


def check_admission(
    cohort_predicate: CohortPredicate | dict[str, Any],
    k_min: int = 10,
    *,
    run_id: UUID | None = None,
) -> AdmissionResult:
    """Admit/reject a candidate cohort under k-anonymity. Returns the eligible
    tenant set for the caller to aggregate.

    - ``k_min`` defaults to 10 and is asserted ``>= 10`` (CL-28 — lowering is a
      Type-3 violation; raising for a more sensitive aggregation is fine).
    - A missing ``signed_up_before`` or any forbidden field → ``predicate_invalid``
      (returned, not raised).
    - Below k_min → ``below_k_min`` (a frequent, non-concerning rejection).
    """
    assert k_min >= K_MIN_FLOOR, (  # CL-28 locked floor — never lower than 10
        f"k_anonymity: k_min={k_min} below the locked floor {K_MIN_FLOOR} (CL-28)"
    )

    # Validate the predicate. A forbidden field (extra='forbid') or a missing
    # required field (incl. signed_up_before) raises ValidationError → reject.
    if isinstance(cohort_predicate, CohortPredicate):
        predicate = cohort_predicate
    else:
        try:
            predicate = CohortPredicate.model_validate(cohort_predicate)
        except ValidationError:
            logger.info("VT-74 k_anonymity: predicate_invalid (validation rejected)")
            return AdmissionResult(
                admitted=False, tenant_count=0, reason="predicate_invalid",
            )

    audit_run_id = run_id or uuid4()

    # The ONE sanctioned cross-tenant read — service-role pool, NOT
    # tenant_connection (cross-population by design). Tenant eligibility keys on
    # business_type + city_tier + signed_up_before; recency_band is pass-through.
    # signed_up_at < signed_up_before also excludes NULL signups (quarantine).
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id FROM tenants "
            "WHERE business_type = %s AND city_tier = %s AND signed_up_at < %s "
            "ORDER BY id LIMIT %s",
            (
                predicate.business_type,
                predicate.city_tier,
                predicate.signed_up_before,
                _TENANT_QUERY_CAP,
            ),
        ).fetchall()

    eligible = [r["id"] if isinstance(r["id"], UUID) else UUID(str(r["id"])) for r in rows]
    tenant_count = len(eligible)
    if tenant_count >= _TENANT_QUERY_CAP:
        logger.warning(
            "VT-74 k_anonymity: cohort hit the %d-tenant cap — Type-2 governance "
            "if this is a real cohort", _TENANT_QUERY_CAP,
        )
    admitted = tenant_count >= k_min
    reason: AdmissionReason = "admitted" if admitted else "below_k_min"

    _audit(audit_run_id, _predicate_hash(predicate), k_min, tenant_count, admitted)

    # eligible_tenant_ids returned ONLY here, in-process, to the L3 caller.
    return AdmissionResult(
        admitted=admitted,
        tenant_count=tenant_count,
        reason=reason,
        eligible_tenant_ids=eligible if admitted else [],
    )


__all__ = ["AdmissionResult", "CohortPredicate", "K_MIN_FLOOR", "check_admission"]
