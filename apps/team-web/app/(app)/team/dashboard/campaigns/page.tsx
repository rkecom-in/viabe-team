import { requireOwnerSession } from '@/lib/auth/require-owner-session'
import { getDictionary, resolveLocale, t } from '@/lib/i18n'
import { fetchCampaigns } from '@/lib/owner-dashboard-client'

/**
 * Owner dashboard — Campaigns (VT-338). Read-only history. Campaigns are tenant-level
 * rollups (no customer PII). tenantId is session-derived server-side (never a client field).
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
      <main data-testid="dashboard-campaigns">
        <h1>{t(dict, 'campaigns.title')}</h1>
        <p>{t(dict, 'common.loadError')}</p>
      </main>
    )
  }

  return (
    <main data-testid="dashboard-campaigns">
      <h1>{t(dict, 'campaigns.title')}</h1>
      {data.campaigns.length === 0 ? (
        <p>{t(dict, 'campaigns.none')}</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>{t(dict, 'campaigns.sent')}</th>
              <th>{t(dict, 'campaigns.status')}</th>
              <th>{t(dict, 'campaigns.responses')}</th>
            </tr>
          </thead>
          <tbody>
            {data.campaigns.map((c) => (
              <tr key={c.campaign_id}>
                <td>{c.sent_at ? new Date(c.sent_at).toLocaleDateString('en-IN') : '—'}</td>
                <td>{c.status ?? t(dict, 'common.unknown')}</td>
                <td>{c.responses}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  )
}
