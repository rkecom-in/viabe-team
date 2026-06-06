/**
 * VT-326 — verify-OTP FOR SIGNUP (pre-tenant).
 *
 * Mirrors verify-otp but, on an APPROVED code, issues a short-lived verified-number
 * PROOF token instead of resolving a tenant + minting a session (signup has no tenant
 * yet). The login verify-otp path is left byte-untouched.
 *
 * Fails closed: a denied code or a verify error returns 401/502 without a token.
 * CL-390: never log the phone or the code.
 */
import { NextResponse } from 'next/server'

import { normalizeOwnerPhone } from '@/lib/auth/owner-phone'
import { issueVerifiedNumberToken } from '@/lib/auth/verified-number-token'
import { checkOwnerVerification } from '@/lib/owner-verify-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

interface Body {
  phone?: unknown
  code?: unknown
}

const GENERIC_FAIL = { error: 'invalid or expired code' }

export async function POST(req: Request): Promise<NextResponse> {
  let body: Body
  try {
    body = (await req.json()) as Body
  } catch {
    return NextResponse.json({ error: 'invalid JSON' }, { status: 400 })
  }

  const code = typeof body.code === 'string' ? body.code.trim() : ''
  if (!code) {
    return NextResponse.json({ error: 'enter the code' }, { status: 400 })
  }
  const phoneE164 = normalizeOwnerPhone(typeof body.phone === 'string' ? body.phone : '')
  if (!phoneE164) {
    return NextResponse.json({ error: 'enter a valid mobile number' }, { status: 400 })
  }

  const check = await checkOwnerVerification(phoneE164, code, null)
  if (!check.ok) {
    console.warn(`[verify-otp-for-signup] verify-check failed (reason=${check.reason})`)
    return NextResponse.json(
      { error: 'could not verify the code right now — try again' },
      { status: 502 },
    )
  }
  if (!check.approved) {
    console.warn(`[verify-otp-for-signup] code not approved (status=${check.status})`)
    return NextResponse.json(GENERIC_FAIL, { status: 401 })
  }

  // Approved → issue the pre-tenant verified-number proof (NO tenant resolution).
  const token = await issueVerifiedNumberToken(phoneE164)
  return NextResponse.json({ ok: true, token })
}
