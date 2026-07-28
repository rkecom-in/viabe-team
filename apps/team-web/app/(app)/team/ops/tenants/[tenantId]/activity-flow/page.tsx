/**
 * VT-704/VT-713 — Tenant ACTIVITY FLOW. Latest-first, FULL-history, load-on-scroll (the
 * FlowStream client component pages older events via a guarded server action), with a
 * year → month → day hotlink index that jumps the stream to any date (?before=<end-of-day>).
 * Visual language = the e2e-sim flow-diagram artifact (Fazal-directed).
 *
 * Guarded per-page (requireOpsOperator) + tenant-scoped (canAccessTenant, deny render);
 * the server action re-guards independently.
 */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { canAccessTenant } from '@/lib/ops/assignments'
import { fetchFlowDayIndex, fetchFlowPage } from '@/lib/ops/activity-flow'
import { FlowStream } from '@/components/ops/flow-stream'

export const dynamic = 'force-dynamic'

interface PageProps {
  params: Promise<{ tenantId: string }>
  searchParams: Promise<{ before?: string }>
}

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function endOfMonth(year: string, month: string): string {
  // last instant of the month, UTC — Date.UTC(month index + 1, day 0) = last day of `month`
  const lastDay = new Date(Date.UTC(Number(year), Number(month), 0)).getUTCDate()
  return `${year}-${month}-${String(lastDay).padStart(2, '0')}T23:59:59.999Z`
}

export default async function TenantActivityFlowPage({ params, searchParams }: PageProps) {
  const { tenantId } = await params
  const { before: rawBefore } = await searchParams
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      redirect(`/team/ops/login?next=/team/ops/tenants/${tenantId}/activity-flow`)
    }
    throw err
  }

  if (!canAccessTenant(operator.assignedTenants, tenantId)) {
    return (
      <main className="min-h-screen bg-[#F6F4EE] p-6" data-area="team-ops-activity-flow">
        <p className="text-sm text-[#B4560F]">Not assigned to this tenant.</p>
      </main>
    )
  }

  // ?before is an operator-supplied jump anchor — bad input just means "from the top".
  const before = rawBefore && /^\d{4}-\d{2}-\d{2}T[\d:.]+Z$/.test(rawBefore) ? rawBefore : null
  const [page, dayIndex] = await Promise.all([
    fetchFlowPage(operator, tenantId, { before }),
    fetchFlowDayIndex(operator, tenantId),
  ])

  const base = `/team/ops/tenants/${tenantId}/activity-flow`

  return (
    <main
      className="min-h-screen bg-[#F6F4EE] px-4 pb-16 text-[#1C2B25]"
      data-area="team-ops-activity-flow"
      data-tenant-id={tenantId}
    >
      <div className="mx-auto max-w-3xl">
        <header className="space-y-3 pt-10 pb-2">
          <h1 className="text-[1.6rem] leading-tight font-semibold [text-wrap:balance]">
            Activity flow
          </h1>
          <p className="text-sm text-[#5C6B63]">
            tenant_id <code className="font-mono text-xs text-[#43554C]">{tenantId}</code>
            {' '}· latest first{before ? ` · jumped to ${before.slice(0, 10)}` : ''}
          </p>
          <p className="text-xs text-[#5C6B63]">
            <a className="underline" href={`/team/ops/tenants/${tenantId}`}>
              ← tenant dashboard
            </a>{' '}
            ·{' '}
            <a className="underline" href={`/team/ops/tenants/${tenantId}/plan`}>
              plan →
            </a>
            {before ? (
              <>
                {' '}·{' '}
                <a className="underline" href={base}>
                  back to latest
                </a>
              </>
            ) : null}
          </p>

          {dayIndex.years.length > 0 ? (
            <nav
              aria-label="Jump to month"
              className="flex flex-wrap items-baseline gap-2 rounded-lg border border-[#DDE4DD] bg-white px-4 py-3 text-sm"
              data-flow-datenav
            >
              <span className="text-[11px] font-medium tracking-[0.05em] text-[#93A399] uppercase">
                Jump to
              </span>
              {dayIndex.years.flatMap((y) =>
                y.months.map((m) => (
                  <a
                    key={`${y.year}-${m.month}`}
                    className="rounded-full bg-[#EAEFE9] px-3 py-1 font-mono text-[12px] text-[#43554C] hover:bg-[#D8EFCB]"
                    href={`${base}?before=${encodeURIComponent(endOfMonth(y.year, m.month))}`}
                  >
                    {y.year}-{MONTH_NAMES[Number(m.month) - 1]}
                  </a>
                )),
              )}
            </nav>
          ) : null}
        </header>

        {page.events.length === 0 ? (
          <p className="text-sm text-[#5C6B63]">No activity recorded.</p>
        ) : (
          <FlowStream tenantId={tenantId} initialEvents={page.events} initialCursor={page.nextBefore} />
        )}
      </div>
    </main>
  )
}
