import { jwtVerify, type JWTPayload } from 'jose'

/**
 * Verify a trial-end deep-link token (VT-91) — authenticates an owner to the
 * `/team/subscribe` conversion page WITHOUT a portal login (the trial-end WhatsApp
 * nudge carries `?token=<jwt>`).
 *
 * Mirrors `verifyOwnerJwt` but with a DISTINCT audience (`trial-end-subscribe`) so a
 * leaked deep-link token can NEVER be replayed against portal / owner-session data.
 * HS256 with `OWNER_JWT_SECRET` (the same secret; the audience is the isolation). `jose`
 * enforces `exp`, so an expired token throws.
 *
 * SINGLE-USE enforcement (consume-after-subscribe) + token MINTING live in the deferred
 * issuance follow-up (VT-332) — until that lands, no real tokens are issued (the
 * deep-link path is dormant; this verify is exercised by manually-minted test tokens).
 */
export const TRIAL_END_AUDIENCE = 'trial-end-subscribe'

export interface TrialEndClaim extends JWTPayload {
  tenant_id: string
}

export class TrialEndTokenError extends Error {}

function _secretBytes(): Uint8Array {
  const secret = process.env.OWNER_JWT_SECRET ?? ''
  // A config error (unset secret) is NOT a token problem — throw a plain Error so it
  // propagates (500), never masked as 'invalid token' (401) by the verify catch below.
  if (!secret) throw new Error('OWNER_JWT_SECRET unset (config)')
  return new TextEncoder().encode(secret)
}

/**
 * Verify a trial-end token and return its tenant. Throws TrialEndTokenError on a
 * missing / malformed / expired / wrong-audience token, or a missing tenant_id claim.
 * The caller derives the tenant from THIS return value — never from a raw client field.
 */
export async function verifyTrialEndToken(token: string): Promise<{ tenantId: string }> {
  if (!token) throw new TrialEndTokenError('missing token')
  const secret = _secretBytes() // config error (unset secret) propagates here — NOT masked below
  let payload: JWTPayload
  try {
    ;({ payload } = await jwtVerify(token, secret, {
      audience: TRIAL_END_AUDIENCE, // a wrong / owner-audience token throws here
    }))
  } catch {
    throw new TrialEndTokenError('invalid or expired trial-end token')
  }
  const tenantId = typeof payload.tenant_id === 'string' ? payload.tenant_id : ''
  if (!tenantId) throw new TrialEndTokenError('token missing tenant_id')
  return { tenantId }
}
