/**
 * VT-97 — the build-time launch-mode toggle. `NEXT_PUBLIC_TEAM_LAUNCH_MODE` picks ONE rendering
 * tree (Pillar 8 — one toggle, not per-section conditionals): `waitlist` (capture interest, no
 * trial), `live` (the full signup flow), `maintenance` ("back soon", no form).
 *
 * Default = `waitlist` (the honest pre-launch state). Read this server-side only — a runtime
 * read in client code causes a hydration mismatch (the value is fixed at build).
 *
 * CL-422: real waitlist PII collection (flipping to a live-collecting `waitlist`) is gated on
 * VT-231 (Mumbai prod) + Fazal, exactly like ENABLE_PUBLIC_SIGNUP. See the launch-mode runbook.
 */
export type LaunchMode = 'waitlist' | 'live' | 'maintenance'

export function launchMode(): LaunchMode {
  const m = process.env.NEXT_PUBLIC_TEAM_LAUNCH_MODE
  return m === 'live' || m === 'maintenance' ? m : 'waitlist'
}
