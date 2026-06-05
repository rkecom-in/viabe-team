import type { ReactNode } from 'react'

import { redirect } from 'next/navigation'

import { OwnerUnauthorizedError, requireOwnerSession } from '@/lib/auth/require-owner-session'

/**
 * Owner portal shell (read-only). VT-87: AUTH-GATED — every /team/dashboard/* page
 * requires a valid owner session (the VT-250 `viabe_team_session` cookie). On a missing
 * or invalid session we redirect to the OTP login; no dashboard data is fetched unauth.
 */
export default async function DashboardLayout({ children }: { children: ReactNode }) {
  try {
    await requireOwnerSession()
  } catch (err) {
    if (err instanceof OwnerUnauthorizedError) redirect('/team/login?next=/team/dashboard')
    throw err
  }
  return (
    <section data-area="team-dashboard">
      {/* VT-338: locale toggle — relative ?lang query preserves the current page; each
          page resolves the active locale from ?lang (override) + the tenant default. */}
      <nav aria-label="language" data-testid="locale-toggle">
        <a href="?lang=en">English</a> <a href="?lang=hi">हिंदी</a>
      </nav>
      {children}
    </section>
  )
}
