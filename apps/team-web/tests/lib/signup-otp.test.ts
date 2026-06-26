/** VT-96 — the signup OTP flow: Bearer-token threading + generic (non-enumerating) errors. */

import { describe, expect, it, vi } from 'vitest'

import { requestSignupOtp, verifyOtpAndCreate } from '@/lib/signup-otp'

function resp(status: number, body: unknown = {}): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response
}

const PHONE = '+919876543210'
const payload = { whatsapp_number: PHONE, business_name: 'Chai Co', preferred_language: 'en' }

describe('VT-96 requestSignupOtp', () => {
  it('200 → ok, posts to request-otp', async () => {
    const f = vi.fn().mockResolvedValue(resp(200))
    expect(await requestSignupOtp(PHONE, f)).toEqual({ ok: true })
    expect(f).toHaveBeenCalledWith(
      '/api/team/auth/request-otp',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('429 → rate_limited', async () => {
    const f = vi.fn().mockResolvedValue(resp(429))
    expect(await requestSignupOtp(PHONE, f)).toEqual({ ok: false, error: 'rate_limited' })
  })

  it('5xx → generic', async () => {
    const f = vi.fn().mockResolvedValue(resp(503))
    expect(await requestSignupOtp(PHONE, f)).toEqual({ ok: false, error: 'generic' })
  })
})

describe('VT-96 verifyOtpAndCreate', () => {
  it('happy path threads Authorization: Bearer <token> to signup; code never in the signup body', async () => {
    const f = vi
      .fn()
      .mockResolvedValueOnce(resp(200, { token: 'tok_abc' })) // verify-otp-for-signup
      .mockResolvedValueOnce(resp(201, { tenant_id: 'ten_123' })) // signup → new tenant_id (VT-411)
    expect(await verifyOtpAndCreate(payload, '123456', f)).toEqual({ ok: true, tenantId: 'ten_123' })
    const [url, init] = f.mock.calls[1] as [string, RequestInit]
    expect(url).toBe('/api/team/signup')
    expect(init.headers).toMatchObject({ authorization: 'Bearer tok_abc' })
    expect(init.body).toBe(JSON.stringify(payload)) // the OTP code is NOT forwarded to signup
  })

  it('VT-411 — 201 with no tenant_id → ok with tenantId null (degrades, never throws)', async () => {
    const f = vi
      .fn()
      .mockResolvedValueOnce(resp(200, { token: 'tok' }))
      .mockResolvedValueOnce(resp(201, {}))
    expect(await verifyOtpAndCreate(payload, '123456', f)).toEqual({ ok: true, tenantId: null })
  })

  it('VT-449 — the create POST carries extra payload fields (confirmed cin) verbatim', async () => {
    const withCin = { ...payload, verified_gstin: '29ABCDE1234F1Z5', cin: 'U22210KA1995PLC012345' }
    const f = vi
      .fn()
      .mockResolvedValueOnce(resp(200, { token: 'tok' }))
      .mockResolvedValueOnce(resp(201, { tenant_id: 'ten_1' }))
    await verifyOtpAndCreate(withCin, '123456', f)
    const [, init] = f.mock.calls[1] as [string, RequestInit]
    expect(JSON.parse(init.body as string).cin).toBe('U22210KA1995PLC012345')
  })

  it('verify 429 → rate_limited, NO signup call', async () => {
    const f = vi.fn().mockResolvedValueOnce(resp(429))
    expect(await verifyOtpAndCreate(payload, '000000', f)).toEqual({
      ok: false,
      error: 'rate_limited',
    })
    expect(f).toHaveBeenCalledTimes(1)
  })

  it('invalid OR expired code → invalid_code (no enumeration), NO signup call', async () => {
    const f = vi.fn().mockResolvedValueOnce(resp(401))
    expect(await verifyOtpAndCreate(payload, '000000', f)).toEqual({
      ok: false,
      error: 'invalid_code',
    })
    expect(f).toHaveBeenCalledTimes(1)
  })

  it('verify ok but token missing → invalid_code, NO signup call', async () => {
    const f = vi.fn().mockResolvedValueOnce(resp(200, {}))
    expect(await verifyOtpAndCreate(payload, '123456', f)).toEqual({
      ok: false,
      error: 'invalid_code',
    })
    expect(f).toHaveBeenCalledTimes(1)
  })

  it('signup duplicate → duplicate', async () => {
    const f = vi
      .fn()
      .mockResolvedValueOnce(resp(200, { token: 'tok' }))
      .mockResolvedValueOnce(resp(409, { detail: { code: 'duplicate' } }))
    expect(await verifyOtpAndCreate(payload, '123456', f)).toEqual({ ok: false, error: 'duplicate' })
  })

  it('signup other failure → generic', async () => {
    const f = vi
      .fn()
      .mockResolvedValueOnce(resp(200, { token: 'tok' }))
      .mockResolvedValueOnce(resp(500, {}))
    expect(await verifyOtpAndCreate(payload, '123456', f)).toEqual({ ok: false, error: 'generic' })
  })
})
