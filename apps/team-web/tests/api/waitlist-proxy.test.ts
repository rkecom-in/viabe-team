/** VT-97 — the waitlist proxy: CL-422 dark gate (404), X-Internal-Secret forward, per-IP cap. */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { POST } from '@/app/api/team/waitlist/route'
import { _resetOtpRateLimit } from '@/lib/auth/otp-rate-limit'

function req(body: unknown, ip = '1.2.3.4'): Request {
  return new Request('http://test/api/team/waitlist', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-forwarded-for': ip },
    body: JSON.stringify(body),
  })
}

const good = { email: 'a@b.com', whatsapp_e164: '+919876543210', consent: true }

beforeEach(() => {
  process.env.INTERNAL_API_SECRET = 'sek'
  _resetOtpRateLimit()
})
afterEach(() => {
  vi.restoreAllMocks()
  delete process.env.ENABLE_WAITLIST_CAPTURE
})

describe('VT-97 waitlist proxy', () => {
  it('404s when ENABLE_WAITLIST_CAPTURE is off (CL-422 dark gate)', async () => {
    delete process.env.ENABLE_WAITLIST_CAPTURE
    const res = await POST(req(good))
    expect(res.status).toBe(404)
  })

  it('forwards to /api/waitlist with X-Internal-Secret when enabled', async () => {
    process.env.ENABLE_WAITLIST_CAPTURE = 'true'
    const f = vi
      .spyOn(global, 'fetch')
      .mockResolvedValue({ status: 200, json: async () => ({ status: 'queued' }) } as Response)
    const res = await POST(req(good, '9.9.9.9'))
    expect(res.status).toBe(200)
    const [url, init] = f.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/waitlist')
    expect((init.headers as Record<string, string>)['X-Internal-Secret']).toBe('sek')
  })

  it('429s on a burst from one IP (flood backstop)', async () => {
    process.env.ENABLE_WAITLIST_CAPTURE = 'true'
    vi.spyOn(global, 'fetch').mockResolvedValue({
      status: 200,
      json: async () => ({}),
    } as Response)
    let last: Response | undefined
    for (let i = 0; i < 7; i++) last = await POST(req({ ...good, email: `a${i}@b.com` }, '5.5.5.5'))
    expect(last?.status).toBe(429)
  })
})
