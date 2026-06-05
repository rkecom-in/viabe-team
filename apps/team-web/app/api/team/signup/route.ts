/** VT-96 — proxy POST for owner signup → orchestrator /api/signup (VT-82).
 *
 * NEEDS-FAZAL / public-exposure: gated on VT-326 (OTP-before-create + per-IP
 * throttle). Do NOT link this publicly until VT-326 lands.
 */
import { NextResponse } from 'next/server'

const BASE = process.env.TEAM_ORCHESTRATOR_URL ?? 'http://localhost:8001'

export async function POST(request: Request): Promise<Response> {
  // VT-96/VT-326: inert-by-construction. The deployed proxy 404s everywhere until
  // ENABLE_PUBLIC_SIGNUP=true is explicitly set — and flipping it on is part of
  // VT-326's acceptance (OTP-before-create + per-IP throttle must land first). A
  // comment is not a gate; this is.
  if (process.env.ENABLE_PUBLIC_SIGNUP !== 'true') {
    return NextResponse.json({ detail: { code: 'not_enabled' } }, { status: 404 })
  }
  const body = await request.json().catch(() => null)
  if (!body) {
    return NextResponse.json({ detail: { code: 'invalid' } }, { status: 400 })
  }
  try {
    const res = await fetch(`${BASE}/api/signup`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
    const data = await res.json().catch(() => ({}))
    return NextResponse.json(data, { status: res.status })
  } catch {
    return NextResponse.json({ detail: { code: 'upstream' } }, { status: 502 })
  }
}
