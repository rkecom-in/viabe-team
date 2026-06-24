import { Card, CardTitle, LoadError, PageHeader } from '@/components/dashboard/ui'
import { requireOwnerSession } from '@/lib/auth/require-owner-session'
import { getDictionary, resolveLocale, t } from '@/lib/i18n'
import { fetchSettings } from '@/lib/owner-dashboard-client'

/**
 * Owner dashboard — Settings (VT-338 + VT-372 styling). Read-only: business profile +
 * plan/trial + the DSR-init button (the ONLY write-exception — owner-initiated requests to
 * /api/dsr/*, not edits). tenantId is session-derived server-side (never a client field). No
 * customer PII.
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
      <div data-testid="dashboard-settings">
        <LoadError title={t(dict, 'settings.title')} message={t(dict, 'common.loadError')} />
      </div>
    )
  }

  const b = data.business
  const p = data.plan

  return (
    <div data-testid="dashboard-settings">
      <PageHeader title={t(dict, 'settings.title')} />

      <div className="grid gap-6 lg:grid-cols-2">
        <Card label="business">
          <CardTitle>{t(dict, 'settings.business')}</CardTitle>
          <dl className="mt-4 divide-y divide-gray-100 text-sm">
            <Row label={t(dict, 'settings.businessName')} value={b?.business_name} />
            <Row label={t(dict, 'settings.ownerName')} value={b?.owner_name} />
            <Row label={t(dict, 'settings.archetype')} value={b?.business_archetype} />
            <Row label={t(dict, 'settings.hours')} value={b?.working_hours} />
          </dl>
        </Card>

        <Card label="plan">
          <CardTitle>{t(dict, 'settings.plan')}</CardTitle>
          <dl className="mt-4 divide-y divide-gray-100 text-sm">
            <Row label={t(dict, 'settings.planTier')} value={p.plan_tier} />
            <Row
              label={t(dict, 'settings.trialEnds')}
              value={p.trial_ends_at ? new Date(p.trial_ends_at).toLocaleDateString('en-IN') : null}
            />
          </dl>
        </Card>
      </div>

      <Card label="privacy" className="mt-6">
        <CardTitle>{t(dict, 'settings.privacy')}</CardTitle>
        {/* VT-341: EXPORT is self-serve (non-destructive, the owner's own PII-scrubbed data)
            via a POST form — a GET <a> could be prefetch/crawler-triggered. DELETE is NOT a
            self-serve control at launch (Fazal ruling 2026-06-06: an instant irreversible
            purge from a button is too hot); the owner contacts us + Fazal/ops runs the
            DSR-delete out-of-band. The request+grace self-serve model is VT-344 (post-launch). */}
        <form method="POST" action="/api/dsr/export" className="mt-4">
          <button
            type="submit"
            data-testid="dsr-export"
            className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-semibold text-gray-800 transition hover:bg-gray-50"
          >
            {t(dict, 'settings.exportData')}
          </button>
        </form>
        <p data-testid="dsr-delete-note" className="mt-3 text-sm text-gray-500">
          {t(dict, 'settings.deleteContact')}
        </p>
      </Card>
    </div>
  )
}

/** A label/value row inside a settings card; falls back to an em-dash when empty. */
function Row({ label, value }: { label: string; value?: string | null }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2.5">
      <dt className="text-gray-500">{label}</dt>
      <dd className="text-right font-medium text-gray-900">{value ?? '—'}</dd>
    </div>
  )
}
