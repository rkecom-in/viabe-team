import { SignJWT } from 'jose'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  TRIAL_END_AUDIENCE,
  TrialEndTokenError,
  verifyTrialEndToken,
} from '@/lib/auth/verify-trial-end-token'

const SECRET = 'test-owner-jwt-secret-vt91'
const TENANT = '22222222-2222-2222-2222-222222222222'

function secretBytes(): Uint8Array {
  return new TextEncoder().encode(SECRET)
}

async function mint(opts: {
  tenant?: string
  aud?: string
  ttlSec?: number
  jti?: string
  alg?: string
}): Promise<string> {
  const now = Math.floor(Date.now() / 1000)
  const builder = new SignJWT({ tenant_id: opts.tenant ?? TENANT })
    .setProtectedHeader({ alg: opts.alg ?? 'HS256' })
    .setAudience(opts.aud ?? TRIAL_END_AUDIENCE)
    .setIssuedAt(now)
    .setExpirationTime(now + (opts.ttlSec ?? 3600))
  if (opts.jti) builder.setJti(opts.jti)
  return await builder.sign(secretBytes())
}

beforeEach(() => {
  process.env.OWNER_JWT_SECRET = SECRET
})
afterEach(() => {
  delete process.env.OWNER_JWT_SECRET
})

describe('verifyTrialEndToken (VT-91)', () => {
  it('accepts a valid trial-end token and returns its tenant', async () => {
    const { tenantId } = await verifyTrialEndToken(await mint({}))
    expect(tenantId).toBe(TENANT)
  })

  it('rejects a wrong-audience token (an owner-session token cannot be replayed here)', async () => {
    await expect(verifyTrialEndToken(await mint({ aud: 'owner' }))).rejects.toBeInstanceOf(
      TrialEndTokenError,
    )
  })

  it('rejects an expired token', async () => {
    await expect(verifyTrialEndToken(await mint({ ttlSec: -10 }))).rejects.toBeInstanceOf(
      TrialEndTokenError,
    )
  })

  it('rejects a token missing tenant_id', async () => {
    const now = Math.floor(Date.now() / 1000)
    const noTenant = await new SignJWT({})
      .setProtectedHeader({ alg: 'HS256' })
      .setAudience(TRIAL_END_AUDIENCE)
      .setIssuedAt(now)
      .setExpirationTime(now + 3600)
      .sign(secretBytes())
    await expect(verifyTrialEndToken(noTenant)).rejects.toBeInstanceOf(TrialEndTokenError)
  })

  it('rejects a token signed with a different secret', async () => {
    const wrong = await new SignJWT({ tenant_id: TENANT })
      .setProtectedHeader({ alg: 'HS256' })
      .setAudience(TRIAL_END_AUDIENCE)
      .setExpirationTime(Math.floor(Date.now() / 1000) + 3600)
      .sign(new TextEncoder().encode('a-different-secret'))
    await expect(verifyTrialEndToken(wrong)).rejects.toBeInstanceOf(TrialEndTokenError)
  })

  it('rejects an empty token', async () => {
    await expect(verifyTrialEndToken('')).rejects.toBeInstanceOf(TrialEndTokenError)
  })

  // VT-332 ----------------------------------------------------------------- //
  it('returns the jti (the single-use key forwarded to the consume)', async () => {
    const { tenantId, jti } = await verifyTrialEndToken(await mint({ jti: 'jti-xyz' }))
    expect(tenantId).toBe(TENANT)
    expect(jti).toBe('jti-xyz')
  })

  it('jti is null when the token carries none (a legacy token)', async () => {
    const { jti } = await verifyTrialEndToken(await mint({}))
    expect(jti).toBeNull()
  })

  it('VT-350: rejects a token signed with a non-HS256 alg (algorithm pin)', async () => {
    // A token whose header alg is not the pinned HS256 must be rejected even with the right key.
    await expect(verifyTrialEndToken(await mint({ alg: 'HS384' }))).rejects.toBeInstanceOf(
      TrialEndTokenError,
    )
  })
})
