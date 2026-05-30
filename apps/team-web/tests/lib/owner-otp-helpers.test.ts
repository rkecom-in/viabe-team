/**
 * VT-250 — owner-OTP support helpers: phone normalization + rate limit +
 * tenant resolution.
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { normalizeOwnerPhone } from '@/lib/auth/owner-phone'
import {
  checkOtpRateLimit,
  OTP_MAX_PER_IP,
  OTP_MAX_PER_PHONE,
  _resetOtpRateLimit,
} from '@/lib/auth/otp-rate-limit'
import { resolveOwnerTenant } from '@/lib/auth/resolve-owner-tenant'

describe('VT-250 — normalizeOwnerPhone', () => {
  it('keeps a valid +E.164 number', () => {
    expect(normalizeOwnerPhone('+919876543210')).toBe('+919876543210')
  })
  it('prefixes +91 onto a bare 10-digit Indian mobile', () => {
    expect(normalizeOwnerPhone('9876543210')).toBe('+919876543210')
  })
  it('strips a leading trunk 0 and prefixes +91', () => {
    expect(normalizeOwnerPhone('09876543210')).toBe('+919876543210')
  })
  it('handles a 91-prefixed 12-digit number without +', () => {
    expect(normalizeOwnerPhone('919876543210')).toBe('+919876543210')
  })
  it('tolerates spaces / dashes', () => {
    expect(normalizeOwnerPhone('+91 98765-43210')).toBe('+919876543210')
  })
  it('rejects junk / too-short', () => {
    expect(normalizeOwnerPhone('12345')).toBeNull()
    expect(normalizeOwnerPhone('')).toBeNull()
    expect(normalizeOwnerPhone(null)).toBeNull()
    expect(normalizeOwnerPhone('abc')).toBeNull()
  })
})

describe('VT-250 — checkOtpRateLimit (D4: per-IP AND per-phone)', () => {
  beforeEach(() => _resetOtpRateLimit())
  afterEach(() => _resetOtpRateLimit())

  it('allows up to the per-IP cap, then blocks by ip', () => {
    const phoneBase = '+9198000000'
    let last = checkOtpRateLimit('1.1.1.1', `${phoneBase}01`)
    // Use distinct phones so the per-phone cap is not what trips first.
    for (let i = 2; i <= OTP_MAX_PER_IP; i++) {
      last = checkOtpRateLimit('1.1.1.1', `${phoneBase}${String(i).padStart(2, '0')}`)
      expect(last.allowed).toBe(true)
    }
    // One more from the same IP (different phone) → blocked by ip.
    last = checkOtpRateLimit('1.1.1.1', `${phoneBase}99`)
    expect(last.allowed).toBe(false)
    expect(last.blockedBy).toBe('ip')
  })

  it('blocks by phone when the same number is hammered from many IPs', () => {
    const phone = '+919812312312'
    for (let i = 1; i <= OTP_MAX_PER_PHONE; i++) {
      const r = checkOtpRateLimit(`10.0.0.${i}`, phone)
      expect(r.allowed).toBe(true)
    }
    const blocked = checkOtpRateLimit('10.0.0.250', phone)
    expect(blocked.allowed).toBe(false)
    expect(blocked.blockedBy).toBe('phone')
  })

  it('a blocked-by-ip request does NOT consume the per-phone budget', () => {
    const phone = '+919800009000'
    // Exhaust the IP cap with distinct phones.
    for (let i = 1; i <= OTP_MAX_PER_IP; i++) {
      checkOtpRateLimit('2.2.2.2', `+91980000${String(1000 + i)}`)
    }
    // Now this IP is at cap → blocked by ip BEFORE the per-phone counter moves.
    const blocked = checkOtpRateLimit('2.2.2.2', phone)
    expect(blocked.blockedBy).toBe('ip')
    // The phone (from a fresh IP) still has its full budget.
    const fresh = checkOtpRateLimit('3.3.3.3', phone)
    expect(fresh.allowed).toBe(true)
  })
})

describe('VT-250 — resolveOwnerTenant', () => {
  function fakeClient(row: { id: string } | null, err = false) {
    return {
      from() {
        return {
          select() {
            return {
              eq() {
                return {
                  maybeSingle: async () => ({
                    data: row,
                    error: err ? { message: 'boom' } : null,
                  }),
                }
              },
            }
          },
        }
      },
    }
  }

  it('returns the tenant id on a match', async () => {
    const id = await resolveOwnerTenant('+919876543210', fakeClient({ id: 'tnt-1' }))
    expect(id).toBe('tnt-1')
  })
  it('returns null on no match (fails closed)', async () => {
    const id = await resolveOwnerTenant('+919876543210', fakeClient(null))
    expect(id).toBeNull()
  })
  it('returns null on db error (fails closed)', async () => {
    const id = await resolveOwnerTenant('+919876543210', fakeClient(null, true))
    expect(id).toBeNull()
  })
})
