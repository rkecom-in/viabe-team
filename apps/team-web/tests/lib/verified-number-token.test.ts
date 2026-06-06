/** VT-326 — pre-tenant verified-number proof token. */

import { afterAll, beforeAll, describe, expect, it } from 'vitest'

const ORIG = process.env.OWNER_JWT_SECRET
beforeAll(() => {
  process.env.OWNER_JWT_SECRET = 'vt326-test-' + 's'.repeat(40)
})
afterAll(() => {
  if (ORIG === undefined) delete process.env.OWNER_JWT_SECRET
  else process.env.OWNER_JWT_SECRET = ORIG
})

describe('VT-326 verified-number-token', () => {
  it('issue → verify roundtrip returns the phone', async () => {
    const { issueVerifiedNumberToken, verifyVerifiedNumberToken } = await import(
      '@/lib/auth/verified-number-token'
    )
    const t = await issueVerifiedNumberToken('+919811111111')
    expect((await verifyVerifiedNumberToken(t)).phoneE164).toBe('+919811111111')
  })

  it('REJECTS an owner-session token (wrong audience — no crossover)', async () => {
    const { issueOwnerJwt } = await import('@/lib/auth/owner-jwt')
    const { verifyVerifiedNumberToken } = await import('@/lib/auth/verified-number-token')
    const ownerTok = await issueOwnerJwt('tenant-1') // aud='owner', same secret
    await expect(verifyVerifiedNumberToken(ownerTok)).rejects.toThrow()
  })

  it('rejects a garbage / tampered token', async () => {
    const { verifyVerifiedNumberToken } = await import('@/lib/auth/verified-number-token')
    await expect(verifyVerifiedNumberToken('garbage.token.here')).rejects.toThrow()
  })

  it('rejects an expired token', async () => {
    const { issueVerifiedNumberToken, verifyVerifiedNumberToken } = await import(
      '@/lib/auth/verified-number-token'
    )
    const t = await issueVerifiedNumberToken('+919811111111', { ttlSec: -1 })
    await expect(verifyVerifiedNumberToken(t)).rejects.toThrow()
  })
})
