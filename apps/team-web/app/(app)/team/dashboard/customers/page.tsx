import Link from 'next/link'

import { DataTable, EmptyState, LoadError, PageHeader, StatusChip } from '@/components/dashboard/ui'
import { requireOwnerSession } from '@/lib/auth/require-owner-session'
import { getDictionary, resolveLocale, t } from '@/lib/i18n'
import { fetchCustomers } from '@/lib/owner-dashboard-client'

/**
 * Owner dashboard — Customers (VT-338 + VT-372 styling). Read-only, paginated. Phones MASKED
 * at source (last-4 only — the orchestrator never sends a raw phone; this is the OWNER's own
 * data, not the VTR PII gate). tenantId is session-derived server-side (never a client field —
 * IDOR); page/excluded/lang are pagination/filter/locale.
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
      <div data-testid="dashboard-customers">
        <LoadError title={t(dict, 'customers.title')} message={t(dict, 'common.loadError')} />
      </div>
    )
  }

  const totalPages = Math.max(1, Math.ceil(data.total / data.page_size))
  const qs = (p: number) =>
    `?page=${p}${excludedOnly ? '&excluded=1' : ''}${sp.lang ? `&lang=${sp.lang}` : ''}`

  const pageLinkClass =
    'rounded-lg border border-input px-3 py-1.5 text-sm font-medium text-foreground transition hover:bg-muted'

  return (
    <div data-testid="dashboard-customers">
      <PageHeader
        title={t(dict, 'customers.title')}
        subtitle={`${data.total.toLocaleString('en-IN')} ${t(dict, 'customers.total')}`}
      />

      {data.customers.length === 0 ? (
        <EmptyState>{t(dict, 'campaigns.none')}</EmptyState>
      ) : (
        <>
          <DataTable
            headers={[
              { label: t(dict, 'customers.name') },
              { label: t(dict, 'customers.phone') },
              { label: t(dict, 'customers.status') },
              { label: t(dict, 'customers.spend'), align: 'right' },
            ]}
          >
            {data.customers.map((c, i) => (
              <tr key={i} className="hover:bg-muted/40">
                <td className="px-4 py-3 font-medium text-foreground">
                  {c.display_name ?? t(dict, 'common.unknown')}
                </td>
                <td className="px-4 py-3 text-muted-foreground">
                  {c.phone_last4 ? `···· ${c.phone_last4}` : '—'}
                </td>
                <td className="px-4 py-3">
                  {c.opt_out_status ? (
                    <StatusChip status={c.opt_out_status} unknownLabel={t(dict, 'common.unknown')} />
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </td>
                <td className="px-4 py-3 text-right font-medium tabular-nums text-foreground">
                  ₹{c.spend_rupees.toLocaleString('en-IN')}
                </td>
              </tr>
            ))}
          </DataTable>

          <nav
            aria-label="pagination"
            className="mt-5 flex items-center justify-between gap-3 text-sm text-muted-foreground"
          >
            {page > 1 ? (
              <Link href={qs(page - 1)} className={pageLinkClass}>
                ← {t(dict, 'customers.prev')}
              </Link>
            ) : (
              <span />
            )}
            <span className="tabular-nums">
              {page} / {totalPages}
            </span>
            {page < totalPages ? (
              <Link href={qs(page + 1)} className={pageLinkClass}>
                {t(dict, 'customers.next')} →
              </Link>
            ) : (
              <span />
            )}
          </nav>
        </>
      )}
    </div>
  )
}
