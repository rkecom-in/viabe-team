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
const ORIGINAL_FAZAL_UUID = process.env.FAZAL_OWNER_UUID
const FAZAL_UUID_TEST = '00000000-0000-4000-8000-0000000fa2a1'

describe('VT-203 — Ops Console login surface', () => {
  beforeEach(() => {
    process.env.OPERATOR_JWT_SECRET = 'vt203-test-' + 'a'.repeat(40)
    process.env.SUPABASE_URL = 'https://example.supabase.co'
    process.env.SUPABASE_SECRET_KEY = 'sb-secret-test'
    process.env.FAZAL_OWNER_UUID = FAZAL_UUID_TEST
  })

  afterEach(() => {
    if (ORIGINAL_SECRET === undefined) delete process.env.OPERATOR_JWT_SECRET
    else process.env.OPERATOR_JWT_SECRET = ORIGINAL_SECRET
    if (ORIGINAL_URL === undefined) delete process.env.SUPABASE_URL
    else process.env.SUPABASE_URL = ORIGINAL_URL
    if (ORIGINAL_KEY === undefined) delete process.env.SUPABASE_SECRET_KEY
    else process.env.SUPABASE_SECRET_KEY = ORIGINAL_KEY
    if (ORIGINAL_FAZAL_UUID === undefined) delete process.env.FAZAL_OWNER_UUID
    else process.env.FAZAL_OWNER_UUID = ORIGINAL_FAZAL_UUID
    vi.resetModules()
    vi.restoreAllMocks()
  })

  it('A1 — login page file exists and exports a default component', async () => {
    // Server-component runtime invocation needs the React jsx-runtime
    // setup that the Next test env doesn't provide here. Smoke-check
    // the module's surface instead — full visual render verified at
    // playwright e2e time.
    const mod = await import('@/app/(app)/team/ops/login/page')
    expect(typeof mod.default).toBe('function')
    expect(mod.dynamic).toBe('force-dynamic')
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

  it('A3 — callback with token_hash + allowlisted UUID → 302 /team/ops + cookie scoped', async () => {
    const verifyOtp = vi.fn().mockResolvedValue({
      data: { session: { user: { id: FAZAL_UUID_TEST } } },
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

  it('A4 — callback with verified session BUT user NOT in operator allowlist → 302 /team/ops/login?error=not_authorized + NO cookie', async () => {
    const verifyOtp = vi.fn().mockResolvedValue({
      data: {
        session: {
          // Random non-Fazal UUID — Supabase Auth proved email delivery,
          // but this user is NOT the Phase-1 operator.
          user: { id: '11111111-1111-4111-8111-111111111111' },
        },
      },
      error: null,
    })
    vi.doMock('@/lib/supabase-client', () => ({
      serverSecretClient: () => ({ auth: { verifyOtp } }),
    }))
    const { GET } = await import('@/app/api/ops/login/callback/route')
    const req = new Request(
      'http://test/api/ops/login/callback?token_hash=abc&type=magiclink',
    )
    const res = await GET(req)
    expect(res.status).toBe(302)
    expect(res.headers.get('location')).toContain('error=not_authorized')
    expect(res.headers.get('set-cookie') ?? '').not.toContain('viabe_ops_jwt')
  })
})
