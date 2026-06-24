/** Ops Console — live stream of pipeline_steps (VT-201 PR-1).
 *
 * VT-412 (PR-D): gated with requireOpsOperator() (was requireFazal()). The live feed is a
 * DIRECT browser Supabase Realtime subscription to the RAW pipeline_steps table, admitted by
 * the operator-claim JWT under the migration-030 RLS policy. That policy admits ANY operator
 * claim to ALL tenants' RAW rows (decision_rationale + raw envelopes ride the realtime payload
 * un-projected) — there is NO per-operator assignment scope and NO view-projection on the
 * realtime path. De-identifying + assignment-scoping the LIVE feed for a VTR is the deferred
 * migration-030 "Phase-2 server-side SSE proxy" (a real DB/architecture gap, NOT a thin change):
 * a VTR-safe live feed needs the SSE proxy to (a) read service-role, (b) project through the
 * de-identified shape, and (c) filter to the operator's assignments server-side.
 *
 * So VT-412 admits a VTR to the SCOPED + de-identified surfaces it CAN serve safely today
 * (run replay, debug, history — all view/server-projected) and FAILS CLOSED here: a VTR sees a
 * role-restricted notice rather than the raw, unscoped firehose. VTAdmin / Fazal keep the live
 * feed (full access). See the VT-412 report: DB view gap = realtime SSE-proxy de-id.
 */

import { redirect } from 'next/navigation'

import { StreamFeed } from '@/components/ops/stream-feed'
import { issueOperatorJwt, OPERATOR_STREAM_TTL_SEC } from '@/lib/auth/operator-jwt'
import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { fetchTopTenants } from '@/lib/ops/data-access'
import { hasFullReadAccess } from '@/lib/ops/run-replay-access'

export const dynamic = 'force-dynamic'

export default async function OpsStreamPage() {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops/stream')
    throw err
  }

  // A VTR cannot be served the live realtime feed safely yet (raw, unscoped — see header).
  // Fail closed with a role-restricted notice rather than the firehose. VTAdmin/Fazal proceed.
  if (!hasFullReadAccess(operator.assignedTenants)) {
    return (
      <main
        className="ops-stream bg-gray-50 min-h-screen p-6 space-y-6"
        data-area="team-ops-stream"
        data-stream-restricted="vtr"
      >
        <header>
          <h1 className="text-2xl font-semibold text-gray-900">Ops Console — Live Stream</h1>
        </header>
        <section className="rounded-lg border border-gray-200 bg-white p-6 shadow-sm">
          <p data-element="vtr-restricted" className="text-sm text-gray-600">
            The live stream is not available to your role yet. Use{' '}
            <a className="text-blue-700 hover:underline" href="/team/ops/stream/history">
              Stream History
            </a>{' '}
            or open a run from Run Control for a scoped, de-identified replay.
          </p>
        </section>
      </main>
    )
  }

  let tenants: Awaited<ReturnType<typeof fetchTopTenants>> = []
  try {
    tenants = await fetchTopTenants(20)
  } catch (err) {
    console.error('OpsStreamPage: fetchTopTenants failed', err)
  }

  // Mint a short-lived operator JWT for the browser Supabase Realtime subscription. 5-min TTL
  // (VT-236). Only reached on the VTAdmin/Fazal path — a VTR never gets this token (the
  // realtime RLS policy is unscoped, so issuing it to a VTR would leak all-tenant raw rows).
  const operatorJwt = await issueOperatorJwt(operator.operatorId, {
    ttlSec: OPERATOR_STREAM_TTL_SEC,
  })

  return (
    <main
      className="ops-stream bg-gray-50 min-h-screen p-6 space-y-6"
      data-area="team-ops-stream"
    >
      <header>
        <h1 className="text-2xl font-semibold text-gray-900">
          Ops Console — Live Stream
        </h1>
      </header>

      <StreamFeed
        operatorJwt={operatorJwt}
        availableTenants={tenants.map((t) => ({
          tenant_id: t.tenant_id,
          business_name: t.business_name,
        }))}
      />
    </main>
  )
}
