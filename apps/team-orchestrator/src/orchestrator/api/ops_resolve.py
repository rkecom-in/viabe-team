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
