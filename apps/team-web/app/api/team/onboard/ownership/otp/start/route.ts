/**
 * VT-411 — proxy POST for the signup ownership-OTP START.
 *
 * After the entity verifies (gstin_verified), the owner must prove they OWN the business — a DISTINCT
 * OTP to the DISCOVERED PUBLIC business number (NOT the personal WhatsApp the signup OTP already
 * proved). The browser POSTs {tenant_id, public_phone}; this route forwards to the orchestrator's
 * internal-secret-gated /api/orchestrator/onboard/ownership/otp/start via the server-side
 * orchestrator-client (INTERNAL_API_SECRET never reaches the browser). The orchestrator holds the OTP
 * vendor creds + sends; team-web NEVER calls the vendor directly.
 *
 * Fail-CLOSED: a vendor/transport failure → {ok:false} — NEVER a faked dispatch. CL-390: never log the
 * public_phone / tenant_id / any body (business identity).
 */
import { NextResponse } from 'next/server'

import { startOwnershipOtp } from '@/lib/orchestrator-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

export async function POST(request: Request): Promise<Response> {
  const body = (await request.json().catch(() => null)) as Record<string, unknown> | null
  const tenantId = body && typeof body.tenant_id === 'string' ? body.tenant_id : ''
  const publicPhone = body && typeof body.public_phone === 'string' ? body.public_phone : ''
  if (!publicPhone.trim()) {
    return NextResponse.json(
      { ok: false, verification_sid: null, status: 'invalid_request' },
      { status: 400 },
    )
  }

  const result = await startOwnershipOtp(tenantId, publicPhone)
  return NextResponse.json({
    ok: result.ok,
    verification_sid: result.verificationSid,
    status: result.status,
  })
}
