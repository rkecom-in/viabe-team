import { NextResponse } from 'next/server'

// Health probe must never be cached.
export const dynamic = 'force-dynamic'

/**
 * GET /api/team/health
 *
 * Liveness probe: returns 200 when the Supabase project is reachable, 503
 * otherwise. The deep probe (pgvector + Apache AGE extension checks) is
 * deferred to VT-122.
 */
export async function GET() {
  const url = process.env.NEXT_PUBLIC_TEAM_SUPABASE_URL
  if (!url) {
    return NextResponse.json(
      {
        status: 'error',
        supabase: 'unreachable',
        reason: 'NEXT_PUBLIC_TEAM_SUPABASE_URL not set',
      },
      { status: 503 },
    )
  }

  try {
    const res = await fetch(`${url}/auth/v1/health`, {
      cache: 'no-store',
      signal: AbortSignal.timeout(5000),
    })
    if (!res.ok) {
      return NextResponse.json(
        { status: 'error', supabase: 'unreachable', reason: `HTTP ${res.status}` },
        { status: 503 },
      )
    }
    return NextResponse.json({ status: 'ok', supabase: 'reachable' })
  } catch (err) {
    return NextResponse.json(
      {
        status: 'error',
        supabase: 'unreachable',
        reason: err instanceof Error ? err.message : 'unknown error',
      },
      { status: 503 },
    )
  }
}
