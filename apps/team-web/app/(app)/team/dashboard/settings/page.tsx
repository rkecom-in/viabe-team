import { requireOwnerSession } from '@/lib/auth/require-owner-session'
import { getDictionary, resolveLocale, t } from '@/lib/i18n'
import { fetchSettings } from '@/lib/owner-dashboard-client'

/**
 * Owner dashboard — Settings (VT-338). Read-only: business profile + plan/trial + the
 * DSR-init buttons (the ONLY write-exception — owner-initiated requests to /api/dsr/*, not
 * edits). tenantId is session-derived server-side (never a client field). No customer PII.
 */
export default async function SettingsPage({
  searchParams,
}: {
  searchParams: Promise<{ lang?: string }>
}) {
  const { tenantId } = await requireOwnerSession()
  const sp = await searchParams
  const dict = getDictionary(resolveLocale(sp.lang))
  const data = await fetchSettings(tenantId)

  if (!data) {
    return (
      <main data-testid="dashboard-settings">
        <h1>{t(dict, 'settings.title')}</h1>
        <p>{t(dict, 'common.loadError')}</p>
      </main>
    )
  }

  const b = data.business
  const p = data.plan

  return (
    <main data-testid="dashboard-settings">
      <h1>{t(dict, 'settings.title')}</h1>

      <section aria-label="business">
        <h2>{t(dict, 'settings.business')}</h2>
        <dl>
          <dt>{t(dict, 'customers.name')}</dt>
          <dd>{b?.business_name ?? '—'}</dd>
          <dd>{b?.owner_name ?? '—'}</dd>
          <dd>{b?.business_archetype ?? '—'}</dd>
          <dd>{b?.working_hours ?? '—'}</dd>
        </dl>
      </section>

      <section aria-label="plan">
        <h2>{t(dict, 'settings.plan')}</h2>
        <dl>
          <dd>{p.plan_tier ?? '—'}</dd>
          <dd>{p.trial_ends_at ? new Date(p.trial_ends_at).toLocaleDateString('en-IN') : '—'}</dd>
        </dl>
      </section>

      <section aria-label="privacy">
        <h2>{t(dict, 'settings.privacy')}</h2>
        {/* DSR-init: owner-initiated requests to the existing endpoints — the only write-exception. */}
        <a href="/api/dsr/export" data-testid="dsr-export">
          {t(dict, 'settings.exportData')}
        </a>
        <a href="/api/dsr/delete" data-testid="dsr-delete">
          {t(dict, 'settings.deleteData')}
        </a>
      </section>
    </main>
  )
}
