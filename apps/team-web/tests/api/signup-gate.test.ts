/** VT-326 — /api/signup OTP-before-create gate + per-IP throttle + orchestrator secret. */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { POST as signupPOST } from '@/app/api/team/signup/route'
import { _resetOtpRateLimit } from '@/lib/auth/otp-rate-limit'
import { normalizeOwnerPhone } from '@/lib/auth/owner-phone'

const RAW = '+919811111111'
const ORIG = { ...process.env }

beforeEach(() => {
  _resetOtpRateLimit()
  process.env.ENABLE_PUBLIC_SIGNUP = 'true'
  process.env.OWNER_JWT_SECRET = 'vt326-test-' + 's'.repeat(40)
  process.env.INTERNAL_API_SECRET = 'sek'
  process.env.TEAM_ORCHESTRATOR_URL = 'http://orch:8001'
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  process.env = { ...ORIG }
})

function req(body: unknown, headers: Record<string, string> = {}): Request {
  return new Request('http://test/api/team/signup', {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify(body),
  })
}

async function proofToken(phoneE164: string): Promise<string> {
  const { issueVerifiedNumberToken } = await import('@/lib/auth/verified-number-token')
  return issueVerifiedNumberToken(phoneE164)
}

describe('VT-326 signup gate', () => {
  it('404 when ENABLE_PUBLIC_SIGNUP is not true (stays dark)', async () => {
    process.env.ENABLE_PUBLIC_SIGNUP = ''
    const res = await signupPOST(req({ whatsapp_number: RAW }))
    expect(res.status).toBe(404)
  })

  it('401 otp_required when no proof token', async () => {
    const res = await signupPOST(req({ whatsapp_number: RAW }))
    expect(res.status).toBe(401)
    expect((await res.json()).detail.code).toBe('otp_required')
  })

  it('401 invalid_proof on a garbage token', async () => {
    const res = await signupPOST(req({ whatsapp_number: RAW }, { authorization: 'Bearer nope.nope.nope' }))
    expect(res.status).toBe(401)
    expect((await res.json()).detail.code).toBe('invalid_proof')
  })

  it('401 when an OWNER-SESSION token is presented (audience crossover blocked)', async () => {
    const { issueOwnerJwt } = await import('@/lib/auth/owner-jwt')
    const ownerTok = await issueOwnerJwt('tenant-1')
    const res = await signupPOST(req({ whatsapp_number: RAW }, { authorization: `Bearer ${ownerTok}` }))
    expect(res.status).toBe(401) // invalid_proof — wrong audience
  })

  it('401 phone_mismatch when the proof is for a different number', async () => {
    const tok = await proofToken(normalizeOwnerPhone('+919800000000') as string)
    const res = await signupPOST(req({ whatsapp_number: RAW }, { authorization: `Bearer ${tok}` }))
    expect(res.status).toBe(401)
    expect((await res.json()).detail.code).toBe('phone_mismatch')
  })

  it('forwards to the orchestrator with X-Internal-Secret on a valid proof', async () => {
    const f = vi.fn(async () => ({ ok: true, status: 201, json: async () => ({ tenant_id: 't1' }) }))
    vi.stubGlobal('fetch', f)
    const tok = await proofToken(normalizeOwnerPhone(RAW) as string)
    const res = await signupPOST(req({ whatsapp_number: RAW }, { authorization: `Bearer ${tok}`, 'x-forwarded-for': '1.1.1.1' }))
    expect(res.status).toBe(201)
    const [url, opts] = f.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toMatch(/\/api\/signup$/)
    expect((opts.headers as Record<string, string>)['X-Internal-Secret']).toBe('sek')
  })

  it('forwards the CANONICAL normalized phone, not the raw body spelling', async () => {
    const f = vi.fn(async () => ({ ok: true, status: 201, json: async () => ({}) }))
    vi.stubGlobal('fetch', f)
    const canonical = normalizeOwnerPhone(RAW) as string
    const tok = await proofToken(canonical)
    // A NON-canonical spelling that normalizes to the same number (passes the equality check).
    const altSpelling = '9811111111'
    expect(normalizeOwnerPhone(altSpelling)).toBe(canonical) // precondition: same number
    const res = await signupPOST(
      req({ whatsapp_number: altSpelling }, { authorization: `Bearer ${tok}`, 'x-forwarded-for': '3.3.3.3' }),
    )
    expect(res.status).toBe(201)
    const [, opts] = f.mock.calls[0] as unknown as [string, RequestInit]
    const forwarded = JSON.parse(opts.body as string) as { whatsapp_number: string }
    expect(forwarded.whatsapp_number).toBe(canonical) // canonical, NOT the raw spelling
    expect(forwarded.whatsapp_number).not.toBe(altSpelling)
  })

  it('429 on the (N+1)th signup from one IP (throttle backstop)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 201, json: async () => ({}) })))
    const tok = await proofToken(normalizeOwnerPhone(RAW) as string)
    const ip = '2.2.2.2'
    let last = 0
    for (let i = 0; i < 6; i++) {
      const res = await signupPOST(req({ whatsapp_number: RAW }, { authorization: `Bearer ${tok}`, 'x-forwarded-for': ip }))
      last = res.status
    }
    expect(last).toBe(429) // 6th request from the same IP, 5/15 cap
  })
})
