/**
 * VT-411 — proxy POST for the signup ownership-OTP CONFIRM.
 *
 * The owner enters the code that landed on the DISCOVERED PUBLIC business number; the browser POSTs
 * {tenant_id, public_phone, code}; this route forwards to the orchestrator's internal-secret-gated
 * /api/orchestrator/onboard/ownership/otp/confirm via the server-side orchestrator-client
 * (INTERNAL_API_SECRET never reaches the browser). The orchestrator verifies the code against the OTP
 * vendor and flips owner_channel_verified; team-web NEVER calls the vendor directly.
 *
 * Fail-CLOSED: a vendor/transport failure → {owner_channel_verified:false} — NEVER a faked proven owner.
 * owner_channel_verified is the sole signal that ownership is proven. CL-390: never log the
 * public_phone / code / tenant_id / any body.
 */
import { NextResponse } from 'next/server'

import { confirmOwnershipOtp } from '@/lib/orchestrator-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

export async function POST(request: Request): Promise<Response> {
  const body = (await request.json().catch(() => null)) as Record<string, unknown> | null
  const tenantId = body && typeof body.tenant_id === 'string' ? body.tenant_id : ''
  const publicPhone = body && typeof body.public_phone === 'string' ? body.public_phone : ''
  const code = body && typeof body.code === 'string' ? body.code : ''
  if (!publicPhone.trim() || !code.trim()) {
    return NextResponse.json(
      { ok: false, owner_channel_verified: false, reason: 'invalid_request' },
      { status: 400 },
    )
  }

  const result = await confirmOwnershipOtp(tenantId, publicPhone, code)
  return NextResponse.json({
    ok: result.ok,
    owner_channel_verified: result.ownerChannelVerified,
    reason: result.reason,
  })
}
