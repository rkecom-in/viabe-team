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
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops')
    throw err
  }
  const { runId } = await params

  // Be tolerant of Supabase failures (CI stub, transient outage):
  // a 500 here surfaces the wrong signal to operators. notFound() is
  // the right negative outcome.
  let steps: Awaited<ReturnType<typeof fetchRunReplay>> = []
  let tenantId: string | null = null
  try {
    ;[steps, tenantId] = await Promise.all([
      fetchRunReplay(runId),
      fetchRunTenant(runId),
    ])
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
