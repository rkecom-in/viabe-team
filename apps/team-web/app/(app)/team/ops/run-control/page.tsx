/**
 * VTR Run-Control canvas (VT-375 Phase B read surface + VT-376 Phase C controls).
 *
 * tenant tiles → per-tenant program tiles (Past / Running / Upcoming 7d) → per-run step
 * timeline. This SERVER component reads ONLY the Phase-A GET surfaces via the orchestrator
 * (vtrPrograms + vtrRunTimeline) — the orchestrator is the sole door to VTR data (CL-425;
 * fail-closed) — and never holds a mutation secret itself.
 *
 * Mutations (VT-376 Phase C — pause/release/override/rerun) ARE wired: the client tile/
 * timeline components (run-control-canvas → run-control-controls) call the server actions in
 * ./actions.ts, each gated server-side (requireOpsOperator + the orchestrator auth chain).
 * The binding honesty copy (observed-tier non-controllable, re-dispatch-not-time-travel,
 * keys-only envelopes, pause-state-unverifiable on degrade, no-ordering on concurrent holds,
 * overlap-escalation) is rendered in those client components.
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
import { scopeTenantsForOperator } from './scope-tenants'

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
    // A VTR is scoped to its assigned tenants (fail-closed); VTAdmin (null) sees all.
    tenants = scopeTenantsForOperator(
      top.map((t) => ({ tenant_id: t.tenant_id, business_name: t.business_name })),
      operator.assignedTenants,
    )
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
            {
              ok: false,
              runId: null,
              tenantId: null,
              steps: [],
              activeControls: [],
              rerunnable: false,
              forbiddenReason: null,
              openApproval: false,
              reason: 'error',
            },
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
        <h1 className="text-2xl font-semibold text-foreground">Run Control — Canvas</h1>
        <p className="text-sm text-muted-foreground">
          Each tenant&apos;s programs and step timelines, with pause/release, step override, and
          re-dispatch controls. Only controllable steps expose controls; observed steps are
          display-only. Every action is audited and re-checked server-side.
        </p>
      </header>

      {loadError ? (
        <p
          data-section-error
          className="text-sm text-destructive bg-destructive/10 border border-destructive/30 rounded p-3"
        >
          couldn&apos;t load tenants: {loadError}
        </p>
      ) : data.length === 0 ? (
        <p className="text-sm text-muted-foreground">No tenants in scope.</p>
      ) : (
        <RunControlCanvas tenants={data} />
      )}
    </main>
  )
}
