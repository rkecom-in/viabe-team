import type { ReactNode } from 'react'

/**
 * Ops UI shell — Fazal-only.
 *
 * Phase 1 scaffold: layout only. Access control lands in a later ticket.
 */
export default function OpsLayout({ children }: { children: ReactNode }) {
  return <section data-area="team-ops">{children}</section>
}
