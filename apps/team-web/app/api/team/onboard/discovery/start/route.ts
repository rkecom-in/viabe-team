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
 * VT-778: the city is resolved SERVER-SIDE from the request IP when the client does not send one.
 * The signup form stopped collecting a city (it is meant to be auto-detected) and hardcodes
 * city="", but nothing was actually detecting it — so every discovery ran India-wide. That is not
 * cosmetic: DataForSEO's AI Mode rung was measured at 0/4 for "Sundaram Book Store" India-wide and
 * 4/4 with Mumbai geo-targeting. The tenant's locale decides whether we find their GSTIN.
 * City is a PREFERENCE, not a filter (Fazal 2026-08-22) — it rides the SERP's geo targeting, never
 * the search keyword.
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
  const clientCity = body && typeof body.city === 'string' ? body.city.trim() : ''
  // An explicit client city wins; otherwise fall back to the edge-provided IP city. Vercel
  // percent-encodes the header (e.g. "New%20Delhi"), and a malformed value must never throw here.
  const city = clientCity || ipCityFromHeaders(request)
  if (!businessName.trim()) {
    return NextResponse.json({ discovery_id: null }, { status: 400 })
  }

  // Fail-closed: on any orchestrator failure return discovery_id: null so the browser degrades
  // to the old blocking fetchCandidates path — signup is never blocked.
  const result = await startEntityDiscovery(businessName, city)
  return NextResponse.json({ discovery_id: result.discoveryId })
}

/**
 * The viewer's city per the CDN edge, or '' when unavailable (local dev, a proxy that strips it,
 * or a request the edge could not geolocate). Never throws: a bad header must degrade discovery to
 * India-wide, not break the signup.
 */
function ipCityFromHeaders(request: Request): string {
  const raw =
    request.headers.get('x-vercel-ip-city') ?? request.headers.get('cf-ipcity') ?? ''
  if (!raw) return ''
  try {
    return decodeURIComponent(raw).trim()
  } catch {
    return raw.trim()
  }
}
