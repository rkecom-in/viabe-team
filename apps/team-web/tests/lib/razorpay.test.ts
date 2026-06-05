import { createHmac } from 'crypto'

import { describe, expect, it } from 'vitest'

import { verifyRazorpaySignature } from '@/lib/razorpay'

describe('verifyRazorpaySignature (VT-89 — real HMAC-SHA256)', () => {
  const secret = 'whsec_test_vt89'
  const body = JSON.stringify({ id: 'evt_x', event: 'subscription.charged', payload: {} })
  const sig = createHmac('sha256', secret).update(body).digest('hex')

  it('accepts a correct signature over the raw body', () => {
    expect(verifyRazorpaySignature(sig, body, secret)).toBe(true)
  })

  it('rejects a tampered body (HMAC mismatch)', () => {
    expect(verifyRazorpaySignature(sig, body + 'x', secret)).toBe(false)
  })

  it('rejects a wrong secret', () => {
    expect(verifyRazorpaySignature(sig, body, 'wrong-secret')).toBe(false)
  })

  it('rejects a missing signature or empty secret (fail-closed)', () => {
    expect(verifyRazorpaySignature(null, body, secret)).toBe(false)
    expect(verifyRazorpaySignature(sig, body, '')).toBe(false)
    expect(verifyRazorpaySignature('', body, secret)).toBe(false)
  })

  it('rejects a length-mismatched signature without throwing (timingSafeEqual guard)', () => {
    expect(verifyRazorpaySignature('deadbeef', body, secret)).toBe(false)
  })
})
