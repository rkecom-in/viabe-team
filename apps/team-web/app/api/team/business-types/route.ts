/** VT-96 — proxy GET for the signup business_type taxonomy (orchestrator config). */
import { NextResponse } from 'next/server'

const BASE = process.env.TEAM_ORCHESTRATOR_URL ?? 'http://localhost:8001'

export async function GET(): Promise<Response> {
  try {
    const res = await fetch(`${BASE}/api/signup/business-types`, { cache: 'no-store' })
    const body = await res.json()
    return NextResponse.json(body, { status: res.status })
  } catch {
    return NextResponse.json({ business_types: [] }, { status: 502 })
  }
}
