import { ComingSoon } from '@/components/dashboard/ui'
import { getDictionary, resolveLocale, t } from '@/lib/i18n'

/**
 * Owner dashboard — Launch tracker (Phase 1, not yet built). VT-372: a clean, labelled,
 * STYLED empty-state with intent (not a bare h1). Bilingual via ?lang.
 */
export default async function DashboardLaunchPage({
  searchParams,
}: {
  searchParams: Promise<{ lang?: string }>
}) {
  const dict = getDictionary(resolveLocale((await searchParams).lang))
  return (
    <div data-testid="dashboard-launch">
      <ComingSoon
        title={t(dict, 'stub.launch.title')}
        headline={t(dict, 'stub.launch.headline')}
        body={t(dict, 'stub.launch.body')}
        badge={t(dict, 'stub.comingSoon')}
      />
    </div>
  )
}
