/** VT-234 — Ops Console phase-1A: read-only debug view.
 *
 * Step-by-step envelope inspection. NO state-changing affordances
 * (override/replay = phase-1B, separate row).
 *
 * VT-412 (PR-D): opened to scoped VTR operators. requireFazal() → requireOpsOperator()
 * + a per-tenant canReplayRun gate (run tenant resolved SERVER-SIDE from the run row —
 * never a client field, VT-293/294 IDOR rule; this page previously read any runId with
 * NO tenant resolution at all — that gap is closed here). Role-split read:
 *   - VTAdmin / Fazal → full service-role read (fetchRunReplay), unchanged.
 *   - VTR → de-identified, assignment-scoped read via vtrRunTimeline → vtr_step_timeline
 *     (decision_rationale — the raw think-text this page rendered at line 77 — is absent by
 *     construction for a VTR; envelopes keys-only; error/tool_calls dropped).
 */

import { notFound, redirect } from 'next/navigation'

import { JsonPretty } from '@/components/ops/json-pretty'
import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { fetchRunReplay, type PipelineStepRow } from '@/lib/ops/data-access'
import { canReplayRun, deIdentifyStepForVtr, hasFullReadAccess } from '@/lib/ops/run-replay-access'
import { vtrRunTimeline, type VtrTimelineStep } from '@/lib/orchestrator-client'
import { serverSecretClient } from '@/lib/supabase-client'

export const dynamic = 'force-dynamic'

interface PageProps {
  params: Promise<{ runId: string }>
}

/** Resolve the run's tenant SERVER-SIDE from the run row (VT-293/294). */
async function fetchRunTenant(runId: string): Promise<string | null> {
  const client = serverSecretClient()
  const { data } = await client
    .from('pipeline_runs')
    .select('tenant_id')
    .eq('id', runId)
    .maybeSingle()
  return (data as { tenant_id: string } | null)?.tenant_id ?? null
}

/** Map a de-identified vtr_step_timeline row to the debug view's PipelineStepRow shape. */
function vtrStepToRow(s: VtrTimelineStep): PipelineStepRow {
  return deIdentifyStepForVtr({
    id: s.step_id ?? '',
    run_id: s.run_id,
    step_seq: s.step_seq ?? 0,
    step_kind: s.step_kind ?? '',
    step_name: s.step_name,
    parent_step_id: null,
    status: s.step_status ?? '',
    decision_rationale: null,
    model_used: null,
    tokens_input: null,
    tokens_output: null,
    cost_paise: null,
    duration_ms: s.duration_ms,
    tool_calls: null,
    input_envelope: s.input_envelope,
    output_envelope: s.output_envelope,
    error: null,
    started_at: s.started_at ?? '',
    ended_at: s.ended_at,
  })
}

function statusPillClass(status: string): string {
  switch (status) {
    case 'ok':
    case 'success':
      return 'bg-green-50 text-green-700 border border-green-200'
    case 'error':
    case 'failed':
      return 'bg-red-50 text-red-700 border border-red-200'
    case 'running':
      return 'bg-blue-50 text-blue-700 border border-blue-200'
    default:
      return 'bg-gray-100 text-gray-700 border border-gray-200'
  }
}

function StepCard({ step }: { step: PipelineStepRow }) {
  const durMs = step.duration_ms ?? null
  const tokensIn = step.tokens_input ?? 0
  const tokensOut = step.tokens_output ?? 0
  const costPaise = step.cost_paise ?? 0
  return (
    <article
      data-element="debug-step"
      data-step-seq={step.step_seq}
      data-step-kind={step.step_kind}
      data-step-status={step.status}
      className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 mb-4"
    >
      <header className="flex items-start justify-between gap-4 mb-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">
            <span data-element="step-kind">{step.step_kind}</span>
            {step.step_name ? (
              <span className="text-gray-500 font-normal">
                {' '}
                — {step.step_name}
              </span>
            ) : null}
          </h2>
          <p className="text-xs text-gray-500 font-mono mt-1">
            seq #{step.step_seq} · {durMs !== null ? `${durMs}ms` : '—'} ·{' '}
            in={tokensIn} · out={tokensOut} · cost={costPaise}p
          </p>
        </div>
        <span
          data-element="step-status"
          className={`text-xs px-2 py-1 rounded ${statusPillClass(step.status)}`}
        >
          {step.status}
        </span>
      </header>

      {step.decision_rationale ? (
        <div
          data-element="reasoning-trace"
          className="text-xs text-gray-700 italic bg-gray-50 rounded p-3 mb-2"
        >
          {step.decision_rationale}
        </div>
      ) : null}

      <JsonPretty label="input_envelope" value={step.input_envelope} />
      <JsonPretty label="output_envelope" value={step.output_envelope} />
      {step.status === 'error' ? (
        <JsonPretty label="error" value={step.error} defaultOpen={true} />
      ) : null}
      <JsonPretty label="tool_calls" value={step.tool_calls} />
    </article>
  )
}

export default async function RunDebugPage({ params }: PageProps) {
  const { runId } = await params
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      redirect(`/team/ops/login?next=/team/ops/runs/${runId}/debug`)
    }
    throw err
  }

  // Resolve the run's tenant server-side, then gate on assignment BEFORE the read.
  let tenantId: string | null = null
  try {
    tenantId = await fetchRunTenant(runId)
  } catch (err) {
    console.error('RunDebugPage: tenant resolve failed', err)
    notFound()
  }
  if (!canReplayRun(operator.assignedTenants, tenantId)) {
    notFound()
  }

  let steps: PipelineStepRow[] = []
  try {
    if (hasFullReadAccess(operator.assignedTenants)) {
      steps = await fetchRunReplay(runId)
    } else {
      const timeline = await vtrRunTimeline(operator.operatorId, runId)
      steps = timeline.steps.map(vtrStepToRow)
    }
  } catch (err) {
    console.error('RunDebugPage: fetch failed', err)
    notFound()
  }
  if (steps.length === 0) notFound()

  return (
    <main
      className="ops-run-debug bg-gray-50 min-h-screen p-6"
      data-area="team-ops-run-debug"
      data-run-id={runId}
    >
      <header className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 mb-4">
        <h1 className="text-2xl font-semibold text-gray-900">Debug view</h1>
        <p className="text-sm text-gray-600 mt-1">
          run_id:{' '}
          <code className="font-mono text-xs text-gray-700">{runId}</code> ·
          steps: {steps.length}
        </p>
        <p className="text-xs text-gray-500 mt-2">
          Read-only envelope inspection. Step-by-step pipeline replay.
        </p>
        <a
          href={`/team/ops/runs/${runId}`}
          data-element="back-to-run"
          className="inline-block mt-3 text-sm text-blue-700 hover:underline"
        >
          ← back to run replay
        </a>
      </header>
      <section data-section="steps">
        {steps.map((s) => (
          <StepCard key={s.id} step={s} />
        ))}
      </section>
    </main>
  )
}
