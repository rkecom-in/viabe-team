import { DataTable, EmptyState, LoadError, PageHeader, StatusChip } from '@/components/dashboard/ui'
import { requireOwnerSession } from '@/lib/auth/require-owner-session'
import { getDictionary, resolveLocale, t } from '@/lib/i18n'
import { fetchCampaigns } from '@/lib/owner-dashboard-client'

/**
 * Owner dashboard — Campaigns (VT-338 + VT-372 styling). Read-only history. Campaigns are
 * tenant-level rollups (no customer PII). tenantId is session-derived server-side (never a
 * client field).
 */
export default async function CampaignsPage({
  searchParams,
}: {
  searchParams: Promise<{ lang?: string }>
}) {
  const { tenantId } = await requireOwnerSession()
  const sp = await searchParams
  const dict = getDictionary(resolveLocale(sp.lang))
  const data = await fetchCampaigns(tenantId)

  if (!data) {
    return (
      <div data-testid="dashboard-campaigns">
        <LoadError title={t(dict, 'campaigns.title')} message={t(dict, 'common.loadError')} />
      </div>
    )
  }

  return (
    <div data-testid="dashboard-campaigns">
      <PageHeader title={t(dict, 'campaigns.title')} />
      {data.campaigns.length === 0 ? (
        <EmptyState>{t(dict, 'campaigns.none')}</EmptyState>
      ) : (
        <DataTable
          headers={[
            { label: t(dict, 'campaigns.sent') },
            { label: t(dict, 'campaigns.status') },
            { label: t(dict, 'campaigns.responses'), align: 'right' },
          ]}
        >
          {data.campaigns.map((c) => (
            <tr key={c.campaign_id} className="hover:bg-muted/40">
              <td className="px-4 py-3 text-muted-foreground">
                {c.sent_at ? new Date(c.sent_at).toLocaleDateString('en-IN') : '—'}
              </td>
              <td className="px-4 py-3">
                <StatusChip status={c.status} unknownLabel={t(dict, 'common.unknown')} />
              </td>
              <td className="px-4 py-3 text-right font-medium tabular-nums text-foreground">
                {c.responses}
              </td>
            </tr>
          ))}
        </DataTable>
      )}
    </div>
  )
}
