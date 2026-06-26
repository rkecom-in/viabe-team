/**
 * VT-411 — proxy POST for the signup ownership DIN-verify (the alternative to the public-number OTP).
 *
 * The owner asserts their Director Identification Number against the company's CIN; the browser POSTs
 * {tenant_id, din, cin, reason}; this route forwards to the orchestrator's internal-secret-gated
 * /api/orchestrator/onboard/ownership/din via the server-side orchestrator-client (INTERNAL_API_SECRET
 * never reaches the browser). The orchestrator checks the DIN against the registry and flips
 * owner_channel_verified; team-web NEVER calls the registry directly.
 *
 * Fail-CLOSED: a registry/transport failure → {owner_channel_verified:false} — NEVER a faked proven
 * owner. owner_channel_verified is the sole signal that ownership is proven. CL-390: never log the
 * din / cin / reason / tenant_id / any body.
 */
import { NextResponse } from 'next/server'

import { verifyOwnerViaDin } from '@/lib/orchestrator-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

export async function POST(request: Request): Promise<Response> {
  const body = (await request.json().catch(() => null)) as Record<string, unknown> | null
  const tenantId = body && typeof body.tenant_id === 'string' ? body.tenant_id : ''
  const din = body && typeof body.din === 'string' ? body.din : ''
  const cin = body && typeof body.cin === 'string' ? body.cin : ''
  const reason = body && typeof body.reason === 'string' ? body.reason : ''
  if (!din.trim()) {
    return NextResponse.json(
      { ok: false, owner_channel_verified: false, reason: 'invalid_request' },
      { status: 400 },
    )
  }

  const result = await verifyOwnerViaDin(tenantId, din, cin, reason)
  return NextResponse.json({
    ok: result.ok,
    owner_channel_verified: result.ownerChannelVerified,
    reason: result.reason,
  })
}
