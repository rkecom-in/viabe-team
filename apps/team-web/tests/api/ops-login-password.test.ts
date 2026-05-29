/**
 * VT-237 — env-password operator login runtime tests.
 *
 * A1: valid email + correct password → 302 + viabe_ops_jwt cookie
 *     (Max-Age=604800)
 * A2: correct email + wrong password → 302 invalid_credentials, no cookie
 * A3: wrong email + correct password → 302 invalid_credentials, no cookie
 * A4: no password (magic-link path) → signInWithOtp called
 * A5: OPERATOR_PASSWORD unset → 302 password_login_not_configured
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const FAZAL_UUID = '00000000-0000-4000-8000-0000000fa2a1'
const O_SECRET = process.env.OPERATOR_JWT_SECRET
const O_FAZAL = process.env.FAZAL_OWNER_UUID
const O_EMAIL = process.env.OPERATOR_EMAIL
const O_PASSWORD = process.env.OPERATOR_PASSWORD

describe('VT-237 — env-password operator login', () => {
  beforeEach(() => {
    process.env.OPERATOR_JWT_SECRET = 'vt237-test-' + 'a'.repeat(40)
    process.env.FAZAL_OWNER_UUID = FAZAL_UUID
    process.env.OPERATOR_EMAIL = 'fazal@viabe.ai'
    process.env.OPERATOR_PASSWORD = 'correct-horse-battery-staple'
  })

  afterEach(() => {
    if (O_SECRET === undefined) delete process.env.OPERATOR_JWT_SECRET
    else process.env.OPERATOR_JWT_SECRET = O_SECRET
    if (O_FAZAL === undefined) delete process.env.FAZAL_OWNER_UUID
    else process.env.FAZAL_OWNER_UUID = O_FAZAL
    if (O_EMAIL === undefined) delete process.env.OPERATOR_EMAIL
    else process.env.OPERATOR_EMAIL = O_EMAIL
    if (O_PASSWORD === undefined) delete process.env.OPERATOR_PASSWORD
    else process.env.OPERATOR_PASSWORD = O_PASSWORD
    vi.resetModules()
    vi.restoreAllMocks()
  })

  it('A1 — correct email + password → 302 + cookie Max-Age=604800', async () => {
    const { POST } = await import('@/app/api/ops/login/route')
    const req = new Request('http://test/api/ops/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        email: 'fazal@viabe.ai',
        password: 'correct-horse-battery-staple',
      }),
    })
    const res = await POST(req)
    expect(res.status).toBe(302)
    expect(res.headers.get('location')).toContain('/team/ops')
    const setCookie = res.headers.get('set-cookie') ?? ''
    expect(setCookie).toContain('viabe_ops_jwt=')
    expect(setCookie).toContain('Path=/team')
    expect(setCookie).toMatch(/Max-Age=604800/)
  })

  it('A2 — correct email + wrong password → invalid_credentials, no cookie', async () => {
    const { POST } = await import('@/app/api/ops/login/route')
    const req = new Request('http://test/api/ops/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        email: 'fazal@viabe.ai',
        password: 'wrong-password',
      }),
    })
    const res = await POST(req)
    expect(res.status).toBe(302)
    expect(res.headers.get('location')).toContain('error=invalid_credentials')
    expect(res.headers.get('set-cookie') ?? '').not.toContain('viabe_ops_jwt')
  })

  it('A3 — wrong email + correct password → invalid_credentials, no cookie', async () => {
    const { POST } = await import('@/app/api/ops/login/route')
    const req = new Request('http://test/api/ops/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        email: 'attacker@example.com',
        password: 'correct-horse-battery-staple',
      }),
    })
    const res = await POST(req)
    expect(res.status).toBe(302)
    expect(res.headers.get('location')).toContain('error=invalid_credentials')
    expect(res.headers.get('set-cookie') ?? '').not.toContain('viabe_ops_jwt')
  })

  it('A4 — no password → magic-link path (signInWithOtp called)', async () => {
    const signInWithOtp = vi.fn().mockResolvedValue({ error: null })
    vi.doMock('@/lib/supabase-client', () => ({
      serverSecretClient: () => ({ auth: { signInWithOtp } }),
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

  it('A5 — OPERATOR_PASSWORD env unset → password_login_not_configured', async () => {
    delete process.env.OPERATOR_PASSWORD
    const { POST } = await import('@/app/api/ops/login/route')
    const req = new Request('http://test/api/ops/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        email: 'fazal@viabe.ai',
        password: 'anything',
      }),
    })
    const res = await POST(req)
    expect(res.status).toBe(302)
    expect(res.headers.get('location')).toContain('password_login_not_configured')
  })
})
