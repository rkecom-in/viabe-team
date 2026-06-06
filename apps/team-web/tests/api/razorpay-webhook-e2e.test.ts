/** VT-330 #3 — Razorpay webhook REAL-HMAC e2e. verifyRazorpaySignature is NOT mocked here (the
 * other route test mocks it); only the orchestrator forward is stubbed. Proves the raw bytes
 * survive req.text() → real HMAC verify intact, and the orchestrator-down → 502 path. */

import { createHmac } from 'crypto'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Mock ONLY the orchestrator forward — verifyRazorpaySignature stays REAL crypto.
vi.mock('@/lib/orchestrator-client', () => ({ forwardRazorpayEvent: vi.fn() }))

import { POST } from '@/app/api/team/razorpay/webhook/route'
import { forwardRazorpayEvent } from '@/lib/orchestrator-client'

const SECRET = 'vt330-test-webhook-secret'

function signedReq(rawBody: string, sig?: string): Request {
  const signature = sig ?? createHmac('sha256', SECRET).update(rawBody).digest('hex')
  return new Request('http://test/api/team/razorpay/webhook', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-razorpay-signature': signature },
    body: rawBody,
  })
}

beforeEach(() => {
  process.env.RZP_WEBHOOK_SECRET_DEV = SECRET
})
afterEach(() => {
  vi.restoreAllMocks()
  delete process.env.RZP_WEBHOOK_SECRET_DEV
})

describe('VT-330 webhook real-HMAC e2e', () => {
  it('real HMAC over the raw bytes survives req.text() → verify passes → forwards → 200', async () => {
    vi.mocked(forwardRazorpayEvent).mockResolvedValue({ ok: true, status: 200 } as never)
    const raw = JSON.stringify({ id: 'evt_1', event: 'subscription.charged', payload: { x: 1 } })
    const res = await POST(signedReq(raw) as never)
    expect(res.status).toBe(200)
    // the forward received the event parsed from the SAME raw bytes (no re-encode corruption).
    expect(forwardRazorpayEvent).toHaveBeenCalledWith('evt_1', 'subscription.charged', { x: 1 })
  })

  it('403 on a tampered signature (real verify, no mock)', async () => {
    const raw = JSON.stringify({ id: 'evt_2', event: 'x' })
    const res = await POST(signedReq(raw, 'deadbeefdeadbeef') as never)
    expect(res.status).toBe(403)
  })

  it('502 when the orchestrator is down (real HMAC passes, forward not ok)', async () => {
    vi.mocked(forwardRazorpayEvent).mockResolvedValue({ ok: false, status: 503 } as never)
    const raw = JSON.stringify({ id: 'evt_3', event: 'subscription.charged', payload: {} })
    const res = await POST(signedReq(raw) as never)
    expect(res.status).toBe(502) // never silently drop a financial event
  })
})
