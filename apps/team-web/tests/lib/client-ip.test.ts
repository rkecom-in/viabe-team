/**
 * VT-394 — trustedClientIp header-precedence + spoof-resistance.
 *
 * The per-IP rate-limit key MUST come from the platform-trusted header, never
 * the client-controllable leftmost x-forwarded-for.
 */

import { describe, expect, it } from 'vitest'

import { trustedClientIp } from '@/lib/auth/client-ip'

function reqWith(headers: Record<string, string>): Request {
  return new Request('http://test/x', { method: 'POST', headers })
}

describe('VT-394 trustedClientIp', () => {
  it('prefers x-vercel-forwarded-for over everything else', () => {
    const ip = trustedClientIp(
      reqWith({
        'x-vercel-forwarded-for': '203.0.113.9',
        'x-real-ip': '198.51.100.1',
        'x-forwarded-for': '66.66.66.66, 203.0.113.9',
      }),
    )
    expect(ip).toBe('203.0.113.9')
  })

  it('falls back to x-real-ip when no vercel header', () => {
    const ip = trustedClientIp(
      reqWith({
        'x-real-ip': '198.51.100.1',
        'x-forwarded-for': '66.66.66.66, 198.51.100.1',
      }),
    )
    expect(ip).toBe('198.51.100.1')
  })

  it('NEVER returns the spoofable leftmost XFF — uses the rightmost platform hop', () => {
    // Attacker prepends a fake IP; the trusted hop is the rightmost (appended).
    const ip = trustedClientIp(reqWith({ 'x-forwarded-for': '1.2.3.4, 203.0.113.9' }))
    expect(ip).toBe('203.0.113.9')
    expect(ip).not.toBe('1.2.3.4')
  })

  it('returns "unknown" when no IP header is present', () => {
    expect(trustedClientIp(reqWith({}))).toBe('unknown')
  })
})
