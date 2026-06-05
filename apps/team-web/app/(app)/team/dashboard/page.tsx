import { requireOwnerSession } from '@/lib/auth/require-owner-session'
import { getDictionary, resolveLocale, t } from '@/lib/i18n'
import { fetchDashboardSummary } from '@/lib/owner-dashboard-client'

/**
 * Owner dashboard — Overview (index). VT-87 PR-1 + VT-338 i18n. Read-only: month/30d hero
 * metrics, top-5 customers (phones MASKED at source — last-4 only), recent-5 campaigns. The
 * layout gated the session; we re-derive tenantId server-side (never a client field). Locale
 * from ?lang (override) > tenant default > en.
 */
export default async function DashboardOverviewPage({
  searchParams,
}: {
  searchParams: Promise<{ lang?: string }>
}) {
  const { tenantId } = await requireOwnerSession()
  const sp = await searchParams
  const dict = getDictionary(resolveLocale(sp.lang))
  const summary = await fetchDashboardSummary(tenantId)

  if (!summary) {
    return (
      <main data-testid="dashboard-overview">
        <h1>{t(dict, 'dashboard.title')}</h1>
        <p>{t(dict, 'common.loadError')}</p>
      </main>
    )
  }

  return (
    <main data-testid="dashboard-overview">
      <h1>{t(dict, 'dashboard.title')}</h1>

      <section aria-label="metrics">
        <p>
          <strong data-testid="customer-count">{summary.customer_count}</strong>{' '}
          {t(dict, 'overview.customers')}
        </p>
      </section>

      <section aria-label="top-customers">
        <h2>{t(dict, 'overview.topCustomers')}</h2>
        <ul>
          {summary.top_customers.map((c, i) => (
            <li key={i}>
              <span>{c.display_name ?? t(dict, 'common.unknown')}</span>
              {c.phone_last4 ? <span> · {c.phone_last4}</span> : null}
              <span> · ₹{c.spend_rupees.toLocaleString('en-IN')}</span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="recent-campaigns">
        <h2>{t(dict, 'overview.recentCampaigns')}</h2>
        <ul>
          {summary.recent_campaigns.map((c) => (
            <li key={c.campaign_id}>
              <span>{c.status ?? t(dict, 'common.unknown')}</span>
              <span>
                {' '}
                · {c.responses} {t(dict, 'overview.responses')}
              </span>
            </li>
          ))}
        </ul>
      </section>
    </main>
  )
}
