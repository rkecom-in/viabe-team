/**
 * VT-233 — shared next-param open-redirect allowlist (extracted from
 * VT-230 callback). Used by:
 *   - app/api/ops/login/callback/route.ts (server-side)
 *   - app/api/ops/login/finalize-hash/route.ts (server-side, client-driven)
 *
 * Allowed paths: /team/ops, /team/ops/stream, /team/ops/stream/history,
 * /team/onboard, /team/dashboard. Anything else (external URL, traversal,
 * protocol-relative `//`) → ignored, caller defaults to /team/ops.
 */

const NEXT_ALLOWLIST = [
  '/team/ops',
  '/team/ops/stream',
  '/team/ops/stream/history',
  '/team/onboard',
  '/team/dashboard',
]

const DEFAULT_NEXT = '/team/ops'

export function safeNext(input: string | null | undefined): string {
  if (!input) return DEFAULT_NEXT
  if (
    !input.startsWith('/team/') ||
    input.includes('//') ||
    input.includes('..')
  ) {
    return DEFAULT_NEXT
  }
  const matched = NEXT_ALLOWLIST.some(
    (p) =>
      input === p ||
      input.startsWith(`${p}/`) ||
      input.startsWith(`${p}?`),
  )
  return matched ? input : DEFAULT_NEXT
}
