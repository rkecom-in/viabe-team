import { beforeEach, describe, expect, it, vi } from 'vitest'
import { NextRequest } from 'next/server'

// Mock the 3 auth gates (keep their real error classes for the route's instanceof
// checks) + the orchestrator forward. The route's OWN behaviour (path order, tenant
// always server-derived, IDOR-safe, status mapping) is under test.
vi.mock('@/lib/auth/require-fazal', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/auth/require-fazal')>()
  return { ...actual, requireFazal: vi.fn() }
})
vi.mock('@/lib/auth/require-owner-session', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/auth/require-owner-session')>()
  return { ...actual, requireOwnerSession: vi.fn() }
})
vi.mock('@/lib/auth/verify-trial-end-token', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/auth/verify-trial-end-token')>()
  return { ...actual, verifyTrialEndToken: vi.fn() }
})
vi.mock('@/lib/orchestrator-client', () => ({ forwardSubscribe: vi.fn() }))

import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import { OwnerUnauthorizedError, requireOwnerSession } from '@/lib/auth/require-owner-session'
import { TrialEndTokenError, verifyTrialEndToken } from '@/lib/auth/verify-trial-end-token'
import { forwardSubscribe } from '@/lib/orchestrator-client'
import { POST } from '@/app/api/team/razorpay/subscribe/route'

const fazalMock = vi.mocked(requireFazal)
const sessionMock = vi.mocked(requireOwnerSession)
const tokenMock = vi.mocked(verifyTrialEndToken)
const fwdMock = vi.mocked(forwardSubscribe)

const OWNER_TENANT = 'owner-tenant-1'
const TOKEN_TENANT = 'token-tenant-2'
const FAZAL_TENANT = 'fazal-tenant-3'

function req(body: unknown = { plan_tier: 'founding' }): NextRequest {
  return new NextRequest('http://localhost:3000/api/team/razorpay/subscribe', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  })
}

beforeEach(() => {
  fazalMock.mockReset()
  sessionMock.mockReset()
  tokenMock.mockReset()
  fwdMock.mockReset()
  process.env.FAZAL_TENANT_ID = FAZAL_TENANT
  sessionMock.mockRejectedValue(new OwnerUnauthorizedError('no session')) // default: no portal session
  fwdMock.mockResolvedValue({ ok: true, status: 'created', razorpaySubscriptionId: 'sub_x' })
})

describe('Razorpay subscribe — 3-path auth resolver (VT-91)', () => {
  it('portal session -> tenant from the session claim', async () => {
    sessionMock.mockResolvedValue({ tenantId: OWNER_TENANT })
    const res = await POST(req({ plan_tier: 'founding' }))
    expect(res.status).toBe(200)
    expect(fwdMock).toHaveBeenCalledWith(OWNER_TENANT, 'founding')
  })

  it('deep-link token -> tenant from the verified token (session absent)', async () => {
    tokenMock.mockResolvedValue({ tenantId: TOKEN_TENANT })
    const res = await POST(req({ plan_tier: 'standard', token: 'jwt' }))
    expect(res.status).toBe(200)
    expect(tokenMock).toHaveBeenCalledWith('jwt')
    expect(fwdMock).toHaveBeenCalledWith(TOKEN_TENANT, 'standard')
  })

  it('fazal fallback -> FAZAL_TENANT_ID (no session, no token)', async () => {
    fazalMock.mockResolvedValue(undefined as never)
    const res = await POST(req({ plan_tier: 'pro' }))
    expect(res.status).toBe(200)
    expect(fwdMock).toHaveBeenCalledWith(FAZAL_TENANT, 'pro')
  })

  it('no auth on any path -> 401, no forward', async () => {
    fazalMock.mockRejectedValue(new UnauthorizedError('not fazal'))
    const res = await POST(req({ plan_tier: 'founding' }))
    expect(res.status).toBe(401)
    expect(fwdMock).not.toHaveBeenCalled()
  })

  it('IDOR: a client tenant_id in the body is IGNORED (tenant from the verified token)', async () => {
    tokenMock.mockResolvedValue({ tenantId: TOKEN_TENANT })
    await POST(req({ plan_tier: 'founding', token: 'jwt', tenant_id: 'attacker-tenant' }))
    expect(fwdMock).toHaveBeenCalledWith(TOKEN_TENANT, 'founding') // never 'attacker-tenant'
  })

  it('bad/expired token -> 401, no forward', async () => {
    tokenMock.mockRejectedValue(new TrialEndTokenError('expired'))
    const res = await POST(req({ plan_tier: 'founding', token: 'bad' }))
    expect(res.status).toBe(401)
    expect(fwdMock).not.toHaveBeenCalled()
  })

  it('missing plan_tier -> 400', async () => {
    sessionMock.mockResolvedValue({ tenantId: OWNER_TENANT })
    const res = await POST(req({}))
    expect(res.status).toBe(400)
  })

  it('orchestrator failure -> 502', async () => {
    sessionMock.mockResolvedValue({ tenantId: OWNER_TENANT })
    fwdMock.mockResolvedValue({ ok: false, status: 'error', razorpaySubscriptionId: null })
    const res = await POST(req({ plan_tier: 'founding' }))
    expect(res.status).toBe(502)
  })
})
