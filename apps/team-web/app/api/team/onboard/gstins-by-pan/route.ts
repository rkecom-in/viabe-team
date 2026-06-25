/**
 * VT-448 — proxy POST for the signup PAN→GSTIN identify step.
 *
 * The browser POSTs {pan, state_code} (the owner's 10-char PAN + the state code derived from their
 * city); this route forwards to the orchestrator's internal-secret-gated
 * /api/orchestrator/onboard/gstins-by-pan via the server-side orchestrator-client
 * (INTERNAL_API_SECRET never reaches the browser). The orchestrator does the vendor round-trip;
 * team-web NEVER calls the vendor directly.
 *
 * This step IDENTIFIES the GSTIN(s) registered against the PAN — it does NOT verify. The owner still
 * PICKS one and the existing entity-confirm Sandbox round-trip (status gstin_verified) is the sole
 * verify gate (VT-408 verify-then-create ordering).
 *
 * Fail-CLOSED: a vendor/transport failure → {ok:false, gstins:[]} — the manual-GSTIN fallback always
 * exists, so a lookup failure never stalls signup. CL-390: never log the pan / gstins / any body.
 */
import { NextResponse } from 'next/server'

import { fetchGstinsByPan } from '@/lib/orchestrator-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

export async function POST(request: Request): Promise<Response> {
  const body = (await request.json().catch(() => null)) as Record<string, unknown> | null
  const pan = body && typeof body.pan === 'string' ? body.pan : ''
  const stateCode = body && typeof body.state_code === 'string' ? body.state_code : ''
  if (!pan.trim() || !stateCode.trim()) {
    return NextResponse.json({ ok: false, gstins: [], reason: 'invalid_request' }, { status: 400 })
  }

  const result = await fetchGstinsByPan(pan, stateCode)
  return NextResponse.json({ ok: result.ok, gstins: result.gstins, reason: result.reason })
}
