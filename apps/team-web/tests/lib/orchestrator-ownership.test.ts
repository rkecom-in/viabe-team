/**
 * VT-411 — the orchestrator-client ownership-verification proxy fns (server-side).
 *
 * Pins: X-Internal-Secret on every call; the orchestrator path + body shape are exact; ALL THREE
 * fns fail CLOSED on non-2xx / throw and NEVER fake owner_channel_verified (a vendor failure can
 * not read as a proven owner). owner_channel_verified is the sole signal ownership is proven.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const ORIGINALS = {
  url: process.env.TEAM_ORCHESTRATOR_URL,
  secret: process.env.INTERNAL_API_SECRET,
}

beforeEach(() => {
  process.env.TEAM_ORCHESTRATOR_URL = 'http://orch:8001'
  process.env.INTERNAL_API_SECRET = 'sek'
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  for (const [k, v] of [
    ['TEAM_ORCHESTRATOR_URL', ORIGINALS.url],
    ['INTERNAL_API_SECRET', ORIGINALS.secret],
  ] as const) {
    if (v === undefined) delete process.env[k]
    else process.env[k] = v
  }
})

async function client() {
  return await import('@/lib/orchestrator-client')
}

function stub(status: number, body: Record<string, unknown> = {}) {
  const f = vi.fn(async () => ({ ok: status >= 200 && status < 300, status, json: async () => body }))
  vi.stubGlobal('fetch', f)
  return f
}

describe('VT-411 startOwnershipOtp', () => {
  it('200 → ok; sends X-Internal-Secret + tenant_id/public_phone to the orchestrator path', async () => {
    const f = stub(200, { verification_sid: 'VEx', status: 'pending' })
    const { startOwnershipOtp } = await client()
    const r = await startOwnershipOtp('t1', '+919876543210')
    expect(r).toEqual({ ok: true, verificationSid: 'VEx', status: 'pending' })
    const [url, init] = (f.mock.calls[0] as unknown) as [string, RequestInit]
    expect(url).toBe('http://orch:8001/api/orchestrator/onboard/ownership/otp/start')
    expect((init.headers as Record<string, string>)['X-Internal-Secret']).toBe('sek')
    expect(JSON.parse(init.body as string)).toEqual({ tenant_id: 't1', public_phone: '+919876543210' })
  })

  it('fails CLOSED on non-2xx (no dispatch claimed)', async () => {
    stub(503)
    const { startOwnershipOtp } = await client()
    expect(await startOwnershipOtp('t1', '+919876543210')).toEqual({
      ok: false,
      verificationSid: null,
      status: 'http_503',
    })
  })

  it('fails CLOSED on throw', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('down') }))
    const { startOwnershipOtp } = await client()
    expect(await startOwnershipOtp('t1', '+919876543210')).toEqual({
      ok: false,
      verificationSid: null,
      status: 'error',
    })
  })
})

describe('VT-411 confirmOwnershipOtp', () => {
  it('owner_channel_verified true → ok:true; exact path + body', async () => {
    const f = stub(200, { owner_channel_verified: true })
    const { confirmOwnershipOtp } = await client()
    const r = await confirmOwnershipOtp('t1', '+919876543210', '123456')
    expect(r).toEqual({ ok: true, ownerChannelVerified: true, reason: 'ok' })
    const [url, init] = (f.mock.calls[0] as unknown) as [string, RequestInit]
    expect(url).toBe('http://orch:8001/api/orchestrator/onboard/ownership/otp/confirm')
    expect((init.headers as Record<string, string>)['X-Internal-Secret']).toBe('sek')
    expect(JSON.parse(init.body as string)).toEqual({
      tenant_id: 't1',
      public_phone: '+919876543210',
      code: '123456',
    })
  })

  it('owner_channel_verified false → ok:false (NEVER a faked proven owner)', async () => {
    stub(200, { owner_channel_verified: false, reason: 'invalid_code' })
    const { confirmOwnershipOtp } = await client()
    const r = await confirmOwnershipOtp('t1', '+919876543210', '000000')
    expect(r.ok).toBe(false)
    expect(r.ownerChannelVerified).toBe(false)
    expect(r.reason).toBe('invalid_code')
  })

  it('fails CLOSED on non-2xx', async () => {
    stub(403)
    const { confirmOwnershipOtp } = await client()
    expect(await confirmOwnershipOtp('t1', '+919876543210', '1')).toEqual({
      ok: false,
      ownerChannelVerified: false,
      reason: 'http_403',
    })
  })

  it('fails CLOSED on throw', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('down') }))
    const { confirmOwnershipOtp } = await client()
    expect(await confirmOwnershipOtp('t1', '+919876543210', '1')).toEqual({
      ok: false,
      ownerChannelVerified: false,
      reason: 'error',
    })
  })
})

describe('VT-411 verifyOwnerViaDin', () => {
  it('owner_channel_verified true → ok:true; exact path + body', async () => {
    const f = stub(200, { owner_channel_verified: true })
    const { verifyOwnerViaDin } = await client()
    const r = await verifyOwnerViaDin('t1', '01234567', 'U72900KA2020PTC000000', 'director')
    expect(r).toEqual({ ok: true, ownerChannelVerified: true, reason: 'ok' })
    const [url, init] = (f.mock.calls[0] as unknown) as [string, RequestInit]
    expect(url).toBe('http://orch:8001/api/orchestrator/onboard/ownership/din')
    expect((init.headers as Record<string, string>)['X-Internal-Secret']).toBe('sek')
    expect(JSON.parse(init.body as string)).toEqual({
      tenant_id: 't1',
      din: '01234567',
      cin: 'U72900KA2020PTC000000',
      reason: 'director',
    })
  })

  it('owner_channel_verified false → ok:false (NEVER a faked proven owner)', async () => {
    stub(200, { owner_channel_verified: false, reason: 'din_mismatch' })
    const { verifyOwnerViaDin } = await client()
    const r = await verifyOwnerViaDin('t1', '01234567', '', '')
    expect(r.ok).toBe(false)
    expect(r.ownerChannelVerified).toBe(false)
    expect(r.reason).toBe('din_mismatch')
  })

  it('fails CLOSED on non-2xx', async () => {
    stub(502)
    const { verifyOwnerViaDin } = await client()
    expect(await verifyOwnerViaDin('t1', '01234567', '', '')).toEqual({
      ok: false,
      ownerChannelVerified: false,
      reason: 'http_502',
    })
  })

  it('fails CLOSED on throw', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('down') }))
    const { verifyOwnerViaDin } = await client()
    expect(await verifyOwnerViaDin('t1', '01234567', '', '')).toEqual({
      ok: false,
      ownerChannelVerified: false,
      reason: 'error',
    })
  })
})
