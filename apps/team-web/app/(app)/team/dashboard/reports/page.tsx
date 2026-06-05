import { requireOwnerSession } from '@/lib/auth/require-owner-session'
import { getDictionary, resolveLocale, t } from '@/lib/i18n'
import { fetchReports } from '@/lib/owner-dashboard-client'

/**
 * Owner dashboard — Reports (VT-338). Read-only list of monthly reports (tenant-level, no
 * customer PII). tenantId is session-derived server-side (never a client field). The PDF
 * RE-DOWNLOAD itself is VT-9.7 (a short-lived tenant-scoped signed Storage URL via
 * /api/team/reports/...); here each report with a stored PDF links to that route.
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
      <main data-testid="dashboard-reports">
        <h1>{t(dict, 'reports.title')}</h1>
        <p>{t(dict, 'common.loadError')}</p>
      </main>
    )
  }

  return (
    <main data-testid="dashboard-reports">
      <h1>{t(dict, 'reports.title')}</h1>
      {data.reports.length === 0 ? (
        <p>{t(dict, 'reports.none')}</p>
      ) : (
        <ul>
          {data.reports.map((r) => (
            <li key={r.year_month}>
              <span>{r.year_month}</span>
              {r.has_pdf ? (
                <a href={`/api/team/reports/${r.year_month}/download`} data-testid="report-download">
                  {' '}
                  {t(dict, 'reports.download')}
                </a>
              ) : (
                <span> {t(dict, 'reports.preparing')}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </main>
  )
}
