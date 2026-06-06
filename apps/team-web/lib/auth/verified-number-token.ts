/**
 * VT-326 — pre-tenant "verified number" proof token.
 *
 * Signup is PRE-TENANT (no tenant exists yet), so the tenant-scoped owner session
 * (verify-otp → owner-jwt, aud='owner') can't gate it. After an OTP check on the
 * whatsapp_number, `verify-otp-for-signup` issues this short-lived JWT proving control
 * of the number; `/api/signup` requires + validates it before create.
 *
 * DISTINCT AUDIENCE ('otp-verified-for-signup') from the owner session ('owner'), the
 * operator token, and the VT-332 trial deep-link token: any of those fails audience
 * validation here and CANNOT be replayed as a signup proof. Same secret
 * (OWNER_JWT_SECRET) — the audience guard is the no-crossover line (mirrors owner-jwt).
 * NO tenant_id claim (pre-tenant; nothing to leak). Short TTL (10 min ≈ the OTP window).
 */
import { jwtVerify, SignJWT, type JWTPayload } from 'jose'

export const VERIFIED_NUMBER_AUDIENCE = 'otp-verified-for-signup'
export const VERIFIED_NUMBER_TTL_SEC = 10 * 60 // 10 minutes

interface VerifiedNumberClaim extends JWTPayload {
  phone_e164: string
  number_verified: true
}

function _secretBytes(): Uint8Array {
  // Read at call-time (not module-load) so the secret is resolved when the route runs.
  const secret = process.env.OWNER_JWT_SECRET ?? ''
  if (!secret) {
    throw new Error('verified-number-token: OWNER_JWT_SECRET env must be set on server')
  }
  return new TextEncoder().encode(secret)
}

export async function issueVerifiedNumberToken(
  phoneE164: string,
  opts: { ttlSec?: number } = {},
): Promise<string> {
  const ttl = Math.min(opts.ttlSec ?? VERIFIED_NUMBER_TTL_SEC, VERIFIED_NUMBER_TTL_SEC)
  return await new SignJWT({ phone_e164: phoneE164, number_verified: true })
    .setProtectedHeader({ alg: 'HS256' })
    .setAudience(VERIFIED_NUMBER_AUDIENCE)
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + ttl)
    .sign(_secretBytes())
}

export async function verifyVerifiedNumberToken(
  token: string,
): Promise<{ phoneE164: string }> {
  // Audience guard = the FIRST no-crossover line: an owner-session (aud='owner') or any
  // other-audience token fails here before any claim inspection.
  const { payload } = await jwtVerify(token, _secretBytes(), {
    audience: VERIFIED_NUMBER_AUDIENCE,
  })
  if (payload.number_verified !== true || typeof payload.phone_e164 !== 'string') {
    throw new Error('verified-number-token: claim missing or malformed')
  }
  return { phoneE164: (payload as VerifiedNumberClaim).phone_e164 }
}
