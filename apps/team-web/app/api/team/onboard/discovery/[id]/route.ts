/**
 * VT-507 — proxy GET for the progressive entity-discovery status poll.
 *
 * The browser GETs /{id}; this route forwards to the orchestrator's
 * /api/orchestrator/onboard/discovery/{id} (which just reads pre-computed state — fast, no
 * 90s wait). Returns overall_status / candidates / both_complete_zero verbatim.
 *
 * Fail-CLOSED: any orchestrator error → HTTP 200 with overalls_status: 'error' and empty
 * candidates so the browser's retry logic kicks in (N retries then degrade to manual path).
 * The browser NEVER receives a hard 500 from this route — a transient outage must not
 * surface as "couldn't find" to the owner.
 * CL-390: never log the id / discovery body (business identity).
 *
 * Uses Next.js 16 async params pattern (params is a Promise).
 */
import { type NextRequest, NextResponse } from 'next/server'

import { getDiscoveryStatus } from '@/lib/orchestrator-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

export async function GET(
  _req: NextRequest,
  ctx: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await ctx.params
  if (!id || !id.trim()) {
    return NextResponse.json(
      { overall_status: 'error', candidates: [], both_complete_zero: false },
      { status: 400 },
    )
  }

  const result = await getDiscoveryStatus(id)
  // Always 200 — the browser interprets ok:false as a transient and retries, never "not found".
  return NextResponse.json({
    overall_status: result.overallStatus,
    candidates: result.candidates,
    both_complete_zero: result.bothCompleteZero,
  })
}
