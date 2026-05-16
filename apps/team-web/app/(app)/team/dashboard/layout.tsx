import type { ReactNode } from 'react'

/**
 * Owner portal shell (read-only).
 *
 * Phase 1 scaffold: layout only. Auth and navigation land in later tickets.
 */
export default function DashboardLayout({ children }: { children: ReactNode }) {
  return <section data-area="team-dashboard">{children}</section>
}
