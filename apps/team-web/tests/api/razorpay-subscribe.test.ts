import { beforeEach, describe, expect, it, vi } from 'vitest'
import { NextRequest } from 'next/server'

// Mock the 2 auth gates (keep their real error classes for the route's instanceof
// checks) + the orchestrator forward. The route's OWN behaviour (path order, tenant
// always server-derived, IDOR-safe, status mapping) is under test. VT-416 PR-1 dropped
// the path-3 FAZAL_TENANT_ID fallback, so requireFazal is no longer wired here.
vi.mock('@/lib/auth/require-owner-session', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/auth/require-owner-session')>()
  return { ...actual, requireOwnerSession: vi.fn() }
})
vi.mock('@/lib/auth/verify-trial-end-token', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/auth/verify-trial-end-token')>()
  return { ...actual, verifyTrialEndToken: vi.fn() }
})
vi.mock('@/lib/orchestrator-client', () => ({ forwardSubscribe: vi.fn() }))

import { OwnerUnauthorizedError, requireOwnerSession } from '@/lib/auth/require-owner-session'
import { TrialEndTokenError, verifyTrialEndToken } from '@/lib/auth/verify-trial-end-token'
import { forwardSubscribe } from '@/lib/orchestrator-client'
import { POST } from '@/app/api/team/razorpay/subscribe/route'

const sessionMock = vi.mocked(requireOwnerSession)
const tokenMock = vi.mocked(verifyTrialEndToken)
const fwdMock = vi.mocked(forwardSubscribe)

const OWNER_TENANT = 'owner-tenant-1'
const TOKEN_TENANT = 'token-tenant-2'
// Synthetic two-owner canary tenants (VT-416 PR-1): two distinct portal owners that must
// each bill THEIR OWN tenant and never cross-attribute (and never Fazal's).
const OWNER_A_TENANT = 'owner-a-tenant'
const OWNER_B_TENANT = 'owner-b-tenant'
const FAZAL_TENANT = 'fazal-tenant-3'

function req(body: unknown = { plan_tier: 'founding' }): NextRequest {
  return new NextRequest('http://localhost:3000/api/team/razorpay/subscribe', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  })
}

beforeEach(() => {
  sessionMock.mockReset()
  tokenMock.mockReset()
  fwdMock.mockReset()
  // FAZAL_TENANT_ID is set in the env to PROVE the route no longer reads it (no fallback):
  // an unauth/no-token call must 401, never silently bill this tenant.
  process.env.FAZAL_TENANT_ID = FAZAL_TENANT
  sessionMock.mockRejectedValue(new OwnerUnauthorizedError('no session')) // default: no portal session
  fwdMock.mockResolvedValue({ ok: true, status: 'created', razorpaySubscriptionId: 'sub_x' })
})

describe('Razorpay subscribe — 2-path auth resolver (VT-91; VT-416 PR-1 dropped path-3)', () => {
  it('portal session -> tenant from the session claim', async () => {
    sessionMock.mockResolvedValue({ tenantId: OWNER_TENANT })
    const res = await POST(req({ plan_tier: 'founding' }))
    expect(res.status).toBe(200)
    expect(fwdMock).toHaveBeenCalledWith(OWNER_TENANT, 'founding', null) // in-app path → no jti
  })

  it('deep-link token -> tenant + jti from the verified token (session absent)', async () => {
    tokenMock.mockResolvedValue({ tenantId: TOKEN_TENANT, jti: 'jti-tok' })
    const res = await POST(req({ plan_tier: 'standard', token: 'jwt' }))
    expect(res.status).toBe(200)
    expect(tokenMock).toHaveBeenCalledWith('jwt')
    // VT-332: the token's jti is forwarded for the single-use consume.
    expect(fwdMock).toHaveBeenCalledWith(TOKEN_TENANT, 'standard', 'jti-tok')
  })

  // ── VT-416 PR-1: 2-owner cross-attribution canary ──────────────────────────────────
  it('2-owner canary: two distinct owner sessions each bill THEIR OWN tenant, never crossed', async () => {
    // Owner A's session resolves to A's tenant.
    sessionMock.mockResolvedValueOnce({ tenantId: OWNER_A_TENANT })
    const resA = await POST(req({ plan_tier: 'founding' }))
    expect(resA.status).toBe(200)
    expect(fwdMock).toHaveBeenNthCalledWith(1, OWNER_A_TENANT, 'founding', null)

    // Owner B's session resolves to B's tenant — a DIFFERENT tenant.
    sessionMock.mockResolvedValueOnce({ tenantId: OWNER_B_TENANT })
    const resB = await POST(req({ plan_tier: 'standard' }))
    expect(resB.status).toBe(200)
    expect(fwdMock).toHaveBeenNthCalledWith(2, OWNER_B_TENANT, 'standard', null)

    // Neither subscription was ever attributed to the other owner, and NEITHER touched
    // Fazal's tenant — the deleted path-3 fallback can no longer cross-bill.
    expect(OWNER_A_TENANT).not.toBe(OWNER_B_TENANT)
    const forwardedTenants = fwdMock.mock.calls.map((c) => c[0])
    expect(forwardedTenants).toEqual([OWNER_A_TENANT, OWNER_B_TENANT])
    expect(forwardedTenants).not.toContain(FAZAL_TENANT)
  })

  it('VT-416: unauth/no-token call 401s — the path-3 FAZAL_TENANT_ID fallback is GONE (no silent Fazal bill)', async () => {
    // No portal session (default) and no deep-link token. Pre-VT-416 this fell through to
    // requireFazal()+FAZAL_TENANT_ID and could bill Fazal's tenant. Now it fails closed as
    // an unauthenticated caller — 401 (Cowork gate ruling: 401 is the accurate semantic;
    // 503 would false-signal "service DOWN" to monitoring).
    const res = await POST(req({ plan_tier: 'pro' }))
    expect(res.status).toBe(401)
    const body = (await res.json()) as { ok: boolean; reason: string }
    expect(body).toEqual({ ok: false, reason: 'unauthorized' })
    // The critical assertion: NOTHING was forwarded — Fazal's tenant is never billed.
    expect(fwdMock).not.toHaveBeenCalled()
  })

  it('IDOR: a client tenant_id in the body is IGNORED (tenant from the verified token)', async () => {
    tokenMock.mockResolvedValue({ tenantId: TOKEN_TENANT, jti: 'jti-tok' })
    await POST(req({ plan_tier: 'founding', token: 'jwt', tenant_id: 'attacker-tenant' }))
    expect(fwdMock).toHaveBeenCalledWith(TOKEN_TENANT, 'founding', 'jti-tok') // never 'attacker-tenant'
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
