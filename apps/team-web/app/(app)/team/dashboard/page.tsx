import { requireOwnerSession } from '@/lib/auth/require-owner-session'
import { fetchDashboardSummary } from '@/lib/owner-dashboard-client'

/**
 * Owner dashboard — Overview (index). VT-87 PR-1. Read-only: month/30d hero metrics,
 * top-5 customers (phones MASKED at source — last-4 only), recent-5 campaigns. The layout
 * already gated the session; we re-derive tenantId server-side (never a client field).
 *
 * NEEDS-FAZAL: copy + i18n (EN/HI) land in PR-2 (VT-338); strings here are placeholders.
 */
export default async function DashboardOverviewPage() {
  const { tenantId } = await requireOwnerSession()
  const summary = await fetchDashboardSummary(tenantId)

  if (!summary) {
    return (
      <main data-testid="dashboard-overview">
        <h1>Dashboard</h1>
        <p>We couldn’t load your dashboard right now. Please try again shortly.</p>
      </main>
    )
  }

  return (
    <main data-testid="dashboard-overview">
      <h1>Dashboard</h1>

      <section aria-label="metrics">
        <p>
          <strong data-testid="customer-count">{summary.customer_count}</strong> customers
        </p>
      </section>

      <section aria-label="top-customers">
        <h2>Top customers</h2>
        <ul>
          {summary.top_customers.map((c, i) => (
            <li key={i}>
              <span>{c.display_name ?? 'Unknown'}</span>
              {c.phone_last4 ? <span> · {c.phone_last4}</span> : null}
              <span> · ₹{c.spend_rupees.toLocaleString('en-IN')}</span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="recent-campaigns">
        <h2>Recent campaigns</h2>
        <ul>
          {summary.recent_campaigns.map((c) => (
            <li key={c.campaign_id}>
              <span>{c.status ?? 'unknown'}</span>
              <span> · {c.responses} responses</span>
            </li>
          ))}
        </ul>
      </section>
    </main>
  )
}
