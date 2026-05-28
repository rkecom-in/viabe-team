/**
 * VT-211 — server-side data access for /team/onboard.
 *
 * Reads tenant_integration_state (migration 031) for the configured
 * Fazal tenant. Per Cowork correction 1 (review-verdict 2026-05-28
 * 11:05 IST): tenant resolution comes from a new env var
 * FAZAL_TENANT_ID (no FK between tenants and operator UUIDs in
 * Phase 1; that's Phase-2 multi-tenant operator work).
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
    super('FAZAL_TENANT_ID env not configured')
    this.name = 'TenantNotConfiguredError'
  }
}

export async function fetchFazalOnboardState(): Promise<OnboardState> {
  const tenantId = process.env.FAZAL_TENANT_ID ?? ''
  if (!tenantId) throw new TenantNotConfiguredError()
  const client = serverSecretClient()
  const { data, error } = await client
    .from('tenant_integration_state')
    .select('tenant_id, phase, pending_owner_input, last_decision')
    .eq('tenant_id', tenantId)
    .maybeSingle()
  if (error) throw new Error(`fetchFazalOnboardState: ${error.message}`)
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
