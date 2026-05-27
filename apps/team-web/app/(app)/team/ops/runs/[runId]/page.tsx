/** Ops Console — n8n-style run replay (VT-123 view 3 of 3; LOAD-BEARING). */

import { notFound, redirect } from 'next/navigation'

import { RunWaterfall } from '@/components/ops/run-waterfall'
import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import { fetchRunReplay } from '@/lib/ops/data-access'

export const dynamic = 'force-dynamic'

interface PageProps {
  params: Promise<{ runId: string }>
}

export default async function RunReplayPage({ params }: PageProps) {
  try {
    await requireFazal()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/login')
    throw err
  }
  const { runId } = await params

  const steps = await fetchRunReplay(runId)
  if (steps.length === 0) notFound()

  return (
    <main className="ops-run-replay" data-area="team-ops-run" data-run-id={runId}>
      <header>
        <h1>Run replay</h1>
        <p>
          run_id: <code>{runId}</code> | tenant_id:{' '}
          <code>{steps[0]?.run_id}</code> | steps: {steps.length}
        </p>
      </header>
      <RunWaterfall steps={steps} />
    </main>
  )
}
