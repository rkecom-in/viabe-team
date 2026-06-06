/** VT-96 — proxy POST for owner signup → orchestrator /api/signup (VT-82).
 *
 * VT-326 gates it: ENABLE_PUBLIC_SIGNUP-dark + OTP-before-create (a verified-number
 * proof token) + per-IP throttle + an X-Internal-Secret to the orchestrator. Still
 * inert until the flag is flipped (NOT in this row — Fazal go-live + the VT-329 i18n
 * hard-gate own that switch).
 */
import { NextResponse } from 'next/server'

import { checkSignupRateLimit } from '@/lib/auth/otp-rate-limit'
import { normalizeOwnerPhone } from '@/lib/auth/owner-phone'
import { verifyVerifiedNumberToken } from '@/lib/auth/verified-number-token'

const BASE = process.env.TEAM_ORCHESTRATOR_URL ?? 'http://localhost:8001'
const _secret = (): string => process.env.INTERNAL_API_SECRET ?? ''

function clientIp(req: Request): string {
  return (
    req.headers.get('x-forwarded-for')?.split(',')[0]?.trim() ??
    req.headers.get('x-real-ip')?.trim() ??
    'unknown'
  )
}

export async function POST(request: Request): Promise<Response> {
  // VT-96/VT-326: inert-by-construction. The deployed proxy 404s everywhere until
  // ENABLE_PUBLIC_SIGNUP=true is explicitly set. A comment is not a gate; this is.
  if (process.env.ENABLE_PUBLIC_SIGNUP !== 'true') {
    return NextResponse.json({ detail: { code: 'not_enabled' } }, { status: 404 })
  }

  const body = (await request.json().catch(() => null)) as Record<string, unknown> | null
  if (!body || typeof body !== 'object') {
    return NextResponse.json({ detail: { code: 'invalid' } }, { status: 400 })
  }

  // VT-326 OTP-before-create: require the verified-number proof (the owner OTP-proved
  // control of whatsapp_number). Blocks number-squatting + anonymous create.
  const authHeader = request.headers.get('authorization') ?? ''
  const token = authHeader.toLowerCase().startsWith('bearer ') ? authHeader.slice(7).trim() : ''
  if (!token) {
    return NextResponse.json({ detail: { code: 'otp_required' } }, { status: 401 })
  }
  let verified: { phoneE164: string }
  try {
    verified = await verifyVerifiedNumberToken(token)
  } catch {
    return NextResponse.json({ detail: { code: 'invalid_proof' } }, { status: 401 })
  }

  // The proof must be for the SAME number being signed up (no bait-and-switch).
  const rawPhone = typeof body.whatsapp_number === 'string' ? body.whatsapp_number : ''
  const normPhone = normalizeOwnerPhone(rawPhone)
  if (!normPhone || verified.phoneE164 !== normPhone) {
    return NextResponse.json({ detail: { code: 'phone_mismatch' } }, { status: 401 })
  }

  // VT-326 per-IP throttle — a backstop behind the OTP gate (Cowork A3: keep 5/15, the
  // OTP proof-of-control is the primary anti-flood; CGNAT makes a tighter cap harmful).
  if (!checkSignupRateLimit(clientIp(request)).allowed) {
    return NextResponse.json({ detail: { code: 'rate_limited' } }, { status: 429 })
  }

  try {
    // Forward the CANONICAL normalized phone we actually verified (not the raw body) so
    // gated == forwarded == persisted == indexed — a token for phone A can't create a tenant
    // for a non-canonical spelling of A even if the orchestrator regex is ever relaxed.
    const forwardBody = { ...body, whatsapp_number: normPhone }
    const res = await fetch(`${BASE}/api/signup`, {
      method: 'POST',
      // VT-326 A2: X-Internal-Secret so only team-web can reach the orchestrator's
      // BYPASSRLS create — closes flooding at the source, not just the edge.
      headers: { 'content-type': 'application/json', 'X-Internal-Secret': _secret() },
      body: JSON.stringify(forwardBody),
    })
    const data = await res.json().catch(() => ({}))
    return NextResponse.json(data, { status: res.status })
  } catch {
    return NextResponse.json({ detail: { code: 'upstream' } }, { status: 502 })
  }
}
