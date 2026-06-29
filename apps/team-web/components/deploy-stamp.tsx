/**
 * VT-508 — deployed-version stamp (server component).
 *
 * Renders a fixed bottom-left instrument:
 *   web <sha7> · HH:MM
 *   api <sha7> · HH:MM
 *
 * Web SHA + build time come from NEXT_PUBLIC_BUILD_SHA / NEXT_PUBLIC_BUILD_TIME (baked into the
 * bundle at Vercel build time via next.config). Orchestrator SHA + boot time are fetched
 * server-side at render time from GET /api/orchestrator/version (internal URL, no proxy needed).
 * Degrades gracefully: if the orchestrator is unreachable the api line shows `? · ?`.
 *
 * No secrets exposed — a git SHA is not a secret. pointer-events:none so it never intercepts
 * clicks. aria-hidden so screen readers skip it.
 */

import React from 'react'

async function fetchOrchestratorVersion(): Promise<{
  git_sha: string
  booted_at: string
} | null> {
  const base = process.env.TEAM_ORCHESTRATOR_URL ?? 'http://localhost:8001'
  try {
    const res = await fetch(`${base}/api/orchestrator/version`, {
      cache: 'no-store',
      // Tight deadline — the stamp is informational; never slow a page for it.
      signal: AbortSignal.timeout(2000),
    })
    if (!res.ok) return null
    return (await res.json()) as { git_sha: string; booted_at: string }
  } catch {
    return null
  }
}

/** Format an ISO string as HH:MM (UTC). Returns '--:--' on any parse failure. */
function fmtTime(iso: string | undefined): string {
  if (!iso) return '--:--'
  try {
    const d = new Date(iso)
    return d.toLocaleTimeString('en-GB', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone: 'UTC',
    })
  } catch {
    return '--:--'
  }
}

export async function DeployStamp() {
  const webSha = (process.env.NEXT_PUBLIC_BUILD_SHA ?? 'dev').slice(0, 7)
  const webTime = fmtTime(process.env.NEXT_PUBLIC_BUILD_TIME)

  const api = await fetchOrchestratorVersion()
  const apiSha = api?.git_sha ? api.git_sha.slice(0, 7) : '?'
  const apiTime = fmtTime(api?.booted_at)

  return (
    <div
      style={{
        position: 'fixed',
        bottom: '6px',
        left: '8px',
        fontSize: '10px',
        fontFamily: 'ui-monospace, monospace',
        opacity: 0.28,
        color: 'currentColor',
        pointerEvents: 'none',
        lineHeight: '1.5',
        zIndex: 9999,
        userSelect: 'none',
        whiteSpace: 'nowrap',
      }}
      aria-hidden="true"
    >
      web {webSha} · {webTime}
      <br />
      api {apiSha} · {apiTime}
    </div>
  )
}
