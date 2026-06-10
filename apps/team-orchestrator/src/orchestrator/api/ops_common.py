"""VT-370 Gap-6 — the shared VTR-action gate (the structurally unskippable authz chokepoint).

Every Gap-6 ops endpoint calls :func:`require_vtr_action` EXACTLY ONCE before touching state;
no handler hand-rolls the steps. This gate is NET-NEW safety, deliberately NOT inherited from the
precedents (adversarial-design finding): ``resolve-escalation``/``vtr-verify`` do NO assignment
check, and ``run-control`` takes no JWT at all — its body-trusted ``operator_id`` makes attribution
forgeable by anything holding ``INTERNAL_API_SECRET``. Here:

  1. the internal secret must match (server-to-server transport auth);
  2. the operator JWT must verify (HS256, audience, operator_claim) — 403 on missing/invalid;
  3. the body's ``operator_id`` must EQUAL the JWT claim (no body-trusted attribution);
  4. the operator must be ASSIGNED to the tenant (fail-CLOSED; the denial itself is audited);
  5. the returned VERIFIED id is the ONLY value ever passed as ``vtr_id`` into any seam.

``require_exception_tier`` gates the param-level drill-in (Fazal=VTR#1): the FAZAL_OWNER_UUID env
unset ⇒ 403 (never an open gate), and the comparison reads the VERIFIED claim id.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import jwt as pyjwt
from fastapi import HTTPException

logger = logging.getLogger(__name__)

_AUDIENCE = "authenticated"


def verify_internal_secret(x_internal_secret: str | None) -> None:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected:
        raise HTTPException(status_code=500, detail="INTERNAL_API_SECRET not configured")
    if x_internal_secret != expected:
        raise HTTPException(status_code=403, detail="X-Internal-Secret mismatch")


def verify_operator_jwt(jwt_str: str | None) -> dict[str, Any]:
    """Decode + validate the HS256 operator-claim JWT issued by team-web. 403 on any failure
    (missing token, bad signature, missing claim) — a valid internal secret alone NEVER suffices
    for a VTR action (the run-control inheritance is deliberately broken here)."""
    if not jwt_str:
        raise HTTPException(status_code=403, detail="X-Operator-Jwt missing")
    secret = os.environ.get("OPERATOR_JWT_SECRET", "")
    if not secret:
        raise HTTPException(status_code=500, detail="OPERATOR_JWT_SECRET not configured")
    try:
        payload = pyjwt.decode(jwt_str, secret, algorithms=["HS256"], audience=_AUDIENCE)
    except pyjwt.InvalidTokenError as exc:
        raise HTTPException(status_code=403, detail=f"JWT verify failed: {exc}") from exc
    if not payload.get("operator_claim") or not payload.get("operator_id"):
        raise HTTPException(status_code=403, detail="JWT missing operator claim")
    return payload


def operator_assigned(cur: Any, operator_id: str, tenant_id: str) -> bool:
    """Active operator_assignments row for (operator, tenant)? Fazal-UUID = VTAdmin break-glass
    (the established run-control idiom)."""
    fazal = (os.environ.get("FAZAL_OWNER_UUID", "") or "").strip()
    if fazal and operator_id == fazal:
        return True
    cur.execute(
        "SELECT 1 FROM operator_assignments "
        "WHERE operator_id = %s AND tenant_id = %s AND unassigned_at IS NULL LIMIT 1",
        (operator_id, tenant_id),
    )
    return cur.fetchone() is not None


def audit(
    cur: Any, *, operator_id: str, tenant_id: str | None, action: str,
    target_kind: str, target_id: str, detail: str | None,
) -> None:
    """One ops_audit row — metadata only (CL-390: field names/ids/counts, NEVER values/bodies)."""
    cur.execute(
        "INSERT INTO ops_audit (operator_id, tenant_id, action, target_kind, target_id, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (operator_id, tenant_id, action, target_kind, target_id, detail),
    )


def require_vtr_action(
    cur: Any, *,
    x_internal_secret: str | None,
    x_operator_jwt: str | None,
    body_operator_id: str,
    tenant_id: str,
    deny_action: str,
    deny_target_kind: str = "tenant",
    deny_target_id: str | None = None,
) -> str:
    """The Gap-6 gate (steps 1-4 above). Returns the VERIFIED operator id — the only value ever
    used as ``vtr_id``/audit attribution downstream. Raises HTTPException(403) on any failure;
    an assignment denial is itself audited as ``deny_action`` before raising (fail-closed AND
    visible)."""
    verify_internal_secret(x_internal_secret)
    claim = verify_operator_jwt(x_operator_jwt)
    verified_id = str(claim["operator_id"])
    if body_operator_id != verified_id:
        raise HTTPException(status_code=403, detail="operator_id in body != JWT claim")
    if not operator_assigned(cur, verified_id, tenant_id):
        audit(
            cur, operator_id=verified_id, tenant_id=tenant_id, action=deny_action,
            target_kind=deny_target_kind, target_id=deny_target_id or tenant_id,
            detail="assignment check failed",
        )
        raise HTTPException(status_code=403, detail="operator not assigned to tenant")
    return verified_id


def require_exception_tier(verified_operator_id: str) -> None:
    """The param-level drill-in gate (Fazal=VTR#1 only). FAZAL_OWNER_UUID unset ⇒ 403 — the
    exception tier NEVER defaults open; the comparison reads the VERIFIED claim id, never a body
    field."""
    fazal = (os.environ.get("FAZAL_OWNER_UUID", "") or "").strip()
    if not fazal or verified_operator_id != fazal:
        raise HTTPException(status_code=403, detail="exception tier required")
