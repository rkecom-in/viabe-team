/**
 * Owner-session JWT issuance + verification for the VT-250 owner portal.
 *
 * The OWNER analog of `operator-jwt.ts`. Where the operator token carries
 * `operator_claim` + `aud='authenticated'` and names a human operator, the
 * owner token is TENANT-SCOPED: it carries a `tenant_id` claim and
 * `aud='owner'`. It is minted only after a Twilio Verify OTP check passes
 * and the entered phone resolves to exactly one tenant (D1 anchor).
 *
 * Cowork ruling D3 (BINDING): the session is a STATELESS HS256 JWT — no
 * `owner_sessions` table. Revocation is via SHORT TTL + re-auth. The TTL is
 * capped at 24h (`OWNER_SESSION_TTL_SEC`).
 *
 * No-privilege-crossover invariant (VT-250 risk): the owner token uses a
 * DISTINCT audience (`owner`) AND a distinct signing secret
 * (`OWNER_JWT_SECRET`) from the operator token (`authenticated` /
 * `OPERATOR_JWT_SECRET`). `verifyOwnerJwt` rejects an operator token and
 * `verifyOperatorJwt` rejects an owner token — neither can be replayed as
 * the other.
 */

import { SignJWT, jwtVerify, type JWTPayload } from 'jose'

const JWT_SECRET = process.env.OWNER_JWT_SECRET ?? ''

// Cowork D3: short TTL ≤ 24h. Revocation = expiry + re-auth (no session table).
export const OWNER_SESSION_TTL_SEC = 60 * 60 * 24 // 24h (the cap)

export const OWNER_AUDIENCE = 'owner'


export interface OwnerClaim extends JWTPayload {
  sub: string // tenant UUID (the owner's tenant — the session subject)
  tenant_id: string
  owner_claim: true
  aud: string
}


function _secretBytes(): Uint8Array {
  if (!JWT_SECRET) {
    throw new Error('owner-jwt: OWNER_JWT_SECRET env must be set on server')
  }
  return new TextEncoder().encode(JWT_SECRET)
}


export async function issueOwnerJwt(
  tenantId: string,
  opts: { ttlSec?: number } = {},
): Promise<string> {
  // Defense-in-depth: clamp any caller-supplied TTL to the 24h cap (D3).
  const requested = opts.ttlSec ?? OWNER_SESSION_TTL_SEC
  const ttl = Math.min(requested, OWNER_SESSION_TTL_SEC)
  return await new SignJWT({
    tenant_id: tenantId,
    owner_claim: true,
  })
    .setProtectedHeader({ alg: 'HS256' })
    .setSubject(tenantId)
    .setAudience(OWNER_AUDIENCE)
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + ttl)
    .sign(_secretBytes())
}


export async function verifyOwnerJwt(jwt: string): Promise<OwnerClaim> {
  // The audience guard here is the FIRST line of no-crossover defense: an
  // operator token (aud='authenticated') fails audience validation before
  // any claim inspection. The distinct secret is the second line — an
  // operator token is not even signed with OWNER_JWT_SECRET.
  const { payload } = await jwtVerify(jwt, _secretBytes(), {
    audience: OWNER_AUDIENCE,
  })
  if (payload.owner_claim !== true || typeof payload.tenant_id !== 'string') {
    throw new Error('owner-jwt: claim missing or malformed')
  }
  return payload as OwnerClaim
}
