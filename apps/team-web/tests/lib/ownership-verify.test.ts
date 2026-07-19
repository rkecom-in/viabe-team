/**
 * VT-517 — the surviving ownership format gates (lib/ownership-verify.ts).
 *
 * The self-serve ownership-OTP/DIN fetch helpers (startOwnershipOtp / confirmOwnershipOtp /
 * verifyOwnerViaDin) and their proxy routes were removed with VT-517 (ownership is now decided by a
 * Viabe human). Only the pure format gates remain — pinned here so the predicates stay correct.
 */

import { describe, expect, it } from 'vitest'

import { isValidDinFormat, isValidPublicPhoneFormat } from '@/lib/ownership-verify'

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
