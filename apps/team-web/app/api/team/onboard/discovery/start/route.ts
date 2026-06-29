/**
 * VT-507 — proxy POST for the progressive entity-discovery start.
 *
 * The browser POSTs {business_name, city}; this route forwards to the orchestrator's
 * internal-secret-gated /api/orchestrator/onboard/discovery/start. The orchestrator
 * launches the parallel async search (LLM + KnowYourGST) and returns a discovery_id
 * immediately — it never blocks waiting for results.
 *
 * Fail-CLOSED: any orchestrator error → {discovery_id: null} with HTTP 200 so the browser
 * can fall back to the manual GST-entry path. Signup is never blocked by a failed start.
 * CL-390: never log the business_name/city/discovery_id (business identity).
 *
 * Mirrors the entity-candidates proxy shape; INTERNAL_API_SECRET never reaches the browser.
 */
import { NextResponse } from 'next/server'

import { startEntityDiscovery } from '@/lib/orchestrator-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

export async function POST(request: Request): Promise<Response> {
  const body = (await request.json().catch(() => null)) as Record<string, unknown> | null
  const businessName = body && typeof body.business_name === 'string' ? body.business_name : ''
  const city = body && typeof body.city === 'string' ? body.city : ''
  if (!businessName.trim()) {
    return NextResponse.json({ discovery_id: null }, { status: 400 })
  }

  // Fail-closed: on any orchestrator failure return discovery_id: null so the browser degrades
  // to the old blocking fetchCandidates path — signup is never blocked.
  const result = await startEntityDiscovery(businessName, city)
  return NextResponse.json({ discovery_id: result.discoveryId })
}
