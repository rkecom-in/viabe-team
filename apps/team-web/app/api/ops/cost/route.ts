/**
 * VT-733 slice A — GET /api/ops/cost?window=today|month&tenant_ids=a,b
 *
 * What a tenant COSTS us, broken out by the axes Fazal named: "how much is being consumed by the
 * Manager, the specialists and any other integrations we have."
 *
 * Read-only and role-split, mirroring the run-replay cluster (VT-412):
 *   - VTAdmin / Fazal (assignedTenants === null) → every tenant.
 *   - VTR → exactly its assigned set. An absent filter means the WHOLE assigned set, never "all";
 *     requesting a tenant outside the set is a 403, not a silent narrowing, so a mis-scoped console
 *     surfaces rather than quietly showing less than the operator thinks they are seeing.
 *
 * IDOR (VT-293/294, caught twice): the tenant set is derived from the operator's own assignment via
 * scopeCostTenantFilter. The client's `tenant_ids` can only ever NARROW within that set.
 *
 * PII: cost rows carry ids, names and numbers — no message content ever reaches this surface.
 */

import { NextResponse, type NextRequest } from 'next/server'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { scopeCostTenantFilter } from '@/lib/ops/cost-access'
import { fetchTenantCapStatus, fetchTenantCostSummary } from '@/lib/ops/data-access'

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
  const windowParam = sp.get('window') ?? 'month'
  if (windowParam !== 'today' && windowParam !== 'month') {
    return NextResponse.json(
      { error: "window must be 'today' or 'month'" },
      { status: 400 },
    )
  }

  const requested = sp.get('tenant_ids')?.split(',').filter(Boolean)
  const { tenantIds, denied } = scopeCostTenantFilter(operator.assignedTenants, requested)
  if (denied) {
    return NextResponse.json({ error: 'tenant out of scope' }, { status: 403 })
  }

  const since = new Date()
  if (windowParam === 'today') {
    since.setUTCHours(0, 0, 0, 0)
  } else {
    since.setUTCDate(1)
    since.setUTCHours(0, 0, 0, 0)
  }

  const [rows, caps] = await Promise.all([
    fetchTenantCostSummary(since, tenantIds),
    fetchTenantCapStatus(tenantIds),
  ])

  return NextResponse.json({
    window: windowParam,
    since: since.toISOString(),
    // Stated so the console never has to infer whether it is seeing everything.
    scope: tenantIds === null ? 'all' : 'assigned',
    rows,
    caps,
  })
}
