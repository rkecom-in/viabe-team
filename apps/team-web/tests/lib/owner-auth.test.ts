/**
 * VT-250 — owner-session auth tests + the no-privilege-crossover invariant.
 *
 * Mints REAL HS256 tokens with jose (distinct owner / operator secrets) so the
 * gates exercise the real verify paths, not mocks.
 *
 * Coverage:
 *   - issueOwnerJwt / verifyOwnerJwt round-trip → tenant_id claim, aud 'owner'
 *   - requireOwnerSession: no cookie → throws; valid owner cookie → tenantId
 *   - TTL is clamped to ≤ 24h (D3)
 *   - NO-PRIVILEGE-CROSSOVER (both directions):
 *       * an OPERATOR token is REJECTED by verifyOwnerJwt / requireOwnerSession
 *       * an OWNER token is REJECTED by verifyOperatorJwt
 */

import { SignJWT } from 'jose'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

const ORIG_OWNER_SECRET = process.env.OWNER_JWT_SECRET
const ORIG_OPERATOR_SECRET = process.env.OPERATOR_JWT_SECRET

const OWNER_SECRET = 'vt250-owner-' + 'o'.repeat(40)
const OPERATOR_SECRET = 'vt250-operator-' + 'p'.repeat(40)

const TENANT_ID = '22222222-2222-4222-8222-222222222222'
const OPERATOR_ID = '00000000-0000-4000-8000-0000000fa2a1'

async function mintOwnerToken(tenantId: string): Promise<string> {
  return await new SignJWT({ tenant_id: tenantId, owner_claim: true })
    .setProtectedHeader({ alg: 'HS256' })
    .setSubject(tenantId)
    .setAudience('owner')
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
    .sign(new TextEncoder().encode(OWNER_SECRET))
}

/** A genuine operator token: aud 'authenticated', signed with the OPERATOR secret. */
async function mintOperatorToken(operatorId: string): Promise<string> {
  return await new SignJWT({ operator_id: operatorId, operator_claim: true })
    .setProtectedHeader({ alg: 'HS256' })
    .setSubject(operatorId)
    .setAudience('authenticated')
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
    .sign(new TextEncoder().encode(OPERATOR_SECRET))
}

function jarWith(name: string, value: string) {
  return async () => ({
    get(n: string) {
      return n === name ? { value } : undefined
    },
  })
}

