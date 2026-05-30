/**
 * VT-250 — request-otp + verify-otp route tests.
 *
 * Mocks the orchestrator boundary (global fetch) and the tenant-resolution
 * module so the routes run without a live orchestrator or Supabase.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { _resetOtpRateLimit } from '@/lib/auth/otp-rate-limit'

// Stub the phone→tenant resolver (covers the Supabase secret-client read).
vi.mock('@/lib/auth/resolve-owner-tenant', () => ({
  resolveOwnerTenant: vi.fn(),
}))

const ORIG_OWNER_SECRET = process.env.OWNER_JWT_SECRET

function jsonReq(body: unknown, ip = '9.9.9.9'): Request {
  return new Request('http://test/api/team/auth/x', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-forwarded-for': ip },
    body: JSON.stringify(body),
  })
}

beforeEach(() => {
  _resetOtpRateLimit()
  process.env.OWNER_JWT_SECRET = 'vt250-route-' + 'r'.repeat(40)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
  if (ORIG_OWNER_SECRET === undefined) delete process.env.OWNER_JWT_SECRET
  else process.env.OWNER_JWT_SECRET = ORIG_OWNER_SECRET
})

describe('POST /api/team/auth/request-otp', () => {
  it('400 on an unnormalizable phone', async () => {
    const { POST } = await import('@/app/api/team/auth/request-otp/route')
    const res = await POST(jsonReq({ phone: 'nope' }))
    expect(res.status).toBe(400)
  })

  it('200 { sent: true } on a successful verify-start (generic — no tenant leak)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ status: 'pending', verification_sid: 'VEx' }),
          { status: 200 },
        ),
      ),
    )
    const { POST } = await import('@/app/api/team/auth/request-otp/route')
    const res = await POST(jsonReq({ phone: '9876543210' }))
    expect(res.status).toBe(200)
    const body = (await res.json()) as { sent?: boolean }
    expect(body.sent).toBe(true)
  })

  it('429 when the per-IP cap is exceeded (D4)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ status: 'pending' }), { status: 200 }),
      ),
    )
    const { POST } = await import('@/app/api/team/auth/request-otp/route')
    let res!: Response
    for (let i = 0; i < 6; i++) {
      res = await POST(jsonReq({ phone: `+91980000${1000 + i}` }, '5.5.5.5'))
    }
    expect(res.status).toBe(429)
  })
})

describe('POST /api/team/auth/verify-otp', () => {
  it('401 (generic) when the code is denied', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ approved: false, status: 'denied', verification_sid: 'VEx' }),
          { status: 200 },
        ),
      ),
    )
    const { POST } = await import('@/app/api/team/auth/verify-otp/route')
    const res = await POST(jsonReq({ phone: '9876543210', code: '000000' }))
    expect(res.status).toBe(401)
  })

  it('401 (generic) when approved but no tenant owns the phone (fails closed, no leak)', async () => {
    const { resolveOwnerTenant } = await import('@/lib/auth/resolve-owner-tenant')
    ;(resolveOwnerTenant as ReturnType<typeof vi.fn>).mockResolvedValue(null)
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ approved: true, status: 'approved', verification_sid: 'VEx' }),
          { status: 200 },
        ),
      ),
    )
    const { POST } = await import('@/app/api/team/auth/verify-otp/route')
    const res = await POST(jsonReq({ phone: '9876543210', code: '654321' }))
    expect(res.status).toBe(401)
  })

  it('200 + viabe_team_session cookie on approved code with a resolved tenant', async () => {
    const { resolveOwnerTenant } = await import('@/lib/auth/resolve-owner-tenant')
    ;(resolveOwnerTenant as ReturnType<typeof vi.fn>).mockResolvedValue(
      '22222222-2222-4222-8222-222222222222',
    )
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ approved: true, status: 'approved', verification_sid: 'VEx' }),
          { status: 200 },
        ),
      ),
    )
    const { POST } = await import('@/app/api/team/auth/verify-otp/route')
    const res = await POST(jsonReq({ phone: '9876543210', code: '654321' }))
    expect(res.status).toBe(200)
    const setCookie = res.headers.get('set-cookie') ?? ''
    expect(setCookie).toContain('viabe_team_session=')
    expect(setCookie.toLowerCase()).toContain('httponly')
    expect(setCookie.toLowerCase()).toContain('samesite=strict')
    expect(setCookie.toLowerCase()).toContain('secure')
  })
})
