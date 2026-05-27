/**
 * Operator-claim JWT issuance + verification for the VT-188 substrate.
 *
 * The orchestrator's stored function `resolve_phone_token_audited` is
 * granted to `app_operator_role`; the RLS policy on the audit log table
 * inspects the JWT for `operator_claim=true` + `operator_id`. This module
 * mints + verifies HS256 tokens against `OPERATOR_JWT_SECRET`.
 *
 * Per CL-88: every JWT carries `aud='authenticated'` + a `sub` (UUID).
 * Per CL-390: every resolve is audit-logged; the `operator_id` claim
 * names the human operator (Fazal in Phase 1).
 */

import { SignJWT, jwtVerify, type JWTPayload } from 'jose'

const JWT_SECRET = process.env.OPERATOR_JWT_SECRET ?? ''
const _OPERATOR_TOKEN_TTL_SEC = 60 * 5  // 5 min — short-lived for [resolve].


export interface OperatorClaim extends JWTPayload {
  sub: string  // operator UUID (Fazal in Phase 1)
  operator_id: string
  operator_claim: true
  aud: string
}


function _secretBytes(): Uint8Array {
  if (!JWT_SECRET) {
    throw new Error(
      'operator-jwt: OPERATOR_JWT_SECRET env must be set on server',
    )
  }
  return new TextEncoder().encode(JWT_SECRET)
}


export async function issueOperatorJwt(operatorId: string): Promise<string> {
  return await new SignJWT({
    operator_id: operatorId,
    operator_claim: true,
  })
    .setProtectedHeader({ alg: 'HS256' })
    .setSubject(operatorId)
    .setAudience('authenticated')
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + _OPERATOR_TOKEN_TTL_SEC)
    .sign(_secretBytes())
}


export async function verifyOperatorJwt(jwt: string): Promise<OperatorClaim> {
  const { payload } = await jwtVerify(jwt, _secretBytes(), {
    audience: 'authenticated',
  })
  if (
    payload.operator_claim !== true ||
    typeof payload.operator_id !== 'string'
  ) {
    throw new Error('operator-jwt: claim missing or malformed')
  }
  return payload as OperatorClaim
}
