/**
 * VT-411 — the browser-side ownership-verify flow logic (lib/ownership-verify.ts).
 *
 * The load-bearing invariants pinned here:
 *   - every fn posts to its /api/team proxy route with the exact body shape (the browser NEVER sees
 *     INTERNAL_API_SECRET — that lives server-side in the proxy);
 *   - owner_channel_verified is the SOLE signal ownership is proven — start/confirm/din fail CLOSED
 *     (ok:false / ownerChannelVerified:false) on non-2xx / throw and NEVER fake a proven owner;
 *   - the DIN + public-phone format gates are pure (8 digits / +91 mobile).
 */

import { describe, expect, it, vi } from 'vitest'

import {
  confirmOwnershipOtp,
  isValidDinFormat,
  isValidPublicPhoneFormat,
  startOwnershipOtp,
  verifyOwnerViaDin,
} from '@/lib/ownership-verify'

function resp(status: number, body: unknown = {}): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response
}

describe('VT-411 startOwnershipOtp', () => {
  it('200 → ok; posts to the proxy route with tenant_id + public_phone', async () => {
    const f = vi.fn().mockResolvedValue(resp(200, { ok: true, status: 'pending' }))
    const r = await startOwnershipOtp('t1', '+919876543210', f)
    expect(r).toEqual({ ok: true, status: 'pending' })
    const [url, init] = f.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/team/onboard/ownership/otp/start')
    expect(JSON.parse(init.body as string)).toEqual({ tenant_id: 't1', public_phone: '+919876543210' })
  })

  it('fails CLOSED on non-2xx', async () => {
    const f = vi.fn().mockResolvedValue(resp(503))
    expect(await startOwnershipOtp('t1', '+919876543210', f)).toEqual({ ok: false, status: 'http_503' })
  })

  it('fails CLOSED on throw', async () => {
    const f = vi.fn().mockRejectedValue(new Error('down'))
    expect(await startOwnershipOtp('t1', '+919876543210', f)).toEqual({ ok: false, status: 'error' })
  })
})

describe('VT-411 confirmOwnershipOtp', () => {
  it('owner_channel_verified true → verified; posts code + public_phone', async () => {
    const f = vi.fn().mockResolvedValue(resp(200, { owner_channel_verified: true }))
    const r = await confirmOwnershipOtp('t1', '+919876543210', '123456', f)
    expect(r).toEqual({ ownerChannelVerified: true, reason: 'ok' })
    const [url, init] = f.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/team/onboard/ownership/otp/confirm')
    expect(JSON.parse(init.body as string)).toEqual({
      tenant_id: 't1',
      public_phone: '+919876543210',
      code: '123456',
    })
  })

  it('owner_channel_verified false → NOT verified (no faked owner)', async () => {
    const f = vi.fn().mockResolvedValue(resp(200, { owner_channel_verified: false, reason: 'invalid_code' }))
    const r = await confirmOwnershipOtp('t1', '+919876543210', '000000', f)
    expect(r.ownerChannelVerified).toBe(false)
    expect(r.reason).toBe('invalid_code')
  })

  it('fails CLOSED on non-2xx', async () => {
    const f = vi.fn().mockResolvedValue(resp(403))
    expect(await confirmOwnershipOtp('t1', '+919876543210', '1', f)).toEqual({
      ownerChannelVerified: false,
      reason: 'http_403',
    })
  })

  it('fails CLOSED on throw', async () => {
    const f = vi.fn().mockRejectedValue(new Error('down'))
    expect(await confirmOwnershipOtp('t1', '+919876543210', '1', f)).toEqual({
      ownerChannelVerified: false,
      reason: 'error',
    })
  })
})

describe('VT-411 verifyOwnerViaDin', () => {
  it('owner_channel_verified true → verified; posts din + cin + reason', async () => {
    const f = vi.fn().mockResolvedValue(resp(200, { owner_channel_verified: true }))
    const r = await verifyOwnerViaDin('t1', '01234567', 'CIN1', 'director', f)
    expect(r).toEqual({ ownerChannelVerified: true, reason: 'ok' })
    const [url, init] = f.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/team/onboard/ownership/din')
    expect(JSON.parse(init.body as string)).toEqual({
      tenant_id: 't1',
      din: '01234567',
      cin: 'CIN1',
      reason: 'director',
    })
  })

  it('fails CLOSED on non-2xx (no faked owner)', async () => {
    const f = vi.fn().mockResolvedValue(resp(502))
    expect(await verifyOwnerViaDin('t1', '01234567', '', '', f)).toEqual({
      ownerChannelVerified: false,
      reason: 'http_502',
    })
  })

  it('fails CLOSED on throw', async () => {
    const f = vi.fn().mockRejectedValue(new Error('down'))
    expect(await verifyOwnerViaDin('t1', '01234567', '', '', f)).toEqual({
      ownerChannelVerified: false,
      reason: 'error',
    })
  })
})

describe('VT-411 format gates', () => {
  it('isValidDinFormat accepts exactly 8 digits', () => {
    expect(isValidDinFormat('01234567')).toBe(true)
    expect(isValidDinFormat(' 01234567 ')).toBe(true) // trimmed
    expect(isValidDinFormat('1234567')).toBe(false) // 7 digits
    expect(isValidDinFormat('012345678')).toBe(false) // 9 digits
    expect(isValidDinFormat('0123456A')).toBe(false) // non-digit
    expect(isValidDinFormat('')).toBe(false)
  })

  it('isValidPublicPhoneFormat accepts +91 mobile only', () => {
    expect(isValidPublicPhoneFormat('+919876543210')).toBe(true)
    expect(isValidPublicPhoneFormat(' +916012345678 ')).toBe(true) // trimmed, starts 6
    expect(isValidPublicPhoneFormat('+915012345678')).toBe(false) // starts 5
    expect(isValidPublicPhoneFormat('9876543210')).toBe(false) // no +91
    expect(isValidPublicPhoneFormat('+91987654321')).toBe(false) // 9 digits
    expect(isValidPublicPhoneFormat('')).toBe(false)
  })
})
