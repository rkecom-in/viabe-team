/**
 * VT-290 — Ops Console V2 auth gate (extends VT-123's requireFazal).
 *
 * Adds the V2 role + assignment scoping on top of the existing operator-JWT +
 * allowlist gate:
 *   - role: Fazal (FAZAL_OWNER_UUID) → VTAdmin (Phase-1 bootstrap, CL-426); any other
 *     allowlisted operator → VTR (least privilege). Full role provisioning = VT-295.
 *   - assignedTenants: null for VTAdmin (unscoped); the VTR's active assigned set
 *     otherwise (fail-closed empty if none).
 *
 * requireFazal stays the single JWT+allowlist verifier; this wraps it so the existing
 * single-operator path is unchanged and the role/scoping is additive.
 */

import { requireFazal } from './require-fazal'
import { OperatorRole, resolveRole } from './roles'
import { resolveAssignedTenants } from '@/lib/ops/assignments'

const FAZAL_UUID = (process.env.FAZAL_OWNER_UUID ?? '').trim()

export interface OpsOperator {
  operatorId: string
  role: OperatorRole
  /** null = VTAdmin (all tenants); array = the VTR's active assigned tenant_ids. */
  assignedTenants: string[] | null
}

export async function requireOpsOperator(): Promise<OpsOperator> {
  const { fazalUuid: operatorId } = await requireFazal()
  const role = resolveRole(undefined, { isFazal: !!FAZAL_UUID && operatorId === FAZAL_UUID })
  const assignedTenants = await resolveAssignedTenants(operatorId, role)
  return { operatorId, role, assignedTenants }
}
