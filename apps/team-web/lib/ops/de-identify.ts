/**
 * VT-290 — PII de-identification for the VTR view (CL-426).
 *
 * A VTR sees OPERATIONAL data, not raw customer PII. Escalation/queue rows shown to a
 * VTR are masked at the data-access boundary: customer name / phone / email / account id
 * are dropped and replaced by a stable, non-PII reference derived from the row's own id
 * (e.g. `REF#a4f9d2`). VTAdmin sees full detail. Actual PII is reachable only via the
 * audited [Resolve] path (VT-188), never silently.
 */

export interface OpsRow {
  id: string
  tenant_id: string
  tenant_name?: string | null
  customer_name?: string | null
  phone?: string | null
  email?: string | null
  account_id?: string | null
  severity?: string | null
  kind?: string | null
  time?: string | null
  status?: string | null
  [k: string]: unknown
}

export interface MaskedOpsRow {
  id: string
  tenant_id: string
  tenant_name: string | null
  reference: string
  severity: string | null
  kind: string | null
  time: string | null
  status: string | null
}

/** Stable non-PII reference from the row id (no customer data). */
export function referenceFor(id: string): string {
  return `REF#${(id || '').replace(/-/g, '').slice(0, 6) || 'unknown'}`
}

/** Mask a row for the VTR view: strip all PII, keep operational fields + a reference. */
export function maskForVtr(row: OpsRow): MaskedOpsRow {
  return {
    id: row.id,
    tenant_id: row.tenant_id,
    tenant_name: row.tenant_name ?? null,
    reference: referenceFor(row.id),
    severity: row.severity ?? null,
    kind: row.kind ?? null,
    time: row.time ?? null,
    status: row.status ?? null,
  }
}

/** Returns true if an object still carries any PII field (guard for tests/assertions). */
export function hasPii(row: Record<string, unknown>): boolean {
  return ['customer_name', 'phone', 'email', 'account_id'].some(
    (k) => row[k] != null && row[k] !== '',
  )
}
