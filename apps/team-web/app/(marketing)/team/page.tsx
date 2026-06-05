import { FoundingCounterWidget, type FoundingStatus } from './founding-counter-widget'

/**
 * Viabe Team landing page.
 *
 * Lives here permanently — product-specific marketing belongs to Team. Server-fetches
 * the founding-counter once at request time (VT-99) so the widget hydrates without a
 * layout shift; the client then re-fetches every 60s.
 */
export const dynamic = 'force-dynamic'

async function fetchFoundingStatus(): Promise<FoundingStatus | null> {
  const base = process.env.TEAM_ORCHESTRATOR_URL ?? 'http://localhost:8001'
  try {
    const res = await fetch(`${base}/api/team/founding-status`, { next: { revalidate: 60 } })
    if (!res.ok) return null
    return (await res.json()) as FoundingStatus
  } catch {
    return null // the widget degrades to "Loading availability…"
  }
}

export default async function TeamLandingPage() {
  const initial = await fetchFoundingStatus()
  return (
    <main>
      <h1>Viabe Team</h1>
      <FoundingCounterWidget initial={initial} />
    </main>
  )
}
