/**
 * VT-290 — Ops Console V2 operator roles.
 *
 * Two roles (CL-426): VTR sees ONLY assigned businesses; VTAdmin sees all + controls
 * assignment. Phase-1 bootstrap: Fazal (FAZAL_OWNER_UUID) is the first VTAdmin (seeded
 * idempotently in operator_allowlist; full provisioning UI is VT-295). The role is
 * resolved server-side from the allowlist row's `role` column (defaulting to VTR for a
 * granted operator without an explicit role).
 *
 * Phase-1 keeps it simple: no JWT role-claim re-issuance (the existing operator JWT is
 * unchanged); role is a server-side lookup, so a role change takes effect within the
 * 30s allowlist cache rather than on JWT expiry.
 */

export enum OperatorRole {
  VTR = 'vt_r',
  VTADMIN = 'vt_admin',
}

/** Resolve a role string (from the allowlist) to the enum; unknown/empty → VTR (least
 *  privilege, fail-closed). Fazal's UUID is always VTAdmin (break-glass parity with
 *  operator-allowlist). */
export function resolveRole(roleValue: string | null | undefined, opts?: { isFazal?: boolean }): OperatorRole {
  if (opts?.isFazal) return OperatorRole.VTADMIN
  return roleValue === OperatorRole.VTADMIN ? OperatorRole.VTADMIN : OperatorRole.VTR
}

export function isVtAdmin(role: OperatorRole): boolean {
  return role === OperatorRole.VTADMIN
}
