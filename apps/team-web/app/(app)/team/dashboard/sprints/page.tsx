import { ComingSoon } from '@/components/dashboard/ui'
import { getDictionary, resolveLocale, t } from '@/lib/i18n'

/**
 * Owner dashboard — Sprints (Phase 1, not yet built). VT-372: a clean, labelled, STYLED
 * empty-state with intent (not a bare h1). Bilingual via ?lang.
 */
export default async function DashboardSprintsPage({
  searchParams,
}: {
  searchParams: Promise<{ lang?: string }>
}) {
  const dict = getDictionary(resolveLocale((await searchParams).lang))
  return (
    <div data-testid="dashboard-sprints">
      <ComingSoon
        title={t(dict, 'stub.sprints.title')}
        headline={t(dict, 'stub.sprints.headline')}
        body={t(dict, 'stub.sprints.body')}
        badge={t(dict, 'stub.comingSoon')}
      />
    </div>
  )
}
