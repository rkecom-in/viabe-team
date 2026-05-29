/**
 * VT-203 — Ops Console login surface tests.
 *
 * 3 assertions:
 *   A1: GET /team/ops/login renders the form (component-level smoke)
 *   A2: POST /api/ops/login with valid email → 302 + Supabase signInWithOtp
 *       was called (mocked)
 *   A3: GET /api/ops/login/callback with verified token → 302 /team/ops
 *       + Set-Cookie viabe_ops_jwt with HttpOnly + Secure + SameSite=Lax
 *       + path=/team/ops
 *
 * Supabase Auth mocked; real magic-link round-trip needs a Supabase
 * Auth project + email delivery — release-prep manual smoke.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const ORIGINAL_SECRET = process.env.OPERATOR_JWT_SECRET
const ORIGINAL_URL = process.env.SUPABASE_URL
const ORIGINAL_KEY = process.env.SUPABASE_SECRET_KEY

describe('VT-203 — Ops Console login surface', () => {
  beforeEach(() => {
    process.env.OPERATOR_JWT_SECRET = 'vt203-test-' + 'a'.repeat(40)
    process.env.SUPABASE_URL = 'https://example.supabase.co'
    process.env.SUPABASE_SECRET_KEY = 'sb-secret-test'
  })

  afterEach(() => {
    if (ORIGINAL_SECRET === undefined) delete process.env.OPERATOR_JWT_SECRET
    else process.env.OPERATOR_JWT_SECRET = ORIGINAL_SECRET
    if (ORIGINAL_URL === undefined) delete process.env.SUPABASE_URL
    else process.env.SUPABASE_URL = ORIGINAL_URL
    if (ORIGINAL_KEY === undefined) delete process.env.SUPABASE_SECRET_KEY
    else process.env.SUPABASE_SECRET_KEY = ORIGINAL_KEY
    vi.resetModules()
    vi.restoreAllMocks()
  })

  it('A1 — login page renders email form', async () => {
    const { default: OpsLoginPage } = await import(
      '@/app/(app)/team/ops/login/page'
    )
    // Server component — invoke and inspect React tree
    const tree = OpsLoginPage({ searchParams: {} }) as Record<string, unknown>
    expect(tree).toBeTruthy()
    // The page returns a <main>; structural smoke check
    expect(JSON.stringify(tree)).toContain('Ops Console')
    expect(JSON.stringify(tree)).toContain('email')
  })

  it('A2 — POST /api/ops/login → 302 + supabase signInWithOtp called', async () => {
    const signInWithOtp = vi.fn().mockResolvedValue({ error: null })
    vi.doMock('@/lib/supabase-client', () => ({
      serverSecretClient: () => ({
        auth: { signInWithOtp },
      }),
    }))
    const { POST } = await import('@/app/api/ops/login/route')
    const req = new Request('http://test/api/ops/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ email: 'fazal@viabe.ai' }),
    })
    const res = await POST(req)
    expect(res.status).toBe(302)
    expect(res.headers.get('location')).toContain('/team/ops/login?sent=1')
    expect(signInWithOtp).toHaveBeenCalledOnce()
  })

  it('A3 — callback with token_hash → 302 /team/ops + cookie scoped', async () => {
    const verifyOtp = vi.fn().mockResolvedValue({
      data: { session: { user: { id: '00000000-0000-4000-8000-0000000fa2a1' } } },
      error: null,
    })
    vi.doMock('@/lib/supabase-client', () => ({
      serverSecretClient: () => ({
        auth: { verifyOtp },
      }),
    }))
    const { GET } = await import('@/app/api/ops/login/callback/route')
    const req = new Request(
      'http://test/api/ops/login/callback?token_hash=abc&type=magiclink',
    )
    const res = await GET(req)
    expect(res.status).toBe(302)
    expect(res.headers.get('location')).toContain('/team/ops')
    const setCookie = res.headers.get('set-cookie') ?? ''
    expect(setCookie).toContain('viabe_ops_jwt=')
    expect(setCookie.toLowerCase()).toContain('httponly')
    expect(setCookie.toLowerCase()).toContain('secure')
    expect(setCookie.toLowerCase()).toContain('samesite=lax')
    expect(setCookie).toContain('Path=/team/ops')
  })
})
