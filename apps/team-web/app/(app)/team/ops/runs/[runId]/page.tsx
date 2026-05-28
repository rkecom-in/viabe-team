/** Ops Console — n8n-style run replay (VT-123 view 3 of 3; LOAD-BEARING).
 *
 * VT-201 PR-3: adds prev/next-run navigation header.
 */

import { notFound, redirect } from 'next/navigation'

import { RunWaterfall } from '@/components/ops/run-waterfall'
import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import { fetchPrevNextRun, fetchRunReplay } from '@/lib/ops/data-access'
import { serverSecretClient } from '@/lib/supabase-client'

export const dynamic = 'force-dynamic'

interface PageProps {
  params: Promise<{ runId: string }>
}

async function fetchRunTenant(runId: string): Promise<string | null> {
  const client = serverSecretClient()
  const { data } = await client
    .from('pipeline_runs')
    .select('tenant_id')
    .eq('id', runId)
    .maybeSingle()
  return (data as { tenant_id: string } | null)?.tenant_id ?? null
}

export default async function RunReplayPage({ params }: PageProps) {
  try {
    await requireFazal()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/login')
    throw err
  }
  const { runId } = await params

  const [steps, tenantId] = await Promise.all([
    fetchRunReplay(runId),
    fetchRunTenant(runId),
  ])
  if (steps.length === 0) notFound()

  const prevNext = tenantId
    ? await fetchPrevNextRun(tenantId, runId)
    : { prevRunId: null, nextRunId: null }

  return (
    <main className="ops-run-replay" data-area="team-ops-run" data-run-id={runId}>
      <header>
        <h1>Run replay</h1>
        <p>
          run_id: <code>{runId}</code> | tenant_id:{' '}
          <code>{tenantId ?? '—'}</code> | steps: {steps.length}
        </p>
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
