/** VT-201 PR-2 — historical Ops stream view (/team/ops/stream/history). */

import { redirect } from 'next/navigation'

import { StreamHistoryView } from '@/components/ops/stream-history-view'
import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import { fetchTopTenants } from '@/lib/ops/data-access'

export const dynamic = 'force-dynamic'

interface OpsStreamHistoryPageProps {
  searchParams: Promise<{ date?: string }>
}

function todayIst(): string {
  const now = new Date()
  const ist = new Date(now.getTime() + (5 * 60 + 30) * 60_000)
  return ist.toISOString().slice(0, 10)
}

export default async function OpsStreamHistoryPage(props: OpsStreamHistoryPageProps) {
  try {
    await requireFazal()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops/stream/history')
    throw err
  }

  const sp = await props.searchParams
  const date = sp.date && /^\d{4}-\d{2}-\d{2}$/.test(sp.date) ? sp.date : todayIst()
  const tenants = await fetchTopTenants(20)

  return (
    <main
      className="ops-stream-history-page bg-gray-50 min-h-screen p-6 space-y-6"
      data-area="team-ops-stream-history"
    >
      <header>
        <h1 className="text-2xl font-semibold text-gray-900">
          Ops Console — Stream History
        </h1>
      </header>

      <StreamHistoryView
        initialDate={date}
        availableTenants={tenants.map((t) => ({
          tenant_id: t.tenant_id,
          business_name: t.business_name,
        }))}
      />
    </main>
  )
}
