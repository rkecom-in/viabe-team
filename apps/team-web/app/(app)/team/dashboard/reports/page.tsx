import { Card, EmptyState, LoadError, PageHeader } from '@/components/dashboard/ui'
import { requireOwnerSession } from '@/lib/auth/require-owner-session'
import { getDictionary, resolveLocale, t } from '@/lib/i18n'
import { fetchReports } from '@/lib/owner-dashboard-client'

/**
 * Owner dashboard — Reports (VT-338 + VT-372 styling). Read-only list of monthly reports
 * (tenant-level, no customer PII). tenantId is session-derived server-side (never a client
 * field). The PDF RE-DOWNLOAD itself is VT-9.7 (a short-lived tenant-scoped signed Storage
 * URL via /api/team/reports/...); here each report with a stored PDF links to that route.
 */
export default async function ReportsPage({
  searchParams,
}: {
  searchParams: Promise<{ lang?: string }>
}) {
  const { tenantId } = await requireOwnerSession()
  const sp = await searchParams
  const dict = getDictionary(resolveLocale(sp.lang))
  const data = await fetchReports(tenantId)

  if (!data) {
    return (
      <div data-testid="dashboard-reports">
        <LoadError title={t(dict, 'reports.title')} message={t(dict, 'common.loadError')} />
      </div>
    )
  }

  return (
    <div data-testid="dashboard-reports">
      <PageHeader title={t(dict, 'reports.title')} />
      {data.reports.length === 0 ? (
        <EmptyState>{t(dict, 'reports.none')}</EmptyState>
      ) : (
        <Card label="reports" className="!p-0">
          <ul className="divide-y divide-gray-100">
            {data.reports.map((r) => (
              <li key={r.year_month} className="flex items-center justify-between gap-3 px-5 py-4">
                <span className="font-medium text-gray-900">{r.year_month}</span>
                {r.has_pdf ? (
                  <a
                    href={`/api/team/reports/${r.year_month}/download`}
                    data-testid="report-download"
                    className="rounded-lg bg-emerald-600 px-4 py-1.5 text-sm font-semibold text-white transition hover:bg-emerald-700"
                  >
                    {t(dict, 'reports.download')}
                  </a>
                ) : (
                  <span className="text-sm text-gray-400">{t(dict, 'reports.preparing')}</span>
                )}
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  )
}
