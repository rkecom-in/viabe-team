/**
 * VT-250 — owner-portal verify-OTP route.
 *
 * Flow:
 *   1. Normalize the entered phone to E.164 (must match the request-otp form).
 *   2. Call the orchestrator verify-check (Twilio Verify). approved? continue.
 *   3. Resolve owner_phone → tenant (D1 anchor; globally-unique index). Exactly
 *      one tenant or login fails closed.
 *   4. Mint the tenant-scoped owner session cookie (viabe_team_session,
 *      HttpOnly + Secure + SameSite=Strict, TTL ≤ 24h — D3).
 *
 * Fails closed at every step: a denied code, an unresolvable phone, or a
 * verify error all return 401 without minting a session. The "approved but no
 * tenant" case is a 401 too (a verified phone with no tenant is not a valid
 * owner) — and it does NOT distinguish itself from a denied code in the
 * client-facing message (no enumeration leak).
 *
 * CL-390: never log the phone or the code. Logs carry only the outcome.
 */

import { NextResponse } from 'next/server'

import { issueOwnerSession } from '@/lib/auth/issue-owner-session'
import { normalizeOwnerPhone } from '@/lib/auth/owner-phone'
import { resolveOwnerTenant } from '@/lib/auth/resolve-owner-tenant'
import { checkOwnerVerification } from '@/lib/owner-verify-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

interface VerifyOtpBody {
  phone?: unknown
  code?: unknown
}

const GENERIC_FAIL = { error: 'invalid or expired code' }

export async function POST(req: Request): Promise<NextResponse> {
  let body: VerifyOtpBody
  try {
    body = (await req.json()) as VerifyOtpBody
  } catch {
    return NextResponse.json({ error: 'invalid JSON' }, { status: 400 })
  }

  const rawPhone = typeof body.phone === 'string' ? body.phone : ''
  const code = typeof body.code === 'string' ? body.code.trim() : ''
  if (!code) {
    return NextResponse.json({ error: 'enter the code' }, { status: 400 })
  }

  const phoneE164 = normalizeOwnerPhone(rawPhone)
  if (!phoneE164) {
    return NextResponse.json(
      { error: 'enter a valid mobile number' },
      { status: 400 },
    )
  }

  const check = await checkOwnerVerification(phoneE164, code, null)
  if (!check.ok) {
    console.warn(`[verify-otp] verify-check failed (reason=${check.reason})`)
    return NextResponse.json(
      { error: 'could not verify the code right now — try again' },
      { status: 502 },
    )
  }
  if (!check.approved) {
    // Denied / expired / max-attempts — generic message, no PII, no leak.
    console.warn(`[verify-otp] code not approved (status=${check.status})`)
    return NextResponse.json(GENERIC_FAIL, { status: 401 })
  }

  // Code approved → resolve the owner's tenant (D1 globally-unique anchor).
  const tenantId = await resolveOwnerTenant(phoneE164)
  if (!tenantId) {
    // Verified phone but no tenant owns it. Fail closed; do NOT distinguish
    // from a denied code (no tenant-existence leak).
    console.warn('[verify-otp] approved code but no tenant for owner_phone')
    return NextResponse.json(GENERIC_FAIL, { status: 401 })
  }

  // Mint the tenant-scoped owner session (D3: stateless JWT, ≤24h cookie).
  const res = NextResponse.json({ ok: true, redirect: '/team/dashboard' })
  return await issueOwnerSession(tenantId, res)
}
