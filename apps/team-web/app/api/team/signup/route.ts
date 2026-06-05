/** VT-96 — proxy POST for owner signup → orchestrator /api/signup (VT-82).
 *
 * NEEDS-FAZAL / public-exposure: gated on VT-326 (OTP-before-create + per-IP
 * throttle). Do NOT link this publicly until VT-326 lands.
 */
import { NextResponse } from 'next/server'

const BASE = process.env.TEAM_ORCHESTRATOR_URL ?? 'http://localhost:8001'

export async function POST(request: Request): Promise<Response> {
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
