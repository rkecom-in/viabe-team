import type { ReactNode } from 'react'

import Link from 'next/link'
import { redirect } from 'next/navigation'

import { DashboardNav, type NavItem } from '@/components/dashboard/dashboard-nav'
import { OwnerUnauthorizedError, requireOwnerSession } from '@/lib/auth/require-owner-session'
import { getDictionary, resolveLocale, t } from '@/lib/i18n'

/**
 * Owner portal shell (read-only). VT-87: AUTH-GATED — every /team/dashboard/* page requires
 * a valid owner session (the VT-250 `viabe_team_session` cookie). On a missing or invalid
 * session we redirect to the OTP login; no dashboard data is fetched unauth.
 *
 * VT-372: styled to the marketing-surface bar — a branded shell with a real sub-page nav
 * (sidebar on desktop, scroll-tabs on mobile) + the language toggle. The nav was previously
 * absent (the layout rendered ONLY the locale toggle, with no links to the sub-pages).
 */
export default async function DashboardLayout({ children }: { children: ReactNode }) {
  try {
    await requireOwnerSession()
  } catch (err) {
    if (err instanceof OwnerUnauthorizedError) redirect('/team/login?next=/team/dashboard')
    throw err
  }

  // Nav labels render in the default locale (en) — the client nav preserves the per-page
  // ?lang override on every link, and each page itself renders its content in the active locale.
  const dict = getDictionary(resolveLocale(null))
  const navItems: NavItem[] = [
    { href: '/team/dashboard', label: t(dict, 'nav.overview') },
    { href: '/team/dashboard/campaigns', label: t(dict, 'nav.campaigns') },
    { href: '/team/dashboard/customers', label: t(dict, 'nav.customers') },
    { href: '/team/dashboard/reports', label: t(dict, 'nav.reports') },
    { href: '/team/dashboard/settings', label: t(dict, 'nav.settings') },
    { href: '/team/dashboard/launch', label: t(dict, 'nav.launch') },
    { href: '/team/dashboard/sessions', label: t(dict, 'nav.sessions') },
    { href: '/team/dashboard/sprints', label: t(dict, 'nav.sprints') },
  ]

  return (
    <div data-area="team-dashboard" className="min-h-screen bg-gray-50 text-gray-900 antialiased">
      <header className="border-b border-gray-200 bg-white">
        <div className="mx-auto flex w-full max-w-6xl items-center justify-between px-4 py-4 sm:px-6">
          <Link
            href="/team/dashboard"
            className="text-lg font-bold tracking-tight text-emerald-700"
          >
            {t(dict, 'brand')}
          </Link>
          {/* VT-338: locale toggle — relative ?lang query preserves the current page; each
              page resolves the active locale from ?lang (override) + the tenant default. */}
          <nav
            aria-label="language"
            data-testid="locale-toggle"
            className="text-sm text-gray-500"
          >
            <Link href="?lang=en" className="rounded px-2 py-1 hover:bg-gray-100 hover:text-gray-900">
              English
            </Link>
            <span aria-hidden className="text-gray-300">
              |
            </span>
            <Link href="?lang=hi" className="rounded px-2 py-1 hover:bg-gray-100 hover:text-gray-900">
              हिंदी
            </Link>
          </nav>
        </div>
      </header>

      <div className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 sm:py-8">
        <div className="gap-8 sm:flex">
          <aside className="mb-6 sm:mb-0 sm:w-52 sm:shrink-0">
            <DashboardNav items={navItems} ariaLabel={t(dict, 'nav.menu')} />
          </aside>
          <main className="min-w-0 flex-1">{children}</main>
        </div>
      </div>
    </div>
  )
}
