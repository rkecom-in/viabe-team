/** VT-99 — proxy GET for the public founding-tier counter (orchestrator VT-94 endpoint). */
import { NextResponse } from 'next/server'

const BASE = process.env.TEAM_ORCHESTRATOR_URL ?? 'http://localhost:8001'

export async function GET(): Promise<Response> {
  try {
    const res = await fetch(`${BASE}/api/team/founding-status`, {
      signal: AbortSignal.timeout(5000),
      next: { revalidate: 60 }, // edge-cache 60s (the orchestrator endpoint also caches)
    })
    const body = await res.json()
    return NextResponse.json(body, { status: res.status })
  } catch {
    // Unreachable / timeout — the widget retains its last-good SSR value.
    return NextResponse.json({ error: 'unavailable' }, { status: 502 })
  }
}
