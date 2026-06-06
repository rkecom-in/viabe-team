/** VT-100 — cookie-free A/B experiment assignment for the landing.
 *
 * NO cookie, NO localStorage, NO tracking ID: the variant IS a deterministic hash of
 * (truncated visitor IP + user-agent + experimentId). Same visitor + same experiment → same
 * variant; different experiment → independent. The IP is truncated (/24 v4, /48 v6) BEFORE
 * hashing to reduce identifiability and is NEVER stored — only consumed to compute the hash
 * (Pillar 7: no covert tracking, no PII at rest). One experiment system (Pillar 8).
 */
import { createHash } from 'crypto'
import { headers } from 'next/headers'

import experimentConfig from '@/config/experiments.json'

export interface Experiment {
  id: string
  name: string
  variants: string[]
  traffic_split: number[]
  active: boolean
  start_at: string | null
  end_at: string | null
}

/** IPv4 → /24 (zero the last octet); IPv6 → /48 (first 3 hextets). Preserves consistency for
 * most visitors while dropping host-level identifiability. Unknown/garbage → returned as-is. */
export function truncateIp(ip: string): string {
  if (ip.includes(':')) return ip.split(':').slice(0, 3).join(':') // IPv6 /48
  const p = ip.split('.')
  return p.length === 4 ? `${p[0]}.${p[1]}.${p[2]}.0` : ip
}

/** Deterministic, pure variant pick: SHA-256(truncatedIp|ua|experimentId) → uint32 → mod. Equal
 * split across variants (Phase 1; weighted traffic_split is a post-launch extension). */
export function assignVariant(
  experimentId: string,
  ip: string,
  userAgent: string,
  variants: string[],
): string {
  if (variants.length === 0) throw new Error('experiments: no variants to assign')
  const digest = createHash('sha256')
    .update(`${truncateIp(ip)}|${userAgent}|${experimentId}`)
    .digest()
  const chosen = variants[digest.readUInt32BE(0) % variants.length]
  if (chosen === undefined) throw new Error('experiments: variant index out of range')
  return chosen
}

export function findExperiment(experimentId: string): Experiment | undefined {
  return (experimentConfig.experiments as Experiment[]).find((e) => e.id === experimentId)
}

/** Server-side: the visitor's assigned variant for an experiment. An inactive/missing experiment
 * returns the CONTROL (first variant, or 'control') — no assignment computed when nothing runs. */
export async function getExperiment(experimentId: string): Promise<string> {
  const exp = findExperiment(experimentId)
  if (!exp || !exp.active || exp.variants.length === 0) {
    return exp?.variants[0] ?? 'control'
  }
  const h = await headers()
  const ip =
    h.get('x-forwarded-for')?.split(',')[0]?.trim() ?? h.get('x-real-ip')?.trim() ?? 'unknown'
  const ua = h.get('user-agent') ?? 'unknown'
  return assignVariant(experimentId, ip, ua, exp.variants)
}
