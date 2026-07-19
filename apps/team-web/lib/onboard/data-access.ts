/**
 * VT-211 — server-side data access for /team/onboard.
 *
 * Reads tenant_integration_state (migration 031) for a given tenant.
 *
 * VT-415 (owner-auth cutover): the tenant is now resolved SERVER-SIDE from
 * the owner session (`requireOwnerSession()` → `tenantId`) and passed in as
 * a parameter. We no longer read `FAZAL_TENANT_ID` here — that env shim was a
 * Phase-1 single-tenant stand-in and reading it under an owner session would
 * be a live cross-tenant data leak (every owner would hit Fazal's tenant).
 * The caller MUST pass the session-derived tenantId; never a client field
 * (IDOR — caught twice VT-293/294).
 */

import { serverSecretClient } from '@/lib/supabase-client'

export type OnboardPhase =
  | 'phase_1_discovery'
  | 'phase_2_auth'
  | 'phase_3_sample_pull'
  | 'phase_4_field_mapping'
  | 'phase_5_confirmed'

export interface OnboardState {
  tenant_id: string
  phase: OnboardPhase
  pending_owner_input: {
    prompt_text?: string
    awaiting?: string
    valid_responses?: string[] | null
    connector_id?: string | null
    walkthrough_url?: string | null
  } | null
  last_decision: Record<string, unknown> | null
}

export class TenantNotConfiguredError extends Error {
  constructor() {
    super('onboard state: no tenant on the owner session')
    this.name = 'TenantNotConfiguredError'
  }
}

/**
 * VT-415: takes the session-derived tenantId (resolved server-side from
 * `requireOwnerSession()`); never reads `FAZAL_TENANT_ID`, never a client field.
 */
export async function fetchOnboardState(tenantId: string): Promise<OnboardState> {
  if (!tenantId) throw new TenantNotConfiguredError()
  const client = serverSecretClient()
  const { data, error } = await client
    .from('tenant_integration_state')
    .select('tenant_id, phase, pending_owner_input, last_decision')
    .eq('tenant_id', tenantId)
    .maybeSingle()
  if (error) throw new Error(`fetchOnboardState: ${error.message}`)
  if (!data) {
    // No row yet — seed defaults so the page can render a starting prompt.
    return {
      tenant_id: tenantId,
      phase: 'phase_1_discovery',
      pending_owner_input: null,
      last_decision: null,
    }
  }
  return data as OnboardState
}
