/**
 * VT-516 — Recent tm_audit_log read endpoint.
 *
 * GET /api/team/ops/tm-audit
 *   ?tenant_id=<uuid>     — optional exact filter
 *   ?event_layer=<str>    — optional: knows | gets | decides | does | asks
 *   ?event_kind=<str>     — optional exact match
 *   ?severity=<str>       — optional: info | warning | error | critical
 *   ?run_id=<uuid>        — optional pin to a single run
 *
 * Returns: { events: TmAuditEvent[] }   (last 100, newest first)
 *
 * Auth: ops-operator gated (requireOpsOperator) — same gate as the debug-events route.
 * Read: serverSecretClient (service-role, bypasses RLS) via fetchRecentTmAuditEvents.
 * PII boundary: every free-text/JSONB column is redacted by the orchestrator emit
 * helper at insert time (CL-390); we render ids + structured facts as-is.
 */

import { NextResponse, type NextRequest } from 'next/server'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { fetchRecentTmAuditEvents } from '@/lib/ops/tm-audit-events'

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
  const event_layer = sp.get('event_layer') ?? undefined
  const event_kind = sp.get('event_kind') ?? undefined
  const severity = sp.get('severity') ?? undefined
  const run_id = sp.get('run_id') ?? undefined

  let events: Awaited<ReturnType<typeof fetchRecentTmAuditEvents>>
  try {
    events = await fetchRecentTmAuditEvents({ tenant_id, event_layer, event_kind, severity, run_id })
  } catch (err) {
    console.error('tm-audit GET: query failed', err)
    return NextResponse.json({ error: 'query failed' }, { status: 500 })
  }

  return NextResponse.json({ events })
}
