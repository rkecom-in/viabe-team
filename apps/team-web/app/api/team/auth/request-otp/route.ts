/**
 * VT-250 — owner-portal request-OTP route.
 *
 * Flow:
 *   1. Parse + normalize the entered phone to E.164 (D1 anchor format).
 *   2. Rate-limit: per-IP AND per-phone cap (Cowork D4) — both must pass.
 *   3. Call the orchestrator verify-start (Twilio Verify; whatsapp live,
 *      sms gated OFF). The orchestrator owns the Verify Service SID + creds.
 *   4. Return a generic { sent: true } envelope. CRITICAL: the response is
 *      identical whether or not the phone maps to a known tenant — we do NOT
 *      leak tenant existence here (enumeration guard). Tenant resolution
 *      happens at verify-otp, after a code check passes.
 *
 * CL-390: never log the phone or any code. The route logs only the outcome
 * reason + (on the orchestrator side) verification_sid.
 *
 * Channel: whatsapp is the live channel. The route requests whatsapp; an
 * sms request is only honored if the orchestrator gate env is set (D2).
 */

import { NextResponse } from 'next/server'

import { checkOtpRateLimit } from '@/lib/auth/otp-rate-limit'
import { normalizeOwnerPhone } from '@/lib/auth/owner-phone'
import { startOwnerVerification } from '@/lib/owner-verify-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

const LIVE_CHANNEL = 'whatsapp'

interface RequestOtpBody {
  phone?: unknown
  channel?: unknown
}

function clientIp(req: Request): string {
  return (
    req.headers.get('x-forwarded-for')?.split(',')[0]?.trim() ??
    req.headers.get('x-real-ip')?.trim() ??
    'unknown'
  )
}

export async function POST(req: Request): Promise<NextResponse> {
  let body: RequestOtpBody
  try {
    body = (await req.json()) as RequestOtpBody
  } catch {
    return NextResponse.json({ error: 'invalid JSON' }, { status: 400 })
  }

  const rawPhone = typeof body.phone === 'string' ? body.phone : ''
  const channel =
    typeof body.channel === 'string' && body.channel ? body.channel : LIVE_CHANNEL

  const phoneE164 = normalizeOwnerPhone(rawPhone)
  if (!phoneE164) {
    return NextResponse.json(
      { error: 'enter a valid mobile number' },
      { status: 400 },
    )
  }

  // Cowork D4: per-IP AND per-phone cap. Both must pass.
  const rl = checkOtpRateLimit(clientIp(req), phoneE164)
  if (!rl.allowed) {
    // PII-safe: log only the dimension that tripped.
    console.warn(`[request-otp] rate limited (blockedBy=${rl.blockedBy})`)
    return NextResponse.json(
      { error: 'too many requests — try again later' },
      { status: 429 },
    )
  }

  const result = await startOwnerVerification(phoneE164, channel, null)
  if (!result.ok) {
    // Channel gated / orchestrator error — surface a generic failure, no PII.
    console.warn(`[request-otp] verify-start failed (reason=${result.reason})`)
    if (result.reason === 'http_400') {
      // e.g. sms channel gated OFF.
      return NextResponse.json(
        { error: 'this channel is unavailable' },
        { status: 400 },
      )
    }
    return NextResponse.json(
      { error: 'could not send a code right now — try again' },
      { status: 502 },
    )
  }

  // Generic success — do NOT leak whether the phone maps to a tenant.
  return NextResponse.json({ sent: true })
}
