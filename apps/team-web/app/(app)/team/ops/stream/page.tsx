/** Ops Console — live stream of pipeline_steps (VT-201 PR-1). */

import { cookies } from 'next/headers'
import { redirect } from 'next/navigation'

import { StreamFeed } from '@/components/ops/stream-feed'
import { issueOperatorJwt } from '@/lib/auth/operator-jwt'
import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import { fetchTopTenants } from '@/lib/ops/data-access'

export const dynamic = 'force-dynamic'

export default async function OpsStreamPage() {
  let fazalUuid: string
  try {
    ;({ fazalUuid } = await requireFazal())
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/login')
    throw err
  }

  const tenants = await fetchTopTenants(20)

  // Mint a short-lived operator JWT for the browser Supabase Realtime
  // subscription. 5-min TTL per lib/auth/operator-jwt.ts; client-side
  // re-fetches via API route on expiry. Phase-1 single-operator
  // (Fazal); Phase-2 migrates to server-side SSE per migration 030
  // header note.
  const operatorJwt = await issueOperatorJwt(fazalUuid)

  return (
    <main className="ops-stream" data-area="team-ops-stream">
      <header>
        <h1>Ops Console — Live Stream</h1>
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
