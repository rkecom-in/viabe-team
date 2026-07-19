import { ComingSoon } from '@/components/dashboard/ui'
import { getDictionary, resolveLocale, t } from '@/lib/i18n'

/**
 * Owner dashboard — Sessions (Phase 1, not yet built). VT-372: a clean, labelled, STYLED
 * empty-state with intent (not a bare h1). Bilingual via ?lang.
 */
export default async function DashboardSessionsPage({
  searchParams,
}: {
  searchParams: Promise<{ lang?: string }>
}) {
  const dict = getDictionary(resolveLocale((await searchParams).lang))
  return (
    <div data-testid="dashboard-sessions">
      <ComingSoon
        title={t(dict, 'stub.sessions.title')}
        headline={t(dict, 'stub.sessions.headline')}
        body={t(dict, 'stub.sessions.body')}
        badge={t(dict, 'stub.comingSoon')}
      />
    </div>
  )
}
