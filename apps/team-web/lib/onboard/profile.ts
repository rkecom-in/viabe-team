/**
 * VT-267 PR-C — owner wizard business_profile draft read + Review-&-Confirm save.
 *
 * The draft is the tenant's single L1 `business_profile` entity (mig 055), populated by the
 * apify_gbp/swiggy/zomato enrichment (Apify-key-gated — may be empty until provisioned) and the
 * onboarding writers. The wizard DISPLAYS it (read via serverSecretClient — l1_entities is
 * tenant-RLS, service-role reads it) and lets the owner EDIT the owner-facing identity fields,
 * saving through the orchestrator (which MERGEs via upsert_business_profile, preserving enrichment
 * siblings). CL-390: business identity, not customer PII.
 */

import { serverSecretClient } from '@/lib/supabase-client'

const _ORCHESTRATOR_DEFAULT = 'http://localhost:8001'
const _SAVE_TIMEOUT_MS = 10_000

/** The owner-editable identity fields (must match the orchestrator allowlist). */
export const WIZARD_EDITABLE_FIELDS = [
  'business_name',
  'business_type',
  'preferred_language',
  'owner_curated_context',
] as const

export type EditableField = (typeof WIZARD_EDITABLE_FIELDS)[number]

export interface ProfileDraft {
  /** True iff an L1 business_profile entity exists (else the draft is empty / not yet enriched). */
  exists: boolean
  business_name: string
  business_type: string
  preferred_language: string
  owner_curated_context: string
}

function _str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

/** Read the tenant's L1 business_profile draft for the wizard. Empty shape if none yet. */
export async function fetchProfileDraft(
  tenantId: string,
  client: { from: (t: string) => any } = serverSecretClient(),
): Promise<ProfileDraft> {
  const empty: ProfileDraft = {
    exists: false,
    business_name: '',
    business_type: '',
    preferred_language: '',
    owner_curated_context: '',
  }
  if (!tenantId) return empty
  const { data, error } = await client
    .from('l1_entities')
    .select('attributes')
    .eq('tenant_id', tenantId)
    .eq('entity_type', 'business_profile')
    .maybeSingle()
  if (error || !data) return empty
  const attrs = ((data as { attributes: Record<string, unknown> }).attributes ?? {}) as Record<
    string,
    unknown
  >
  return {
    exists: true,
    business_name: _str(attrs.business_name),
    business_type: _str(attrs.business_type),
    preferred_language: _str(attrs.preferred_language),
    owner_curated_context: _str(attrs.owner_curated_context),
  }
}

export interface SaveProfileResult {
  ok: boolean
  /** ok | http_<n> | timeout | error | no_changes | invalid_field */
  reason: string
}

/** Save owner-edited identity fields via the orchestrator (MERGE-not-clobber). Never throws.
 *  Only the editable allowlist is forwarded; unknown keys are dropped client-side too. */
export async function saveProfileEdits(
  tenantId: string,
  edits: Partial<Record<EditableField, string>>,
): Promise<SaveProfileResult> {
  if (!tenantId) return { ok: false, reason: 'invalid_field' }
  const attributes: Record<string, string> = {}
  for (const k of WIZARD_EDITABLE_FIELDS) {
    const v = edits[k]
    if (typeof v === 'string') attributes[k] = v
  }
  if (Object.keys(attributes).length === 0) return { ok: false, reason: 'no_changes' }

  const base = process.env.TEAM_ORCHESTRATOR_URL ?? _ORCHESTRATOR_DEFAULT
  const secret = process.env.INTERNAL_API_SECRET ?? ''
  try {
    const res = await fetch(`${base}/api/orchestrator/integrations/onboard/business-profile`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'X-Internal-Secret': secret },
      body: JSON.stringify({ tenant_id: tenantId, attributes }),
      signal: AbortSignal.timeout(_SAVE_TIMEOUT_MS),
    })
    if (!res.ok) return { ok: false, reason: `http_${res.status}` }
    return { ok: true, reason: 'ok' }
  } catch (err) {
    const timedOut = err instanceof Error && err.name === 'TimeoutError'
    return { ok: false, reason: timedOut ? 'timeout' : 'error' }
  }
}
