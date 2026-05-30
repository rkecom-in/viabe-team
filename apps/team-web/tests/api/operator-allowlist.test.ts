/**
 * VT-228 — operator allowlist (isOperator) + require-fazal allowlist gate.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const FAZAL_UUID = '00000000-0000-4000-8000-0000000fa2a1'
const OTHER_UUID = '11111111-1111-4111-8111-111111111111'
const O_FAZAL = process.env.FAZAL_OWNER_UUID

// Mock Supabase client whose .from().select().eq().is().maybeSingle()
// resolves to a configurable row.
function mockClient(row: unknown, error: unknown = null) {
  const chain: any = {
    select: () => chain,
    eq: () => chain,
    is: () => chain,
    maybeSingle: async () => ({ data: row, error }),
  }
  return { from: () => chain }
}

describe('VT-228 isOperator', () => {
  beforeEach(() => {
    process.env.FAZAL_OWNER_UUID = FAZAL_UUID
  })
  afterEach(async () => {
    if (O_FAZAL === undefined) delete process.env.FAZAL_OWNER_UUID
    else process.env.FAZAL_OWNER_UUID = O_FAZAL
    const mod = await import('@/lib/auth/operator-allowlist')
    mod._clearOperatorCache()
    vi.resetModules()
  })

  it('break-glass: FAZAL_OWNER_UUID always allowed, no client needed', async () => {
    const { isOperator } = await import('@/lib/auth/operator-allowlist')
    // Pass a client that would throw if touched — proves break-glass skips DB.
    const exploding = { from: () => { throw new Error('should not query') } }
    expect(await isOperator(FAZAL_UUID, exploding as any)).toBe(true)
  })

  it('allowlisted non-Fazal operator → true', async () => {
    const { isOperator, _clearOperatorCache } = await import('@/lib/auth/operator-allowlist')
    _clearOperatorCache()
    expect(await isOperator(OTHER_UUID, mockClient({ user_id: OTHER_UUID }))).toBe(true)
  })

  it('unknown / not-in-table → false', async () => {
    const { isOperator, _clearOperatorCache } = await import('@/lib/auth/operator-allowlist')
    _clearOperatorCache()
    expect(await isOperator(OTHER_UUID, mockClient(null))).toBe(false)
  })

  it('revoked (no active row) → false', async () => {
    const { isOperator, _clearOperatorCache } = await import('@/lib/auth/operator-allowlist')
    _clearOperatorCache()
    // revoked rows are filtered by .is('revoked_at', null) → maybeSingle null
    expect(await isOperator(OTHER_UUID, mockClient(null))).toBe(false)
  })

  it('DB error → fail closed (false) for non-Fazal', async () => {
    const { isOperator, _clearOperatorCache } = await import('@/lib/auth/operator-allowlist')
    _clearOperatorCache()
    expect(await isOperator(OTHER_UUID, mockClient(null, { message: 'boom' }))).toBe(false)
  })

  it('empty userId → false', async () => {
    const { isOperator } = await import('@/lib/auth/operator-allowlist')
    expect(await isOperator('', mockClient({ user_id: 'x' }))).toBe(false)
  })
})

describe('VT-228 requireFazal allowlist gate', () => {
  beforeEach(() => {
    process.env.FAZAL_OWNER_UUID = FAZAL_UUID
    process.env.OPERATOR_JWT_SECRET = 'vt228-test-' + 'a'.repeat(40)
  })
  afterEach(() => {
    if (O_FAZAL === undefined) delete process.env.FAZAL_OWNER_UUID
    else process.env.FAZAL_OWNER_UUID = O_FAZAL
    vi.resetModules()
  })

  async function mintJwt(sub: string): Promise<string> {
    const { issueOperatorJwt } = await import('@/lib/auth/operator-jwt')
    return issueOperatorJwt(sub)
  }

  it('allowlisted operator (injected check true) passes', async () => {
    const { requireFazal } = await import('@/lib/auth/require-fazal')
    const jwt = await mintJwt(OTHER_UUID)
    const jar = async () => ({ get: () => ({ value: jwt }) })
    const res = await requireFazal(jar, async () => true)
    expect(res.fazalUuid).toBe(OTHER_UUID)
  })

  it('revoked operator (injected check false) is rejected', async () => {
    const { requireFazal, UnauthorizedError } = await import('@/lib/auth/require-fazal')
    const jwt = await mintJwt(OTHER_UUID)
    const jar = async () => ({ get: () => ({ value: jwt }) })
    await expect(requireFazal(jar, async () => false)).rejects.toBeInstanceOf(UnauthorizedError)
  })
})
