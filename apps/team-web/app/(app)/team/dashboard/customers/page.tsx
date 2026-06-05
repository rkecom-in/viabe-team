import { requireOwnerSession } from '@/lib/auth/require-owner-session'
import { getDictionary, resolveLocale, t } from '@/lib/i18n'
import { fetchCustomers } from '@/lib/owner-dashboard-client'

/**
 * Owner dashboard — Customers (VT-338). Read-only, paginated. Phones MASKED at source
 * (last-4 only — the orchestrator never sends a raw phone). tenantId is session-derived
 * server-side (never a client field — IDOR); page/excluded/lang are pagination/filter/locale.
 */
export default async function CustomersPage({
  searchParams,
}: {
  searchParams: Promise<{ page?: string; excluded?: string; lang?: string }>
}) {
  const { tenantId } = await requireOwnerSession()
  const sp = await searchParams
  const dict = getDictionary(resolveLocale(sp.lang))
  const page = Math.max(1, Number(sp.page) || 1)
  const excludedOnly = sp.excluded === '1'
  const data = await fetchCustomers(tenantId, { page, pageSize: 20, excludedOnly })

  if (!data) {
    return (
      <main data-testid="dashboard-customers">
        <h1>{t(dict, 'customers.title')}</h1>
        <p>{t(dict, 'common.loadError')}</p>
      </main>
    )
  }

  const totalPages = Math.max(1, Math.ceil(data.total / data.page_size))
  const qs = (p: number) =>
    `?page=${p}${excludedOnly ? '&excluded=1' : ''}${sp.lang ? `&lang=${sp.lang}` : ''}`

  return (
    <main data-testid="dashboard-customers">
      <h1>{t(dict, 'customers.title')}</h1>
      <p>
        {data.total} {t(dict, 'customers.total')}
      </p>

      <table>
        <thead>
          <tr>
            <th>{t(dict, 'customers.name')}</th>
            <th>{t(dict, 'customers.phone')}</th>
            <th>{t(dict, 'customers.status')}</th>
            <th>{t(dict, 'customers.spend')}</th>
          </tr>
        </thead>
        <tbody>
          {data.customers.map((c, i) => (
            <tr key={i}>
              <td>{c.display_name ?? t(dict, 'common.unknown')}</td>
              <td>{c.phone_last4 ?? ''}</td>
              <td>{c.opt_out_status ?? ''}</td>
              <td>₹{c.spend_rupees.toLocaleString('en-IN')}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <nav aria-label="pagination">
        {page > 1 ? <a href={qs(page - 1)}>{t(dict, 'customers.prev')}</a> : null}
        <span>
          {' '}
          {page} / {totalPages}{' '}
        </span>
        {page < totalPages ? <a href={qs(page + 1)}>{t(dict, 'customers.next')}</a> : null}
      </nav>
    </main>
  )
}
