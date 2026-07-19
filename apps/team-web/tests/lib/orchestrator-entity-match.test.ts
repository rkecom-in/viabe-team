/**
 * VT-406 (Part B) — the orchestrator-client entity-match proxy fns (server-side).
 *
 * Pins: X-Internal-Secret on every call; fetchEntityCandidates fails CLOSED to {candidates: []}
 * on non-2xx / throw (never stalls signup); confirmEntity fails CLOSED to {ok:false, reason} and
 * NEVER fakes a verified result; gstin_verified is the only ok:true status.
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

describe('VT-406 fetchEntityCandidates', () => {
  it('200 → candidates; sends X-Internal-Secret + business_name/city to the orchestrator path', async () => {
    const f = stub(200, { candidates: [{ trade_name: 'X', source: 'web', candidate_gstin: 'G', legal_name: null, detail: 'd' }] })
    const { fetchEntityCandidates } = await client()
    const r = await fetchEntityCandidates('X', 'Bengaluru')
    expect(r).toEqual({
      ok: true,
      candidates: [{ trade_name: 'X', source: 'web', candidate_gstin: 'G', legal_name: null, detail: 'd' }],
      reason: 'ok',
    })
    const [url, init] = (f.mock.calls[0] as unknown) as [string, RequestInit]
    expect(url).toBe('http://orch:8001/api/orchestrator/onboard/entity-candidates')
    expect((init.headers as Record<string, string>)['X-Internal-Secret']).toBe('sek')
    expect(JSON.parse(init.body as string)).toEqual({ business_name: 'X', city: 'Bengaluru' })
  })

  it('fails CLOSED to [] on non-2xx', async () => {
    stub(403)
    const { fetchEntityCandidates } = await client()
    expect(await fetchEntityCandidates('X', 'Y')).toEqual({ ok: false, candidates: [], reason: 'http_403' })
  })

  it('fails CLOSED to [] on throw', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('down') }))
    const { fetchEntityCandidates } = await client()
    expect(await fetchEntityCandidates('X', 'Y')).toEqual({ ok: false, candidates: [], reason: 'error' })
  })
})

describe('VT-406 confirmEntity', () => {
  it('gstin_verified → ok:true with the authoritative name', async () => {
    stub(200, { ok: true, status: 'gstin_verified', name: 'OFFICIAL NAME LTD' })
    const { confirmEntity } = await client()
    expect(await confirmEntity('', 'G')).toEqual({
      ok: true,
      status: 'gstin_verified',
      reason: undefined,
      name: 'OFFICIAL NAME LTD',
    })
  })

  it('invalid_gstin → ok:false with the reason (UI collapses to generic reject)', async () => {
    stub(200, { ok: false, status: 'unverified', reason: 'invalid_gstin' })
    const { confirmEntity } = await client()
    const r = await confirmEntity('', 'G')
    expect(r.ok).toBe(false)
    expect(r.reason).toBe('invalid_gstin')
  })

  it('vendor_down → ok:false, reason vendor_down (retryable, NOT verified)', async () => {
    stub(200, { ok: false, reason: 'vendor_down' })
    const { confirmEntity } = await client()
    const r = await confirmEntity('', 'G')
    expect(r.ok).toBe(false)
    expect(r.reason).toBe('vendor_down')
  })

  it('fails CLOSED on non-2xx (NEVER a faked verified)', async () => {
    stub(403)
    const { confirmEntity } = await client()
    expect(await confirmEntity('', 'G')).toEqual({ ok: false, reason: 'http_403' })
  })

  it('fails CLOSED on throw', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('down') }))
    const { confirmEntity } = await client()
    expect(await confirmEntity('', 'G')).toEqual({ ok: false, reason: 'error' })
  })
})
