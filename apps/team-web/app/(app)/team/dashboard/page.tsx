import { Card, CardTitle, LoadError, MetricTile, PageHeader, StatusChip } from '@/components/dashboard/ui'
import { requireOwnerSession } from '@/lib/auth/require-owner-session'
import { getDictionary, resolveLocale, t } from '@/lib/i18n'
import { fetchDashboardSummary } from '@/lib/owner-dashboard-client'

/**
 * Owner dashboard — Overview (index). VT-87 PR-1 + VT-338 i18n + VT-372 styling. Read-only:
 * customer-count metric tile, top-5 customers (phones MASKED at source — last-4 only), recent-5
 * campaigns. The layout gated the session; we re-derive tenantId server-side (never a client
 * field). Locale from ?lang (override) > tenant default > en.
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
      <div data-testid="dashboard-overview">
        <LoadError title={t(dict, 'dashboard.title')} message={t(dict, 'common.loadError')} />
      </div>
    )
  }

  return (
    <div data-testid="dashboard-overview">
      <PageHeader title={t(dict, 'dashboard.title')} subtitle={t(dict, 'overview.subtitle')} />

      <section aria-label="metrics" className="grid gap-4 sm:grid-cols-3">
        <MetricTile
          testid="customer-count"
          value={summary.customer_count.toLocaleString('en-IN')}
          label={t(dict, 'overview.customers')}
        />
      </section>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card label="top-customers">
          <CardTitle>{t(dict, 'overview.topCustomers')}</CardTitle>
          {summary.top_customers.length === 0 ? (
            <p className="mt-4 text-sm text-muted-foreground">{t(dict, 'campaigns.none')}</p>
          ) : (
            <ul className="mt-4 divide-y divide-border">
              {summary.top_customers.map((c, i) => (
                <li key={i} className="flex items-center justify-between gap-3 py-3">
                  <div className="min-w-0">
                    <p className="truncate font-medium text-foreground">
                      {c.display_name ?? t(dict, 'common.unknown')}
                    </p>
                    {c.phone_last4 ? (
                      <p className="text-xs text-muted-foreground">···· {c.phone_last4}</p>
                    ) : null}
                  </div>
                  <span className="shrink-0 font-semibold tabular-nums text-foreground">
                    ₹{c.spend_rupees.toLocaleString('en-IN')}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card label="recent-campaigns">
          <CardTitle>{t(dict, 'overview.recentCampaigns')}</CardTitle>
          {summary.recent_campaigns.length === 0 ? (
            <p className="mt-4 text-sm text-muted-foreground">{t(dict, 'campaigns.none')}</p>
          ) : (
            <ul className="mt-4 divide-y divide-border">
              {summary.recent_campaigns.map((c) => (
                <li key={c.campaign_id} className="flex items-center justify-between gap-3 py-3">
                  <StatusChip status={c.status} unknownLabel={t(dict, 'common.unknown')} />
                  <span className="text-sm text-muted-foreground">
                    {c.responses} {t(dict, 'overview.responses')}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  )
}
