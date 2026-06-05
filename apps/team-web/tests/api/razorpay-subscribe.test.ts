import { beforeEach, describe, expect, it, vi } from 'vitest'
import { NextRequest } from 'next/server'

vi.mock('@/lib/auth/require-fazal', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/auth/require-fazal')>()
  return { ...actual, requireFazal: vi.fn() }
})
vi.mock('@/lib/orchestrator-client', () => ({
  forwardSubscribe: vi.fn(),
}))

import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import { forwardSubscribe } from '@/lib/orchestrator-client'
import { POST } from '@/app/api/team/razorpay/subscribe/route'

const authMock = vi.mocked(requireFazal)
const fwdMock = vi.mocked(forwardSubscribe)

const FAZAL_TENANT = '11111111-1111-1111-1111-111111111111'

function subscribeRequest(body: unknown = { plan_tier: 'founding' }): NextRequest {
  return new NextRequest('http://localhost:3000/api/team/razorpay/subscribe', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  })
}

beforeEach(() => {
  authMock.mockReset()
  fwdMock.mockReset()
  process.env.FAZAL_TENANT_ID = FAZAL_TENANT
})

describe('Razorpay subscribe (VT-331)', () => {
  it('unauthorized -> 401, no forward', async () => {
    authMock.mockRejectedValue(new UnauthorizedError('no'))
    const res = await POST(subscribeRequest())
    expect(res.status).toBe(401)
    expect(fwdMock).not.toHaveBeenCalled()
  })

  it('authorized + created -> 200', async () => {
    authMock.mockResolvedValue(undefined as never)
    fwdMock.mockResolvedValue({ ok: true, status: 'created', razorpaySubscriptionId: 'sub_x' })
    const res = await POST(subscribeRequest())
    expect(res.status).toBe(200)
    expect(await res.json()).toMatchObject({ ok: true, status: 'created' })
  })

  it('server-derives tenant — a client tenant_id in the body is IGNORED (IDOR-safe)', async () => {
    authMock.mockResolvedValue(undefined as never)
    fwdMock.mockResolvedValue({ ok: true, status: 'created', razorpaySubscriptionId: 'sub_x' })
    await POST(subscribeRequest({ plan_tier: 'founding', tenant_id: 'attacker-tenant' }))
    // forward called with the SERVER tenant, never the body's tenant_id
    expect(fwdMock).toHaveBeenCalledWith(FAZAL_TENANT, 'founding')
  })

  it('orchestrator failure -> 502', async () => {
    authMock.mockResolvedValue(undefined as never)
    fwdMock.mockResolvedValue({ ok: false, status: 'error', razorpaySubscriptionId: null })
    const res = await POST(subscribeRequest())
    expect(res.status).toBe(502)
  })

  it('missing plan_tier -> 400, no forward', async () => {
    authMock.mockResolvedValue(undefined as never)
    const res = await POST(subscribeRequest({}))
    expect(res.status).toBe(400)
    expect(fwdMock).not.toHaveBeenCalled()
  })

  it('malformed body -> 400', async () => {
    authMock.mockResolvedValue(undefined as never)
    const res = await POST(subscribeRequest('not json'))
    expect(res.status).toBe(400)
  })
})
