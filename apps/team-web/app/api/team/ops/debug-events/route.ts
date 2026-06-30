/**
 * VT-515 — Recent debug_events read endpoint.
 *
 * GET /api/team/ops/debug-events
 *   ?tenant_id=<uuid>   — optional exact filter
 *   ?component=<str>    — optional exact filter
 *   ?severity=<str>     — optional: warning | error | critical
 *
 * Returns: { events: DebugEvent[] }   (last 100, newest first)
 *
 * Auth: ops-operator gated (requireOpsOperator) — same gate as all VTR endpoints.
 * Read: serverSecretClient (service-role, bypasses RLS) via fetchRecentDebugEvents.
 * PII boundary: error_message / error_stack are already redacted by the orchestrator
 * at emit time; we render as-is and do NOT attempt further processing.
 */

import { NextResponse, type NextRequest } from 'next/server'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { fetchRecentDebugEvents } from '@/lib/ops/debug-events'

export const dynamic = 'force-dynamic'

export async function GET(req: NextRequest): Promise<Response> {
  try {
    await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      return NextResponse.json({ error: 'unauthenticated' }, { status: 401 })
    }
    throw err
  }

  const sp = req.nextUrl.searchParams
  const tenant_id = sp.get('tenant_id') ?? undefined
  const component = sp.get('component') ?? undefined
  const severity = sp.get('severity') ?? undefined

  let events: Awaited<ReturnType<typeof fetchRecentDebugEvents>>
  try {
    events = await fetchRecentDebugEvents({ tenant_id, component, severity })
  } catch (err) {
    console.error('debug-events GET: query failed', err)
    return NextResponse.json({ error: 'query failed' }, { status: 500 })
  }

  return NextResponse.json({ events })
}
