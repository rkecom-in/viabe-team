/**
 * VT-233 — finalize-hash POST endpoint tests.
 *
 * A1: missing access_token → 400
 * A2: invalid token (supabase.auth.getUser rejects) → 401
 * A3: valid token but userId NOT in FAZAL_OWNER_UUID allowlist → 403, no cookie
 * A4: valid token + allowlisted UUID → 200 {ok, next} + cookie path=/team
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const FAZAL_UUID = '00000000-0000-4000-8000-0000000fa2a1'
const O_SECRET = process.env.OPERATOR_JWT_SECRET
const O_FAZAL = process.env.FAZAL_OWNER_UUID
const O_SUPA = process.env.SUPABASE_URL
const O_KEY = process.env.SUPABASE_SECRET_KEY

describe('VT-233 — finalize-hash POST', () => {
  beforeEach(() => {
    process.env.OPERATOR_JWT_SECRET = 'vt233-test-' + 'a'.repeat(40)
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

  it('A1 — missing access_token → 400', async () => {
    const { POST } = await import('@/app/api/ops/login/finalize-hash/route')
    const req = new Request('http://test/api/ops/login/finalize-hash', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({}),
    })
    const res = await POST(req)
    expect(res.status).toBe(400)
  })

  it('A2 — invalid token → 401', async () => {
    const getUser = vi
      .fn()
      .mockResolvedValue({ data: null, error: { message: 'invalid_jwt' } })
    vi.doMock('@/lib/supabase-client', () => ({
      serverSecretClient: () => ({ auth: { getUser } }),
    }))
    const { POST } = await import('@/app/api/ops/login/finalize-hash/route')
    const req = new Request('http://test/api/ops/login/finalize-hash', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ access_token: 'bad-token' }),
    })
    const res = await POST(req)
    expect(res.status).toBe(401)
  })

  it('A3 — valid token + non-allowlisted UUID → 403 + no cookie', async () => {
    const getUser = vi.fn().mockResolvedValue({
      data: { user: { id: '11111111-1111-4111-8111-111111111111' } },
      error: null,
    })
    vi.doMock('@/lib/supabase-client', () => ({
      serverSecretClient: () => ({ auth: { getUser } }),
    }))
    const { POST } = await import('@/app/api/ops/login/finalize-hash/route')
    const req = new Request('http://test/api/ops/login/finalize-hash', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ access_token: 'real-token' }),
    })
    const res = await POST(req)
    expect(res.status).toBe(403)
    expect(res.headers.get('set-cookie') ?? '').not.toContain('viabe_ops_jwt')
  })

  it('A4 — valid token + allowlisted UUID → 200 + cookie path=/team + safe next', async () => {
    const getUser = vi.fn().mockResolvedValue({
      data: { user: { id: FAZAL_UUID } },
      error: null,
    })
    vi.doMock('@/lib/supabase-client', () => ({
      serverSecretClient: () => ({ auth: { getUser } }),
    }))
    const { POST } = await import('@/app/api/ops/login/finalize-hash/route')
    const req = new Request('http://test/api/ops/login/finalize-hash', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ access_token: 'real-token', next: '/team/onboard' }),
    })
    const res = await POST(req)
    expect(res.status).toBe(200)
    const body = (await res.json()) as { ok: boolean; next: string }
    expect(body.ok).toBe(true)
    expect(body.next).toBe('/team/onboard')
    const setCookie = res.headers.get('set-cookie') ?? ''
    expect(setCookie).toContain('viabe_ops_jwt=')
    expect(setCookie).toContain('Path=/team')
    // VT-236: 7-day Max-Age
    expect(setCookie).toMatch(/Max-Age=604800/)
  })
})
