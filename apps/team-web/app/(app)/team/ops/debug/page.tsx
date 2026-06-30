/**
 * VT-515 — Ops Console "Debug / Failures" feed page.
 *
 * Shows pre-tenant signup failures + in-flight component errors live.
 * Server-fetches the initial batch (last 100 events); subscribes to
 * Supabase Realtime for new inserts on the client.
 *
 * Auth: VTAdmin-only (same as the live Stream page — the Realtime path
 * is an unscoped raw feed from the debug_events table; a VTR would need
 * the server-side SSE proxy path, deferred to VT-516).
 *
 * PII: error_message / error_stack are already redacted by the orchestrator
 * at emit time. Render as-is; do NOT decrypt or post-process.
 */

import { redirect } from 'next/navigation'

import { DebugFeed } from '@/components/ops/debug-feed'
import { OpsPageHeader, OpsError } from '@/components/ops/ops-ui'
import { issueOperatorJwt, OPERATOR_STREAM_TTL_SEC } from '@/lib/auth/operator-jwt'
import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { fetchRecentDebugEvents } from '@/lib/ops/debug-events'
import { hasFullReadAccess } from '@/lib/ops/run-replay-access'
import type { DebugEvent } from '@/lib/ops/stream'

export const dynamic = 'force-dynamic'

export default async function OpsDebugPage() {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops/debug')
    throw err
  }

  // Same access restriction as the live Stream page: the Realtime path is unscoped.
  // A VTR sees a notice; VTAdmin / Fazal get the full feed.
  if (!hasFullReadAccess(operator.assignedTenants)) {
    return (
      <main
        data-area="team-ops-debug"
        data-debug-restricted="vtr"
        className="space-y-5 p-6"
      >
        <OpsPageHeader
          title="Debug / Failures"
          subtitle="Live component failure feed."
        />
        <section className="rounded-lg border border-border bg-card p-6 shadow-sm">
          <p data-element="vtr-restricted" className="text-sm text-muted-foreground">
            The live debug feed is not available to your role yet (unscoped Realtime path).
            Use{' '}
            <a className="text-primary hover:underline" href="/team/ops/stream/history">
              Stream History
            </a>{' '}
            for a scoped, de-identified view.
          </p>
        </section>
      </main>
    )
  }

  // Mint a short-lived operator JWT for the browser Realtime subscription.
  const operatorJwt = await issueOperatorJwt(operator.operatorId, {
    ttlSec: OPERATOR_STREAM_TTL_SEC,
  })

  // Pre-fetch the initial batch server-side so the feed renders immediately.
  let initialEvents: DebugEvent[] = []
  let fetchError: string | null = null
  try {
    initialEvents = await fetchRecentDebugEvents({}, 100)
  } catch (err) {
    fetchError = err instanceof Error ? err.message : 'unknown error'
    console.error('OpsDebugPage: fetchRecentDebugEvents failed', err)
  }

  return (
    <main data-area="team-ops-debug" className="space-y-5 p-6">
      <OpsPageHeader
        title="Debug / Failures"
        subtitle="Live feed of component failures and degradations. Pre-tenant events (no tenant_id) appear here first — the immediate value for signup debugging."
      />
      {fetchError ? (
        <OpsError data-section-error>Initial load failed: {fetchError}</OpsError>
      ) : null}
      <DebugFeed operatorJwt={operatorJwt} initialEvents={initialEvents} />
    </main>
  )
}
