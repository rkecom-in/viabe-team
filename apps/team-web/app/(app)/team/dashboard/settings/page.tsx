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
        {/* VT-341: EXPORT is self-serve (non-destructive, the owner's own PII-scrubbed data)
            via a POST form — a GET <a> could be prefetch/crawler-triggered. DELETE is NOT a
            self-serve control at launch (Fazal ruling 2026-06-06: an instant irreversible
            purge from a button is too hot); the owner contacts us + Fazal/ops runs the
            DSR-delete out-of-band. The request+grace self-serve model is VT-344 (post-launch). */}
        <form method="POST" action="/api/dsr/export">
          <button type="submit" data-testid="dsr-export">
            {t(dict, 'settings.exportData')}
          </button>
        </form>
        <p data-testid="dsr-delete-note">{t(dict, 'settings.deleteContact')}</p>
      </section>
    </main>
  )
}
