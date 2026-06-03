/**
 * VT-297 — Telegram inbound identity binding (THE security boundary / IDOR-crux).
 *
 * Every inbound Telegram update carries an attacker-controllable `message.from.id`. This module
 * is the ONLY thing that turns that id into an authenticated VTR, fail-CLOSED at every step:
 *
 *   telegram_user_id → (verified operator_telegram row) → operator_id
 *                    → (uncached, TTL=0 revocation gate on operator_allowlist)
 *                    → role (Fazal-UUID → VTAdmin, else VTR)
 *                    → assigned-tenant set (resolveAssignedTenants)
 *
 * No verified binding → null. Revoked operator → null. Any error → null. The resolved
 * OpsOperator (NOT any chat-supplied field) is what every command/action scopes against.
 *
 * TTL=0: unlike the web `isOperator` (30s cache), the bot re-checks revocation on EVERY inbound
 * message (Cowork ruling) — a revoked VTR loses bot access immediately. operator_telegram +
 * operator_allowlist are deny-all RLS → serverSecretClient (service-role); scoping is app-side.
 */

import { serverSecretClient } from '@/lib/supabase-client'

import { OperatorRole, resolveRole } from '@/lib/auth/roles'
import { resolveAssignedTenants } from '@/lib/ops/assignments'

const FAZAL_UUID = (process.env.FAZAL_OWNER_UUID ?? '').trim()

type Client = { from: (t: string) => any }

export interface TelegramOperator {
  operatorId: string
  role: OperatorRole
  /** null = VTAdmin (all tenants); array = the VTR's active assigned tenant_ids. */
  assignedTenants: string[] | null
}

/** Uncached revocation check (TTL=0). Fazal break-glass always passes. */
async function _isActiveOperator(operatorId: string, client: Client): Promise<boolean> {
  if (!operatorId) return false
  if (FAZAL_UUID && operatorId === FAZAL_UUID) return true
  try {
    const { data, error } = await client
      .from('operator_allowlist')
      .select('user_id')
      .eq('user_id', operatorId)
      .is('revoked_at', null)
      .maybeSingle()
    if (error) return false // fail-closed
    return data != null
  } catch {
    return false
  }
}

/**
 * Resolve an inbound Telegram user_id to a verified, active VTR with their tenant scope.
 * Returns null (fail-closed) on: no verified binding, revoked operator, or any error.
 */
export async function resolveOperatorFromTelegram(
  telegramUserId: number | string,
  client: Client = serverSecretClient(),
): Promise<TelegramOperator | null> {
  if (telegramUserId === undefined || telegramUserId === null || telegramUserId === '') return null
  try {
    // 1. Binding: a VERIFIED operator_telegram row for this telegram_user_id.
    const { data, error } = await client
      .from('operator_telegram')
      .select('operator_id, verified_at')
      .eq('telegram_user_id', telegramUserId)
      .not('verified_at', 'is', null)
      .maybeSingle()
    if (error || !data) return null
    const row = data as { operator_id: string; verified_at: string | null }
    if (!row.operator_id || !row.verified_at) return null // belt-and-braces

    // 2. Revocation gate (uncached, TTL=0).
    if (!(await _isActiveOperator(row.operator_id, client))) return null

    // 3. Role (Fazal-UUID → VTAdmin; else VTR). operator_allowlist has no role column, so
    //    isFazal is derived from the UUID — NOT a stored field.
    const role = resolveRole(undefined, { isFazal: !!FAZAL_UUID && row.operator_id === FAZAL_UUID })

    // 4. Tenant scope (VTAdmin → null/all; VTR → assigned set, fail-closed []).
    const assignedTenants = await resolveAssignedTenants(row.operator_id, role, client as never)

    return { operatorId: row.operator_id, role, assignedTenants }
  } catch {
    return null
  }
}