describe('VT-250 — owner-session auth', () => {
  beforeEach(() => {
    process.env.OWNER_JWT_SECRET = OWNER_SECRET
    process.env.OPERATOR_JWT_SECRET = OPERATOR_SECRET
  })

  afterEach(() => {
    if (ORIG_OWNER_SECRET === undefined) delete process.env.OWNER_JWT_SECRET
    else process.env.OWNER_JWT_SECRET = ORIG_OWNER_SECRET
    if (ORIG_OPERATOR_SECRET === undefined) delete process.env.OPERATOR_JWT_SECRET
    else process.env.OPERATOR_JWT_SECRET = ORIG_OPERATOR_SECRET
  })

  it('issueOwnerJwt → verifyOwnerJwt round-trip carries tenant_id + aud owner', async () => {
    const { issueOwnerJwt, verifyOwnerJwt, OWNER_AUDIENCE } = await import(
      '@/lib/auth/owner-jwt'
    )
    const jwt = await issueOwnerJwt(TENANT_ID)
    const claim = await verifyOwnerJwt(jwt)
    expect(claim.tenant_id).toBe(TENANT_ID)
    expect(claim.sub).toBe(TENANT_ID)
    expect(claim.owner_claim).toBe(true)
    expect(claim.aud).toBe(OWNER_AUDIENCE)
  })

  it('owner TTL is clamped to ≤ 24h (D3)', async () => {
    const { issueOwnerJwt, verifyOwnerJwt, OWNER_SESSION_TTL_SEC } = await import(
      '@/lib/auth/owner-jwt'
    )
    // Request a 30-day TTL; it must be clamped to the 24h cap.
    const jwt = await issueOwnerJwt(TENANT_ID, { ttlSec: 60 * 60 * 24 * 30 })
    const claim = await verifyOwnerJwt(jwt)
    const now = Math.floor(Date.now() / 1000)
    const lifetime = (claim.exp ?? 0) - now
    expect(lifetime).toBeLessThanOrEqual(OWNER_SESSION_TTL_SEC + 2)
    expect(OWNER_SESSION_TTL_SEC).toBeLessThanOrEqual(60 * 60 * 24)
  })

  it('requireOwnerSession: no cookie → OwnerUnauthorizedError', async () => {
    const { requireOwnerSession, OwnerUnauthorizedError } = await import(
      '@/lib/auth/require-owner-session'
    )
    const emptyJar = async () => ({ get: () => undefined })
    await expect(requireOwnerSession(emptyJar)).rejects.toBeInstanceOf(
      OwnerUnauthorizedError,
    )
  })

  it('requireOwnerSession: valid owner cookie → tenantId', async () => {
    const { requireOwnerSession } = await import(
      '@/lib/auth/require-owner-session'
    )
    const jwt = await mintOwnerToken(TENANT_ID)
    const result = await requireOwnerSession(jarWith('viabe_team_session', jwt))
    expect(result.tenantId).toBe(TENANT_ID)
  })

  // ---- NO-PRIVILEGE-CROSSOVER (both directions) -----------------------------

  it('CROSSOVER A: an OPERATOR token is REJECTED by verifyOwnerJwt', async () => {
    const { verifyOwnerJwt } = await import('@/lib/auth/owner-jwt')
    const operatorJwt = await mintOperatorToken(OPERATOR_ID)
    // Rejected on BOTH audience ('authenticated' ≠ 'owner') and secret.
    await expect(verifyOwnerJwt(operatorJwt)).rejects.toBeTruthy()
  })

  it('CROSSOVER A: an OPERATOR token is REJECTED by requireOwnerSession', async () => {
    const { requireOwnerSession, OwnerUnauthorizedError } = await import(
      '@/lib/auth/require-owner-session'
    )
    const operatorJwt = await mintOperatorToken(OPERATOR_ID)
    await expect(
      requireOwnerSession(jarWith('viabe_team_session', operatorJwt)),
    ).rejects.toBeInstanceOf(OwnerUnauthorizedError)
  })

  it('CROSSOVER B: an OWNER token is REJECTED by verifyOperatorJwt', async () => {
    const { verifyOperatorJwt } = await import('@/lib/auth/operator-jwt')
    const ownerJwt = await mintOwnerToken(TENANT_ID)
    // The operator verifier requires aud 'authenticated' + operator_claim;
    // the owner token has aud 'owner' and is signed with the OWNER secret.
    await expect(verifyOperatorJwt(ownerJwt)).rejects.toBeTruthy()
  })

  it('CROSSOVER B: an OWNER token signed with the OWNER secret is not a valid operator token even if secrets were shared', async () => {
    // Defense-in-depth: even if an attacker forced the operator secret, the
    // owner token's audience ('owner') still fails the operator verifier's
    // audience check ('authenticated'). Sign an owner-shaped token with the
    // OPERATOR secret and confirm it is still rejected.
    const { verifyOperatorJwt } = await import('@/lib/auth/operator-jwt')
    const ownerShapedWithOpSecret = await new SignJWT({
      tenant_id: TENANT_ID,
      owner_claim: true,
    })
      .setProtectedHeader({ alg: 'HS256' })
      .setSubject(TENANT_ID)
      .setAudience('owner')
      .setIssuedAt()
      .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
      .sign(new TextEncoder().encode(OPERATOR_SECRET))
    await expect(
      verifyOperatorJwt(ownerShapedWithOpSecret),
    ).rejects.toBeTruthy()
  })
})
