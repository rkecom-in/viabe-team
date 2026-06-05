'use client'

import { useEffect, useState } from 'react'

/**
 * Founding-counter widget (VT-99) — the landing page's live count of remaining founding
 * spots. Honest framing (Pillar 7): real numbers only, urgency framing ONLY at >=90
 * claimed, no fake timers/counts. The cap is structurally real (VT-94 enforces it).
 *
 * SSR-seeded via `initial` (no hydration layout shift); re-fetches `/api/team/founding-
 * status` every 60s. On a slow/failed fetch it RETAINS the last-good value (no jank).
 * Purely informational — the actual founding assignment is the server-side atomic claim
 * at signup (VT-94); a user who races past slot 100 simply gets Standard.
 */
export interface FoundingStatus {
  remaining: number
  cap: number
  public_count: number
  all_claimed: boolean
}

export type FoundingDisplay =
  | { kind: 'available'; claimed: number; cap: number; remaining: number; almostFull: boolean }
  | { kind: 'full'; cap: number }
  | { kind: 'unknown' }

/** Pure display-state mapping — unit-tested without a DOM. Urgency only at >=90 claimed. */
export function foundingDisplayState(s: FoundingStatus | null): FoundingDisplay {
  if (s === null) return { kind: 'unknown' }
  if (s.all_claimed || s.remaining <= 0) return { kind: 'full', cap: s.cap }
  return {
    kind: 'available',
    claimed: s.public_count,
    cap: s.cap,
    remaining: s.remaining,
    almostFull: s.public_count >= 90,
  }
}

const _REFRESH_MS = 60_000

export function FoundingCounterWidget({
  initial = null,
}: {
  initial?: FoundingStatus | null
}) {
  const [status, setStatus] = useState<FoundingStatus | null>(initial)

  useEffect(() => {
    let alive = true
    async function poll(): Promise<void> {
      try {
        const res = await fetch('/api/team/founding-status', {
          signal: AbortSignal.timeout(5000),
        })
        if (!res.ok) return // retain last-good
        const data = (await res.json()) as FoundingStatus
        if (alive) setStatus(data)
      } catch {
        // network / timeout -> keep the existing value (no flicker)
      }
    }
    const id = setInterval(() => void poll(), _REFRESH_MS)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [])

  const d = foundingDisplayState(status)

  if (d.kind === 'unknown') {
    return (
      <div data-widget="founding-counter" data-state="loading">
        Loading availability…
      </div>
    )
  }
  if (d.kind === 'full') {
    // De-emphasize: no CTA — Standard becomes the default (honest: the tier really closed).
    return (
      <div data-widget="founding-counter" data-state="full">
        <p>All {d.cap} founding spots are claimed. Standard pricing applies.</p>
      </div>
    )
  }
  return (
    <div data-widget="founding-counter" data-state={d.almostFull ? 'almost-full' : 'available'}>
      <p data-count>
        {d.claimed} of {d.cap} founding spots claimed
        {d.almostFull ? ` — only ${d.remaining} left` : ''}
      </p>
      <div
        data-bar="founding"
        role="progressbar"
        aria-valuenow={d.claimed}
        aria-valuemin={0}
        aria-valuemax={d.cap}
      />
      <p>Founding pricing is locked in forever for these first {d.cap} customers.</p>
      <a data-cta="claim-founding" href="/team/signup?plan=founding">
        Claim a founding spot
      </a>
    </div>
  )
}
