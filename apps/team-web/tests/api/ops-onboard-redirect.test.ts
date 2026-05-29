/**
 * VT-230 — onboard auth flow tests.
 *
 * A1: GET /team/onboard unauthenticated → expects redirect to
 *     /team/ops/login?next=/team/onboard (server-component invocation
 *     is brittle in vitest; we smoke-check the module's redirect
 *     constant via a snapshot of the page source).
 * A2: callback with valid session + next=/team/onboard → 302 to
 *     /team/onboard + viabe_ops_jwt cookie scoped path=/team
 * A3: callback with off-allowlist next (e.g. //evil.com) → defaults
 *     to /team/ops; no open redirect
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const FAZAL_UUID = '00000000-0000-4000-8000-0000000fa2a1'
const O_SECRET = process.env.OPERATOR_JWT_SECRET
const O_FAZAL = process.env.FAZAL_OWNER_UUID
const O_SUPA = process.env.SUPABASE_URL
const O_KEY = process.env.SUPABASE_SECRET_KEY

describe('VT-230 — onboard auth flow', () => {
  beforeEach(() => {
    process.env.OPERATOR_JWT_SECRET = 'vt230-test-' + 'a'.repeat(40)
    process.env.FAZAL_OWNER_UUID = FAZAL_UUID
    process.env.SUPABASE_URL = 'https://example.supabase.co'
    process.env.SUPABASE_SECRET_KEY = 'sb-secret-test'
  })

  afterEach(() => {
    if (O_SECRET === undefined) delete process.env.OPERATOR_JWT_SECRET
    else process.env.OPERATOR_JWT_SECRET = O_SECRET
    if (O_FAZAL === undefined) delete process.env.FAZAL_OWNER_UUID
    else process.env.FAZAL_OWNER_UUID = O_FAZAL
    if (O_SUPA === undefined) delete process.env.SUPABASE_URL
    else process.env.SUPABASE_URL = O_SUPA
    if (O_KEY === undefined) delete process.env.SUPABASE_SECRET_KEY
    else process.env.SUPABASE_SECRET_KEY = O_KEY
    vi.resetModules()
    vi.restoreAllMocks()
  })

  it('A1 — onboard page source contains redirect to /team/ops/login?next=/team/onboard', async () => {
    // Server-component runtime needs jsx-runtime that vitest env lacks
    // — smoke the module's source. Real visual + redirect verified at
    // playwright e2e + manual canary time.
    const { readFile } = await import('fs/promises')
    const path = await import('path')
    // CWD is apps/team-web during `pnpm --filter @viabe/team-web test`
    const filePath = path.resolve(
      process.cwd(),
      'app/(app)/team/onboard/page.tsx',
    )
    const src = await readFile(filePath, 'utf8')
    expect(src).toContain("/team/ops/login?next=/team/onboard")
    expect(src).not.toContain("redirect('/login')")
  })

  it('A2 — callback with valid session + allowlisted next → 302 to next + cookie path=/team', async () => {
    const verifyOtp = vi.fn().mockResolvedValue({
      data: { session: { user: { id: FAZAL_UUID } } },
      error: null,
    })
    vi.doMock('@/lib/supabase-client', () => ({
      serverSecretClient: () => ({ auth: { verifyOtp } }),
    }))
    const { GET } = await import('@/app/api/ops/login/callback/route')
    const req = new Request(
      'http://test/api/ops/login/callback?token_hash=abc&type=magiclink&next=%2Fteam%2Fonboard',
    )
    const res = await GET(req)
    expect(res.status).toBe(302)
    expect(res.headers.get('location')).toContain('/team/onboard')
    const setCookie = res.headers.get('set-cookie') ?? ''
    expect(setCookie).toContain('viabe_ops_jwt=')
    expect(setCookie).toContain('Path=/team')
    // Wasn't widened to root
    expect(setCookie).not.toContain('Path=/;')
  })

  it('A3 — callback with off-allowlist next (//evil.com) → defaults to /team/ops; no open redirect', async () => {
    const verifyOtp = vi.fn().mockResolvedValue({
      data: { session: { user: { id: FAZAL_UUID } } },
      error: null,
    })
    vi.doMock('@/lib/supabase-client', () => ({
      serverSecretClient: () => ({ auth: { verifyOtp } }),
    }))
    const { GET } = await import('@/app/api/ops/login/callback/route')
    const req = new Request(
      'http://test/api/ops/login/callback?token_hash=abc&type=magiclink&next=%2F%2Fevil.com%2Fxss',
    )
    const res = await GET(req)
    expect(res.status).toBe(302)
    const location = res.headers.get('location') ?? ''
    expect(location).toContain('/team/ops')
    expect(location).not.toContain('evil.com')
  })

  it('A3b — callback with traversal next (/team/../etc) → defaults to /team/ops', async () => {
    const verifyOtp = vi.fn().mockResolvedValue({
      data: { session: { user: { id: FAZAL_UUID } } },
      error: null,
    })
    vi.doMock('@/lib/supabase-client', () => ({
      serverSecretClient: () => ({ auth: { verifyOtp } }),
    }))
    const { GET } = await import('@/app/api/ops/login/callback/route')
    const req = new Request(
      'http://test/api/ops/login/callback?token_hash=abc&type=magiclink&next=%2Fteam%2F..%2Fetc',
    )
    const res = await GET(req)
    expect(res.status).toBe(302)
    const location = res.headers.get('location') ?? ''
    expect(location).toContain('/team/ops')
    expect(location).not.toContain('..')
  })
})
