/**
 * VT-394 — team-web forwards the client IP to the orchestrator verify-start
 * proxy so the orchestrator-side (authoritative) per-IP OTP cap enforces on the
 * real caller IP. Mock the fetch, assert the X-Forwarded-For header.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { startOwnerVerification } from '@/lib/owner-verify-client'

describe('VT-394 — startOwnerVerification forwards the client IP', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ status: 'pending', verification_sid: 'VEx' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    vi.stubEnv('INTERNAL_API_SECRET', 'test-secret')
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.unstubAllEnvs()
  })

  function lastCall(): { url: string; headers: Record<string, string> } {
    const calls = fetchMock.mock.calls
    const call = calls[calls.length - 1] as [string, RequestInit] | undefined
    if (!call) throw new Error('fetch was not called')
    const [url, init] = call
    return { url, headers: (init.headers ?? {}) as Record<string, string> }
  }

  it('sets X-Forwarded-For to the passed client IP', async () => {
    const res = await startOwnerVerification('+919876543210', 'whatsapp', null, '203.0.113.9')
    expect(res.ok).toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const { url, headers } = lastCall()
    expect(headers['X-Forwarded-For']).toBe('203.0.113.9')
    // The internal secret + verify-start URL still go out unchanged.
    expect(headers['X-Internal-Secret']).toBe('test-secret')
    expect(url).toContain('/api/orchestrator/owner/verify-start')
  })

  it('omits X-Forwarded-For when no IP is passed (backward-compat)', async () => {
    await startOwnerVerification('+919876543210', 'whatsapp', null)
    const { headers } = lastCall()
    expect(headers['X-Forwarded-For']).toBeUndefined()
  })

  it('does not leak the phone into the forwarded headers', async () => {
    await startOwnerVerification('+919876543210', 'whatsapp', null, '203.0.113.9')
    const { headers } = lastCall()
    const serialized = JSON.stringify(headers)
    expect(serialized).not.toContain('+919876543210')
  })
})
