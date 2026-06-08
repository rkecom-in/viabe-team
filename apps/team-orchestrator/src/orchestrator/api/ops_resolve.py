"""VT-192-absorbed / VT-123 — orchestrator-side decrypt proxy for [resolve].

Endpoint: ``POST /api/orchestrator/ops/resolve-phone``.

Flow (per CL-390 + VT-188 + VT-191):
1. Verify ``X-Internal-Secret`` header (CL-72 — internal API secret).
2. Verify ``X-Operator-Jwt`` header carries an HS256-signed operator
   claim (signed by team-web's ``issueOperatorJwt`` with
   ``OPERATOR_JWT_SECRET``). The orchestrator validates the same secret.
3. Set ``app.jwt.operator_claim`` GUC + ``app.current_tenant`` if known
   (RLS substrate from VT-188).
4. Call ``resolve_phone_token_audited(phone_token, operator_id)`` —
   returns the Fernet ciphertext + writes the audit row atomically.
5. Decrypt via VT-191's ``decrypt_phone`` (key stays in this process).
6. Return ``{phone_e164}`` JSON.

Per CL-390: every resolve writes the audit row inside the stored
function transaction; resolution-without-audit is impossible.
Per Q3 Option A (Cowork plan-review locked): team-web does NOT carry
the encryption key — defense-in-depth.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, cast

import jwt as pyjwt
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from orchestrator.graph import get_pool
from orchestrator.observability.phone_tokens import decrypt_phone

logger = logging.getLogger(__name__)
router = APIRouter()


_AUDIENCE = "authenticated"


class ResolvePhoneBody(BaseModel):
    phone_token: str
    operator_id: str


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _verify_operator_jwt(jwt_str: str | None) -> dict[str, Any]:
    """Decode + validate the HS256 operator-claim JWT issued by team-web.

    Returns the decoded payload on success; raises ``HTTPException(403)``
    on any failure (missing secret, bad signature, missing claim).
    """
    if not jwt_str:
        raise HTTPException(status_code=403, detail="X-Operator-Jwt missing")
    secret = os.environ.get("OPERATOR_JWT_SECRET", "")
    if not secret:
        raise HTTPException(
            status_code=500, detail="OPERATOR_JWT_SECRET not configured"
        )
    try:
        payload = pyjwt.decode(
            jwt_str,
            secret,
            algorithms=["HS256"],
            audience=_AUDIENCE,
        )
    except pyjwt.InvalidTokenError as exc:
        raise HTTPException(status_code=403, detail=f"JWT verify failed: {exc}")
    if not payload.get("operator_claim") or not payload.get("operator_id"):
        raise HTTPException(
            status_code=403, detail="JWT missing operator_claim / operator_id"
        )
    return payload


@router.post("/api/orchestrator/ops/resolve-phone")
def resolve_phone(
    body: ResolvePhoneBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """[resolve] decrypt-proxy. See module docstring for the full flow."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="X-Internal-Secret mismatch")
    claim = _verify_operator_jwt(x_operator_jwt)
    if claim.get("operator_id") != body.operator_id:
        raise HTTPException(
            status_code=403,
            detail="operator_id in body != JWT claim",
        )

    pool = get_pool()
    with pool.connection() as conn, conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.jwt.operator_claim', %s, true)",
                ("true",),
            )
            cur.execute(
                "SELECT set_config('app.jwt.operator_id', %s, true)",
                (body.operator_id,),
            )
            cur.execute(
                "SELECT resolve_phone_token_audited(%s, %s) AS phone",
                (body.phone_token, body.operator_id),
            )
            raw_row = cur.fetchone()
    row = cast("dict[str, Any] | None", raw_row)
    ciphertext = row["phone"] if row else None
    if ciphertext is None:
        return {"phone_e164": None}
    try:
        phone_e164 = decrypt_phone(ciphertext)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "VT-123 decrypt failed for resolve-phone",
            extra={
                "operator_id": body.operator_id,
                "phone_token": body.phone_token,
                "exc": repr(exc),
            },
        )
        return {"phone_e164": None}
    return {"phone_e164": phone_e164}


class ResolveEscalationBody(BaseModel):
    escalation_id: str
    operator_id: str
    resolution_reason: str = ""


@router.post("/api/orchestrator/ops/resolve-escalation")
def resolve_escalation(
    body: ResolveEscalationBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """VT-359 (VT-357-p3 minimal): an operator resolves an escalation → mark resolved + ops-audit +
    best-effort `support_resolved` send to the owner. Same internal-secret + operator-JWT gate as
    resolve-phone. (A Telegram /resolve ingress is a later nicety — this is the ops-invokable
    entry point.)"""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="X-Internal-Secret mismatch")
    claim = _verify_operator_jwt(x_operator_jwt)
    if claim.get("operator_id") != body.operator_id:
        raise HTTPException(status_code=403, detail="operator_id in body != JWT claim")

    pool = get_pool()
    with pool.connection() as conn, conn.transaction():
        raw = conn.execute(
            "UPDATE escalations SET status = 'resolved', resolved_by = %s, resolved_at = now() "
            "WHERE id = %s AND status <> 'resolved' RETURNING tenant_id",
            (body.operator_id, body.escalation_id),
        ).fetchone()
        if raw is None:
            # Either no such escalation, or already resolved (idempotent — not an error to re-resolve).
            exists = conn.execute(
                "SELECT 1 FROM escalations WHERE id = %s", (body.escalation_id,)
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="escalation not found")
            return {"status": "already_resolved", "escalation_id": body.escalation_id}
        row = cast("dict[str, Any]", raw)
        tenant_id = str(row["tenant_id"])
        # Ops-audit the resolve (append-only; no PII in detail — CL-390).
        from orchestrator.escalations import record_ops_audit

        record_ops_audit(
            body.operator_id,
            "resolve",
            "escalation",
            tenant_id=tenant_id,
            target_id=body.escalation_id,
            detail=body.resolution_reason[:200] or None,
        )

    # Best-effort owner notification (support_resolved). Outside the txn — a send failure must not
    # roll back the resolve. send_template_message is naturally dormant pre-go-live (WABA not live
    # → 4xx → success=False). support_reference_id = the escalation id.
    try:
        from uuid import UUID

        from orchestrator.utils.twilio_send import send_template_message

        send_template_message(
            UUID(tenant_id),
            "support_resolved",
            {"support_reference_id": body.escalation_id},
        )
    except Exception:
        logger.exception(
            "support_resolved send failed escalation=%s (resolve still committed)",
            body.escalation_id,
        )
    return {"status": "resolved", "escalation_id": body.escalation_id, "tenant_id": tenant_id}


