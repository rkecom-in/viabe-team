import { beforeEach, describe, expect, it, vi } from 'vitest'
import { NextRequest } from 'next/server'

// The route's collaborators are mocked: the route's OWN behaviour (403 bad-sig,
// Q1 conditional 200/502, 400 malformed) is under test — not the HMAC crypto or
// the network call.
vi.mock('@/lib/razorpay', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/razorpay')>()
  return { ...actual, verifyRazorpaySignature: vi.fn() }
})
vi.mock('@/lib/orchestrator-client', () => ({
  forwardRazorpayEvent: vi.fn(),
}))

import { forwardRazorpayEvent } from '@/lib/orchestrator-client'
import { verifyRazorpaySignature } from '@/lib/razorpay'
import { POST } from '@/app/api/team/razorpay/webhook/route'

const verifyMock = vi.mocked(verifyRazorpaySignature)
const forwardMock = vi.mocked(forwardRazorpayEvent)

function razorpayRequest(
  body = JSON.stringify({ id: 'evt_1', event: 'subscription.charged', payload: {} }),
): NextRequest {
  return new NextRequest('http://localhost:3000/api/team/razorpay/webhook', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-razorpay-signature': 'sig' },
    body,
  })
}

beforeEach(() => {
  verifyMock.mockReset()
  forwardMock.mockReset()
})

describe('Razorpay webhook (VT-89)', () => {
  it('bad signature -> 403, no forward', async () => {
    verifyMock.mockReturnValue(false)
    const res = await POST(razorpayRequest())
    expect(res.status).toBe(403)
    expect(forwardMock).not.toHaveBeenCalled()
  })

  it('verified + orchestrator durably records -> 200', async () => {
    verifyMock.mockReturnValue(true)
    forwardMock.mockResolvedValue({ ok: true, status: 'processed' })
    const res = await POST(razorpayRequest())
    expect(res.status).toBe(200)
    expect(forwardMock).toHaveBeenCalledOnce()
  })

  it('Q1: orchestrator unavailable -> 502 so Razorpay retries (event not silently dropped)', async () => {
    verifyMock.mockReturnValue(true)
    forwardMock.mockResolvedValue({ ok: false, status: 'error' })
    const res = await POST(razorpayRequest())
    expect(res.status).toBe(502)
  })

  it('verified but malformed body -> 400, no forward', async () => {
    verifyMock.mockReturnValue(true)
    const res = await POST(razorpayRequest('not json'))
    expect(res.status).toBe(400)
    expect(forwardMock).not.toHaveBeenCalled()
  })

  it('verified but missing event id/type -> 400', async () => {
    verifyMock.mockReturnValue(true)
    const res = await POST(razorpayRequest(JSON.stringify({ payload: {} })))
    expect(res.status).toBe(400)
  })
})
