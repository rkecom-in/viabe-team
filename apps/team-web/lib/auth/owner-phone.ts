/**
 * VT-250 — owner-phone E.164 normalization for the OTP login anchor.
 *
 * The owner_phone column (migration 050) is a GLOBALLY-unique E.164 anchor:
 * one phone → one tenant. The lookup at login MUST normalize identically to
 * the write at onboarding (Cowork D1 invariant). This helper is that single
 * normalization point for the login surface.
 *
 * Launch persona = Indian SMB owners, so the default country is India (+91).
 * The team-web app has no libphonenumber dependency; this is a conservative
 * normalizer tuned for the launch surface, NOT a general international parser:
 *   - already-E.164 (`+<digits>`) → kept (digits only), validated length
 *   - 10-digit local Indian mobile → prefixed `+91`
 *   - `0`-prefixed 11-digit Indian (e.g. 0XXXXXXXXXX) → `+91` + last 10
 *   - `91`-prefixed 12-digit (no `+`) → `+91XXXXXXXXXX`
 * Anything that doesn't resolve to a plausible E.164 (8–15 digits) → null,
 * which the route turns into a 400 (never a Twilio call with garbage).
 */

const _DIGITS = /\d/g

function digitsOnly(s: string): string {
  return (s.match(_DIGITS) ?? []).join('')
}

/**
 * Normalize a raw owner-entered phone to E.164, defaulting to India (+91).
 * Returns null when the input cannot be normalized to a plausible E.164.
 */
export function normalizeOwnerPhone(raw: string | null | undefined): string | null {
  if (!raw) return null
  const trimmed = raw.trim()

  // Explicit international form: keep, strip non-digits after the leading +.
  if (trimmed.startsWith('+')) {
    const d = digitsOnly(trimmed)
    return isPlausibleE164Digits(d) ? `+${d}` : null
  }

  const d = digitsOnly(trimmed)
  if (!d) return null

  // Bare 10-digit local Indian mobile → +91XXXXXXXXXX.
  if (d.length === 10) {
    return `+91${d}`
  }
  // 0-prefixed 11-digit Indian local (trunk 0) → strip the 0, prefix +91.
  if (d.length === 11 && d.startsWith('0')) {
    return `+91${d.slice(1)}`
  }
  // 91-prefixed 12-digit (country code, no +) → +91XXXXXXXXXX.
  if (d.length === 12 && d.startsWith('91')) {
    return `+${d}`
  }
  // Any other already-country-coded run (e.g. pasted without +) → accept if
  // plausible E.164 length.
  if (isPlausibleE164Digits(d)) {
    return `+${d}`
  }
  return null
}

/** E.164 allows up to 15 digits; require at least 8 to reject junk. */
function isPlausibleE164Digits(d: string): boolean {
  return d.length >= 8 && d.length <= 15
}
