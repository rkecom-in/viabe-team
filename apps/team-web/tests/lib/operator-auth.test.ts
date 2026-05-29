/**
 * VT-192 — operator-auth wrapper tests.
 *
 * 4 assertions:
 *   A1: no JWT (no Authorization, no cookie) → UnauthorizedError
 *   A2: invalid JWT → UnauthorizedError
 *   A3: valid JWT in Authorization Bearer → returns operatorId
 *   A4: valid JWT in viabe_ops_jwt cookie → returns operatorId
 *
 * Uses jose to mint real HS256 tokens against a throwaway secret
 * matching the env stub, so the wrapper exercises the real
 * verifyOperatorJwt path (not mocked).
 */

import { SignJWT } from 'jose'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

const ORIGINAL_SECRET = process.env.OPERATOR_JWT_SECRET
const TEST_SECRET = 'vt192-canary-' + 'a'.repeat(40)

async function mintJwt(operatorId: string): Promise<string> {
  return await new SignJWT({
    operator_id: operatorId,
    operator_claim: true,
  })
    .setProtectedHeader({ alg: 'HS256' })
    .setSubject(operatorId)
    .setAudience('authenticated')
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + 600)
    .sign(new TextEncoder().encode(TEST_SECRET))
}

describe('VT-192 — operator-auth wrapper', () => {
  beforeEach(() => {
    process.env.OPERATOR_JWT_SECRET = TEST_SECRET
  })

  afterEach(() => {
    if (ORIGINAL_SECRET === undefined) {
      delete process.env.OPERATOR_JWT_SECRET
    } else {
      process.env.OPERATOR_JWT_SECRET = ORIGINAL_SECRET
    }
  })

  it('A1 — no JWT (no Authorization, no cookie) → UnauthorizedError', async () => {
    const { requireOperator, UnauthorizedError } = await import(
      '@/lib/operator-auth'
    )
    const req = new Request('http://test/api/ops/resolve-phone', { method: 'POST' })
    await expect(requireOperator(req)).rejects.toBeInstanceOf(UnauthorizedError)
  })

  it('A2 — invalid JWT → UnauthorizedError', async () => {
    const { requireOperator, UnauthorizedError } = await import(
      '@/lib/operator-auth'
    )
    const req = new Request('http://test/api/ops/resolve-phone', {
      method: 'POST',
      headers: { Authorization: 'Bearer not-a-jwt' },
    })
    await expect(requireOperator(req)).rejects.toBeInstanceOf(UnauthorizedError)
  })

  it('A3 — valid JWT in Authorization Bearer → returns operatorId', async () => {
    const { requireOperator } = await import('@/lib/operator-auth')
    const operatorId = '00000000-0000-4000-8000-0000000fa2a1'
    const jwt = await mintJwt(operatorId)
    const req = new Request('http://test/api/ops/resolve-phone', {
      method: 'POST',
      headers: { Authorization: `Bearer ${jwt}` },
    })
    const result = await requireOperator(req)
    expect(result.operatorId).toBe(operatorId)
    expect(result.claim.operator_claim).toBe(true)
    expect(result.rawToken).toBe(jwt)
  })

  it('A4 — valid JWT in viabe_ops_jwt cookie → returns operatorId', async () => {
    const { requireOperator } = await import('@/lib/operator-auth')
    const operatorId = '00000000-0000-4000-8000-0000000fa2a2'
    const jwt = await mintJwt(operatorId)
    const req = new Request('http://test/api/ops/resolve-phone', {
      method: 'POST',
      headers: { cookie: `other=x; viabe_ops_jwt=${jwt}; another=y` },
    })
    const result = await requireOperator(req)
    expect(result.operatorId).toBe(operatorId)
    expect(result.rawToken).toBe(jwt)
  })
})
