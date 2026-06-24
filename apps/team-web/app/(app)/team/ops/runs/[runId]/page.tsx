/** Ops Console — n8n-style run replay (VT-123 view 3 of 3; LOAD-BEARING).
 *
 * VT-201 PR-3: adds prev/next-run navigation header.
 *
 * VT-412 (PR-D): opened to scoped VTR operators. requireFazal() → requireOpsOperator()
 * + a per-tenant canReplayRun gate (the run's tenant is resolved SERVER-SIDE from the run
 * row — never a client field, the VT-293/294 IDOR rule). The read path splits by role:
 *   - VTAdmin / Fazal → the full service-role read (fetchRunReplay), unchanged.
 *   - VTR → the de-identified, assignment-scoped read through the orchestrator
 *     (vtrRunTimeline → the mig-132/134 vtr_step_timeline VIEW): decision_rationale /
 *     error / tool_calls are absent by construction, envelopes are keys-only. A VTR
 *     CANNOT replay an unassigned tenant's run even by guessing the runId — the app gate
 *     denies it AND the view self-scopes to the operator's assignments (defense in depth).
 */

import { notFound, redirect } from 'next/navigation'

import { RunWaterfall } from '@/components/ops/run-waterfall'
import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { fetchPrevNextRun, fetchRunReplay, type PipelineStepRow } from '@/lib/ops/data-access'
import { canReplayRun, hasFullReadAccess } from '@/lib/ops/run-replay-access'
import { vtrRunTimeline, type VtrTimelineStep } from '@/lib/orchestrator-client'
import { serverSecretClient } from '@/lib/supabase-client'

export const dynamic = 'force-dynamic'

interface PageProps {
  params: Promise<{ runId: string }>
}

/** Resolve the run's tenant SERVER-SIDE from the run row (VT-293/294 — never a client
 *  field). Used for the assignment gate; a null result fails the gate closed. */
async function fetchRunTenant(runId: string): Promise<string | null> {
  const client = serverSecretClient()
  const { data } = await client
    .from('pipeline_runs')
    .select('tenant_id')
    .eq('id', runId)
    .maybeSingle()
  return (data as { tenant_id: string } | null)?.tenant_id ?? null
}

/** Map a de-identified vtr_step_timeline row to the waterfall's PipelineStepRow shape.
 *  The view already excludes decision_rationale / error / tool_calls and key-projects
 *  envelopes — we surface those as null/keys so the renderer shows no PII. */
function vtrStepToRow(s: VtrTimelineStep): PipelineStepRow {
  return {
    id: s.step_id ?? '',
    run_id: s.run_id,
    step_seq: s.step_seq ?? 0,
    step_kind: s.step_kind ?? '',
    step_name: s.step_name,
    parent_step_id: null,
    status: s.step_status ?? '',
    decision_rationale: null, // not in the view — never reaches a VTR
    model_used: null,
    tokens_input: null,
    tokens_output: null,
    cost_paise: null,
    duration_ms: s.duration_ms,
    tool_calls: null, // not in the view
    input_envelope: s.input_envelope, // keys-only by construction
    output_envelope: s.output_envelope, // keys-only by construction
    error: null, // not in the view
    started_at: s.started_at ?? '',
    ended_at: s.ended_at,
  }
}

export default async function RunReplayPage({ params }: PageProps) {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops')
    throw err
  }
  const { runId } = await params

  // Resolve the run's tenant server-side BEFORE any data read (IDOR gate substrate).
  let tenantId: string | null = null
  try {
    tenantId = await fetchRunTenant(runId)
  } catch (err) {
    console.error('RunReplayPage: tenant resolve failed', err)
    notFound()
  }

  // Assignment gate: a VTR may only replay runs of its assigned tenants; VTAdmin/Fazal
  // pass. Unknown tenant (null) fails closed. This denial is BEFORE the data read, so an
  // unassigned VTR never even issues the query (no run-existence oracle).
  if (!canReplayRun(operator.assignedTenants, tenantId)) {
    notFound()
  }

  // Role-split read: VTAdmin → full service-role; VTR → de-identified view via orchestrator.
  let steps: PipelineStepRow[] = []
  try {
    if (hasFullReadAccess(operator.assignedTenants)) {
      steps = await fetchRunReplay(runId)
    } else {
      const timeline = await vtrRunTimeline(operator.operatorId, runId)
      steps = timeline.steps.map(vtrStepToRow)
    }
  } catch (err) {
    console.error('RunReplayPage: fetch failed', err)
    notFound()
  }
  if (steps.length === 0) notFound()

  let prevNext: Awaited<ReturnType<typeof fetchPrevNextRun>> = {
    prevRunId: null,
    nextRunId: null,
  }
  if (tenantId) {
    try {
      prevNext = await fetchPrevNextRun(tenantId, runId)
    } catch (err) {
      console.error('RunReplayPage: prev/next fetch failed', err)
    }
  }

  return (
    <main
      className="ops-run-replay bg-gray-50 min-h-screen p-6 space-y-6"
      data-area="team-ops-run"
      data-run-id={runId}
    >
      <header className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-2">
        <h1 className="text-2xl font-semibold text-gray-900">Run replay</h1>
        <p className="text-sm text-gray-600">
          run_id: <code className="font-mono text-xs text-gray-700">{runId}</code> | tenant_id:{' '}
          <code className="font-mono text-xs text-gray-700">{tenantId ?? '—'}</code> | steps: {steps.length}
        </p>
        <a
          href={`/team/ops/runs/${runId}/debug`}
          data-element="debug-view-link"
          className="inline-block bg-blue-50 text-blue-700 px-3 py-1 rounded text-sm hover:bg-blue-100"
        >
          Debug view
        </a>
        <nav data-section="prev-next-run">
          {prevNext.prevRunId ? (
            <a
              data-element="prev-run"
              href={`/team/ops/runs/${prevNext.prevRunId}`}
            >
              ← previous run
            </a>
          ) : (
            <span data-element="prev-run-none">no previous run</span>
          )}
          {prevNext.nextRunId ? (
            <a
              data-element="next-run"
              href={`/team/ops/runs/${prevNext.nextRunId}`}
            >
              next run →
            </a>
          ) : (
            <span data-element="next-run-none">no next run</span>
          )}
        </nav>
      </header>
      <RunWaterfall steps={steps} />
    </main>
  )
}
