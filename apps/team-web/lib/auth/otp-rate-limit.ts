/**
 * VT-250 — request-OTP rate limiter (Cowork ruling D4, BINDING).
 *
 * D4 requires BOTH a per-IP AND a per-phone cap on request-OTP. This is a
 * thin in-process fixed-window limiter layered ON TOP of Twilio Verify's own
 * native per-number rate limiting + brute-force protection — its job is to
 * blunt enumeration / abuse before a Verify call is even made (and before
 * Twilio costs are incurred).
 *
 * Fixed-window counters keyed by (kind, identifier, window). Default: 5
 * requests per 15-minute window for EACH of per-IP and per-phone. Both must
 * pass; the first to trip blocks the request.
 *
 * Scope note: in-process Map state — per serverless instance, not global.
 * For Phase-1 launch scale this is the cheap thin cap D4 asks for; a shared
 * store (rate_limit_buckets / Redis) is a deliberate follow-up, not in this
 * row's scope. The per-phone key is a SHA-256 token (NEVER the plaintext
 * phone — CL-390) so the in-memory map holds no PII.
 */

import { createHash } from 'crypto'

export const OTP_WINDOW_MS = 15 * 60 * 1000 // 15 minutes
export const OTP_MAX_PER_IP = 5
export const OTP_MAX_PER_PHONE = 5

type Bucket = { count: number; windowStart: number }

// key = `${kind}:${identifier}:${windowStart}` → count
const _buckets = new Map<string, Bucket>()

/** Test seam: reset all in-process counters. */
export function _resetOtpRateLimit(): void {
  _buckets.clear()
}

function _tokenizePhone(phoneE164: string): string {
  // Never key the map on the plaintext phone (CL-390).
  return createHash('sha256').update(phoneE164, 'utf8').digest('hex').slice(0, 16)
}

function _hit(
  kind: 'ip' | 'phone' | 'signup' | 'waitlist',
  identifier: string,
  max: number,
  now: number,
): boolean {
  const windowStart = Math.floor(now / OTP_WINDOW_MS) * OTP_WINDOW_MS
  const key = `${kind}:${identifier}:${windowStart}`
  const bucket = _buckets.get(key)
  if (!bucket || bucket.windowStart !== windowStart) {
    _buckets.set(key, { count: 1, windowStart })
    return true
  }
  if (bucket.count >= max) {
    return false
  }
  bucket.count += 1
  return true
}

export interface RateLimitResult {
  allowed: boolean
  /** Which cap tripped, when blocked. PII-safe ('ip' | 'phone'). */
  blockedBy: 'ip' | 'phone' | null
}

/**
 * Consume one request-OTP token for BOTH the per-IP and per-phone caps.
 * Returns allowed=false (with blockedBy) if EITHER cap is exceeded.
 *
 * Both caps are consumed only when both would pass, so a blocked request
 * does not asymmetrically burn the other dimension's budget: the per-IP cap
 * is checked first; if it trips, the per-phone counter is left untouched.
 */
export function checkOtpRateLimit(
  ip: string,
  phoneE164: string,
  now: number = Date.now(),
): RateLimitResult {
  // Per-IP first (cheap enumeration guard).
  if (!_hit('ip', ip || 'unknown', OTP_MAX_PER_IP, now)) {
    return { allowed: false, blockedBy: 'ip' }
  }
  // Per-phone (tokenized — no plaintext in the map).
  if (!_hit('phone', _tokenizePhone(phoneE164), OTP_MAX_PER_PHONE, now)) {
    return { allowed: false, blockedBy: 'phone' }
  }
  return { allowed: true, blockedBy: null }
}

/**
 * VT-326 — per-IP throttle for /api/signup. A BACKSTOP, not the primary defense:
 * the OTP proof-of-control gate in front of it is the real anti-flood (an attacker
 * must control a real WhatsApp number per attempt, which doesn't scale). Per-IP is
 * unreliable in India anyway (CGNAT / shared carrier IPs mean many legitimate owners
 * share one IP), so we keep the standard 5/15 and do NOT tighten (Cowork A3).
 *
 * Uses a DISTINCT bucket kind ('signup') so it has its own window — a signup attempt
 * does not consume the request-otp per-IP budget, and vice versa.
 */
export function checkSignupRateLimit(
  ip: string,
  now: number = Date.now(),
): RateLimitResult {
  if (!_hit('signup', ip || 'unknown', OTP_MAX_PER_IP, now)) {
    return { allowed: false, blockedBy: 'ip' }
  }
  return { allowed: true, blockedBy: null }
}

/**
 * VT-97 — per-IP cap for waitlist capture. A DISTINCT 'waitlist' bucket so it has its own
 * window (independent of signup/otp). A flood backstop behind the orchestrator dedup + the
 * X-Internal-Secret; intentionally the same lenient 5/15 (CGNAT — see above).
 */
export function checkWaitlistRateLimit(
  ip: string,
  now: number = Date.now(),
): RateLimitResult {
  if (!_hit('waitlist', ip || 'unknown', OTP_MAX_PER_IP, now)) {
    return { allowed: false, blockedBy: 'ip' }
  }
  return { allowed: true, blockedBy: null }
}
