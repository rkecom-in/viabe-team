/**
 * VT-375 (Phase B) — VTR Run-Control canvas (READ-ONLY).
 *
 * tenant tiles → per-tenant program tiles (Past / Running / Upcoming 7d) → per-run step
 * timeline. Reads ONLY the Phase-A GET surfaces via the orchestrator (vtrPrograms +
 * vtrRunTimeline) — the orchestrator is the sole door to VTR data (CL-425; fail-closed).
 * NO mutations in Phase B (pause/override/rerun = VT-376): this page ships zero POST calls
 * and zero buttons that mutate. The binding honesty copy (observed-tier non-controllable,
 * re-dispatch-not-time-travel, keys-only envelopes, pause-state-unverifiable on degrade,
 * no-ordering on concurrent holds) is rendered in the client tile/timeline components.
 *
 * EN-primary (VTR operator surface, not owner-facing) — noted per the row contract §6.
 *
 * Server component: gates with requireOpsOperator (Fazal → VTAdmin sees all; a VTR sees its
 * assigned set, fail-closed empty). Tenant list = the ops home tenant-listing source
 * (fetchTopTenants RPC). Programs + each run's timeline are fetched server-side per tenant so
 * the page degrades section-by-section and the client components never need the server-only
 * orchestrator secret/JWT.
 */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { fetchTopTenants } from '@/lib/ops/data-access'
import {
  vtrPrograms,
  vtrRunTimeline,
  type VtrProgramsResult,
  type VtrRunTimelineResult,
} from '@/lib/orchestrator-client'
import { RunControlCanvas, type TenantCanvasData } from './run-control-canvas'

export const dynamic = 'force-dynamic'

// Bound the per-render fan-out: tenant tiles shown, and how many runs per tenant get their
// timeline eagerly fetched (newest running first, then newest past). The canvas is a triage
// surface, not a full archive — VT-377 multi-VTR scoping will narrow this further.
const MAX_TENANTS = 20
const MAX_TIMELINES_PER_TENANT = 4
// Bounded fan-out: process tenants in chunks so a slow/timing-out orchestrator can't stack
// MAX_TENANTS × MAX_TIMELINES_PER_TENANT sequential timeouts (worst case ~23 min). Within a
// chunk every tenant — and within a tenant every timeline — runs in parallel.
const TENANT_CONCURRENCY = 5

export default async function RunControlPage() {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops/run-control')
    throw err
  }

  let loadError: string | null = null
  let tenants: { tenant_id: string; business_name: string | null }[] = []
  try {
    const top = await fetchTopTenants(MAX_TENANTS)
    tenants = top.map((t) => ({ tenant_id: t.tenant_id, business_name: t.business_name }))
    // A VTR is scoped to its assigned tenants (fail-closed); VTAdmin (null) sees all.
    if (operator.assignedTenants !== null) {
      const allowed = new Set(operator.assignedTenants)
      tenants = tenants.filter((t) => allowed.has(t.tenant_id))
    }
  } catch (err) {
    loadError = err instanceof Error ? err.message : 'unknown error'
    console.error('RunControlPage: tenant list load failed', err)
  }

  // Per-tenant programs projection + the timeline for the most relevant runs. Each tenant is
  // isolated in its own try/catch so one failing tenant degrades only its tile, and every wire
  // call fails closed (empty/degraded projection) rather than throwing out of the render.
  const operatorId = operator.operatorId
  async function loadTenant(t: {
    tenant_id: string
    business_name: string | null
  }): Promise<TenantCanvasData> {
    let programs: VtrProgramsResult
    try {
      programs = await vtrPrograms(operatorId, t.tenant_id)
    } catch (err) {
      console.error('RunControlPage: vtrPrograms failed', t.tenant_id, err)
      programs = {
        ok: false,
        past: [],
        running: [],
        upcoming7d: [],
        holds: [],
        degraded: true,
        reason: 'error',
      }
    }

    // Eagerly fetch timelines for running runs first, then the newest past runs, up to the cap.
    // All timelines for one tenant fetch in parallel; each still fails closed independently.
    const runIds = [
      ...programs.running.map((r) => r.run_id),
      ...programs.past.map((r) => r.run_id),
    ].slice(0, MAX_TIMELINES_PER_TENANT)

    const fetched = await Promise.all(
      runIds.map(async (runId): Promise<[string, VtrRunTimelineResult]> => {
        try {
          return [runId, await vtrRunTimeline(operatorId, runId)]
        } catch (err) {
          console.error('RunControlPage: vtrRunTimeline failed', runId, err)
          return [
            runId,
            { ok: false, runId: null, tenantId: null, steps: [], activeControls: [], reason: 'error' },
          ]
        }
      }),
    )
    const timelines: Record<string, VtrRunTimelineResult> = Object.fromEntries(fetched)

    return {
      tenantId: t.tenant_id,
      tenantName: t.business_name,
      programs,
      timelines,
    }
  }

  // Bounded-concurrency fan-out: chunk tenants so we never stack the full tenant × timeline
  // matrix of timeouts; within each chunk tenants load in parallel. Result order preserved.
  const data: TenantCanvasData[] = []
  for (let i = 0; i < tenants.length; i += TENANT_CONCURRENCY) {
    const chunk = tenants.slice(i, i + TENANT_CONCURRENCY)
    data.push(...(await Promise.all(chunk.map(loadTenant))))
  }

  return (
    <main data-area="team-ops-run-control" className="p-6 space-y-4">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold text-gray-900">Run Control — Canvas</h1>
        <p className="text-sm text-gray-500">
          Read-only view of each tenant&apos;s programs and step timelines. Controls
          (pause, override, re-dispatch) arrive in a later phase — nothing here mutates state.
        </p>
      </header>

      {loadError ? (
        <p
          data-section-error
          className="text-sm text-red-700 bg-red-50 border border-red-200 rounded p-3"
        >
          couldn&apos;t load tenants: {loadError}
        </p>
      ) : data.length === 0 ? (
        <p className="text-sm text-gray-500">No tenants in scope.</p>
      ) : (
        <RunControlCanvas tenants={data} />
      )}
    </main>
  )
}
