/**
 * VT-290 / VT-360 — VTR view row shapes (CL-426).
 *
 * The VTR sees OPERATIONAL data, not raw customer PII. As of VT-360 the de-identification is
 * DB-ENFORCED, not app-side: the VTR ops surface reads the VT-281/360 de-identified views through
 * the orchestrator as `app_vtr_role` (NO grant on raw tables / decrypt), so PII is unreachable, not
 * merely masked. The old app-side `maskForVtr` / `referenceFor` / `hasPii` helpers were RETIRED in
 * VT-360 — these are now just the row-shape types the surface renders. VTAdmin (operator
 * full-access) keeps the service-role read; actual PII is reachable only via the audited [Resolve]
 * path (VT-188), never silently.
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

// maskForVtr / referenceFor / hasPii were RETIRED in VT-360 — the VTR surface now reads
// DB-de-identified views through the orchestrator (app_vtr_role), so there is no app-side masking
// or raw-id REF# left to generate. The types above are the rendered row shapes; `reference` is the
// operational row handle the orchestrator returns (the escalation/alert id — not a person id).
