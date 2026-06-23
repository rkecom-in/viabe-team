/**
 * VT-406 (Part B) — proxy POST for the signup entity-match candidate lookup.
 *
 * The browser POSTs {business_name, city}; this route forwards to the orchestrator's
 * internal-secret-gated /api/orchestrator/onboard/entity-candidates via the server-side
 * orchestrator-client (INTERNAL_API_SECRET never reaches the browser). The orchestrator does
 * the web-search + GBP lookup; team-web NEVER calls those vendors directly.
 *
 * Fail-CLOSED: the proxy fn returns {candidates: []} on any orchestrator error — candidate lookup
 * must never stall or block signup (the not-listed path always exists). CL-390: never log the
 * business_name / city / any candidate (business identity).
 */
import { NextResponse } from 'next/server'

import { fetchEntityCandidates } from '@/lib/orchestrator-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

export async function POST(request: Request): Promise<Response> {
  const body = (await request.json().catch(() => null)) as Record<string, unknown> | null
  const businessName = body && typeof body.business_name === 'string' ? body.business_name : ''
  const city = body && typeof body.city === 'string' ? body.city : ''
  if (!businessName.trim()) {
    return NextResponse.json({ candidates: [] }, { status: 400 })
  }

  // Fail-closed by construction: fetchEntityCandidates returns {ok:false, candidates:[]} on any
  // orchestrator failure — we always 200 with whatever candidates we have (never stall signup).
  const result = await fetchEntityCandidates(businessName, city)
  return NextResponse.json({ candidates: result.candidates })
}