# ---------------------------------------------------------------------------
# VT-360 — VTR-facing de-identified reads (route through app_vtr_role + the VT-281/VT-360 views).
#
# CL-425 becomes DB-ENFORCED on ALL VTR paths here: team-web's VTR ops surface stops querying raw
# tables via the service-role (maskForVtr, app-side) and instead calls these endpoints, which read
# ONLY the de-identified views as app_vtr_role (NO grant on raw / decrypt — VT-281). Read-only,
# bounded, returns EXACTLY the view columns (no enrichment/joins — a new field is added to the VIEW,
# never here). Internal-secret + operator-JWT gated (the resolve-phone pattern).
#
# MULTI-VTR PRECONDITION (read before adding a 2nd VTR): the views are NOT assignment-scoped —
# Phase-1 = Fazal-as-VTR#1 sees ALL tenants. BEFORE a second VTR exists, the views MUST gain
# `WHERE tenant_id IN (SELECT ... FROM vtr_assignments WHERE vtr_id = ...)` (VT-281/VT-360 note);
# until then these endpoints intentionally return all tenants.
# ---------------------------------------------------------------------------

_VTR_PAGE_CAP = 200


class VtrReadBody(BaseModel):
    operator_id: str
    limit: int = 100


def _vtr_read_auth(
    x_internal_secret: str | None, x_operator_jwt: str | None, operator_id: str
) -> None:
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="X-Internal-Secret mismatch")
    claim = _verify_operator_jwt(x_operator_jwt)
    if claim.get("operator_id") != operator_id:
        raise HTTPException(status_code=403, detail="operator_id in body != JWT claim")


def _vtr_query(sql: str, limit: int) -> list[dict[str, Any]]:
    """Run a bounded read as app_vtr_role via vtr_connection. The view is the only door."""
    from orchestrator.privacy.vtr import vtr_connection

    capped = max(1, min(limit, _VTR_PAGE_CAP))
    with vtr_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, (capped,))
        return [dict(r) for r in cur.fetchall()]


@router.post("/api/orchestrator/ops/vtr-escalations")
def vtr_escalations_read(
    body: VtrReadBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """VTR escalations queue — EXACTLY the vtr_escalations view columns, app_vtr_role, bounded.
    Server-side filtered to the VTR queue: route='vtr' (knowledge-gap) + unresolved (early-review
    F7 — the filter lives here, not client-side)."""
    _vtr_read_auth(x_internal_secret, x_operator_jwt, body.operator_id)
    rows = _vtr_query(
        "SELECT escalation_id, tenant_id, tenant_name, kind, severity, status, opened_at, "
        "resolved_at, route FROM vtr_escalations "
        "WHERE route = 'vtr' AND status <> 'resolved' ORDER BY opened_at DESC LIMIT %s",
        body.limit,
    )
    return {"rows": rows, "count": len(rows)}


@router.post("/api/orchestrator/ops/vtr-monitoring")
def vtr_monitoring_read(
    body: VtrReadBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """VTR monitoring board — EXACTLY the vtr_tenant_alerts view columns (no message_text/payload/
    run_id — early-review F3), app_vtr_role, bounded."""
    _vtr_read_auth(x_internal_secret, x_operator_jwt, body.operator_id)
    rows = _vtr_query(
        "SELECT alert_id, tenant_id, tenant_name, trigger_kind, severity, fired_at "
        "FROM vtr_tenant_alerts ORDER BY fired_at DESC LIMIT %s",
        body.limit,
    )
    return {"rows": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# VT-361 — VTR "green" override (vtr_verified). Operator-JWT + internal-secret (same gate as
# resolve-escalation). Audited (who/when/free-text basis). Green gates nothing today, but the audit
# trail is load-bearing for when it gains significance. The target tenant is server-resolved
# (run_vtr_override verifies the row exists); operator_id is taken from the verified JWT, not trusted
# from the body alone (IDOR rule).
# ---------------------------------------------------------------------------


class VtrVerifyBody(BaseModel):
    tenant_id: str
    operator_id: str
    basis: str = ""


@router.post("/api/orchestrator/ops/vtr-verify")
def vtr_verify(
    body: VtrVerifyBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_operator_jwt: str | None = Header(default=None, alias="X-Operator-Jwt"),
) -> dict[str, Any]:
    """VT-361: upgrade a tenant to vtr_verified ("green"). Operator-JWT gated + audited."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="X-Internal-Secret mismatch")
    claim = _verify_operator_jwt(x_operator_jwt)
    if claim.get("operator_id") != body.operator_id:
        raise HTTPException(status_code=403, detail="operator_id in body != JWT claim")

    from orchestrator.onboarding.verification import run_vtr_override

    out = run_vtr_override(body.tenant_id, body.operator_id, body.basis)
    if not out.get("ok"):
        raise HTTPException(status_code=404, detail={"code": out.get("reason", "failed")})
    return out
