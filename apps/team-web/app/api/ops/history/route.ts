/**
 * VT-201 PR-2 — historical pipeline_steps fetch endpoint.
 *
 * GET /api/ops/history?date=YYYY-MM-DD&hour=H&cursor=...&tenant_ids=a,b&step_kinds=...&statuses=...&q=...
 *
 * VT-412 (PR-D): opened to scoped VTR operators. requireFazal() → requireOpsOperator().
 * The read path splits by role:
 *   - VTAdmin / Fazal (assignedTenants null) → full service-role read, unchanged.
 *   - VTR → the requested tenant filter is NARROWED server-side to the operator's assigned
 *     set (scopeHistoryTenantFilter — never trust the client tenant_ids, VT-293/294 IDOR);
 *     an absent filter defaults to the WHOLE assigned set (never "all"); a VTR with no
 *     tenant in scope gets [] rows (fail-closed). Every returned row is de-identified
 *     (deIdentifyStepForVtr: no decision_rationale / error / tool_calls; envelopes
 *     keys-only) — a conservative superset of the vtr_step_timeline view's redaction.
 *
 * Keyset paginated; returns `{ rows, next_cursor }` per call. Client pages through until
 * next_cursor is null.
 */

import { NextResponse, type NextRequest } from 'next/server'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { fetchHistoricalSteps } from '@/lib/ops/data-access'
import {
  canUseFreeTextSearch,
  deIdentifyStepForVtr,
  hasFullReadAccess,
  scopeHistoryTenantFilter,
} from '@/lib/ops/run-replay-access'

export const dynamic = 'force-dynamic'

export async function GET(req: NextRequest): Promise<Response> {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      return NextResponse.json({ error: 'unauthenticated' }, { status: 401 })
    }
    throw err
  }

  const sp = req.nextUrl.searchParams
  const date = sp.get('date')
  if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    return NextResponse.json(
      { error: 'date param required (YYYY-MM-DD)' },
      { status: 400 },
    )
  }
  const hourParam = sp.get('hour')
  let hour: number | undefined
  if (hourParam !== null) {
    const h = Number(hourParam)
    if (!Number.isInteger(h) || h < 0 || h > 23) {
      return NextResponse.json({ error: 'invalid hour' }, { status: 400 })
    }
    hour = h
  }
  const cursor = sp.get('cursor') ?? null
  const limitParam = sp.get('limit')
  const limit = limitParam ? Math.min(Math.max(Number(limitParam) || 100, 1), 500) : 100

  // VT-412 — narrow the requested tenant filter to the operator's scope server-side.
  // A VTR can NEVER widen past its assigned set by passing tenant_ids it isn't assigned to.
  const requested = sp.get('tenant_ids')?.split(',').filter(Boolean)
  const { tenantIds, denied } = scopeHistoryTenantFilter(operator.assignedTenants, requested)
  if (denied) {
    // VTR with no tenant in scope — fail-closed, no rows (and no query issued).
    return NextResponse.json({ rows: [], next_cursor: null })
  }

  // VT-412 PR-D (Finding 1) — the `q` free-text search runs against the RAW
  // envelope tsvector (envelope_search_tsv, migrations/038) BEFORE de-id, so for a
  // VTR it would be a result-set membership ORACLE over un-de-identified text even
  // though returned rows are de-identified. DROP `q` for a VTR; VTAdmin/Fazal keep
  // it. Role resolved server-side from the gated operator — never a client flag.
  const q = canUseFreeTextSearch(operator.assignedTenants)
    ? (sp.get('q') ?? undefined)
    : undefined

  const result = await fetchHistoricalSteps({
    date,
    hour,
    cursor,
    tenantIds,
    stepKinds: sp.get('step_kinds')?.split(',').filter(Boolean),
    statuses: sp.get('statuses')?.split(',').filter(Boolean),
    q,
    limit,
  })

  // VT-412 — de-identify every row for a VTR (VTAdmin/Fazal keep the full rows).
  const rows = hasFullReadAccess(operator.assignedTenants)
    ? result.rows
    : result.rows.map(deIdentifyStepForVtr)

  return NextResponse.json({
    rows,
    next_cursor: result.nextCursor,
  })
}
