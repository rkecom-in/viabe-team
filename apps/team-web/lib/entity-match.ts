/**
 * VT-406 (Part B) — the signup entity-match flow, extracted from the wizard component so the
 * decision logic (candidate fetch, confirm sequence, the verified/reject/retry classification, and
 * the create-account gate) is unit-testable in the node test env (no jsdom — the repo's pattern,
 * mirroring lib/signup-otp.ts). The component maps these results to bilingual copy + sub-step
 * transitions; this module owns the fetch sequence + the outcome classifier ONLY.
 *
 * Provenance discipline (Fazal 2026-06-23, VT-406): candidates from web/GBP are "found", UNCONFIRMED.
 * Only a Sandbox-confirmed entity is "verified". This module NEVER classifies a candidate as verified
 * — `verified` is reachable ONLY through a confirm that returns status === 'gstin_verified'.
 *
 * No-enumeration-oracle (Fazal): a rejected confirm (invalid_gstin / invalid_gstin_format) and a
 * "none of these" pick both collapse to the SAME `reject` outcome with no reason carried forward —
 * the UI shows one generic "GST-registered businesses only" copy, never an inactive-vs-not-found tell.
 *
 * CL-390: no business identity (name / gstin / city) is logged here — values flow straight to the
 * server-side proxy routes (which forward to the orchestrator under X-Internal-Secret).
 */

import type {
  EntityCandidate,
  EntityCandidatesResult,
  EntityConfirmResult,
} from '@/lib/orchestrator-client'

type Fetch = typeof fetch

export type { EntityCandidate, EntityCandidatesResult, EntityConfirmResult }

/**
 * The terminal classification of a confirm attempt. `verified` is the ONLY outcome that may unlock
 * account-creation; `reject` is the graceful "GST-registered only" terminus (also the not-listed
 * pick); `retry` is a transient vendor failure (NOT a reject — the owner retries).
 */
export type ConfirmOutcome =
  | { kind: 'verified'; gstin: string; name: string | null }
  | { kind: 'reject' } // none-of-these OR invalid_gstin[_format] — generic, no enumeration oracle
  | { kind: 'retry' } // vendor_down / timeout / transport — "on our side, try again"

/** The full wizard sub-step state machine. The component renders one screen per `step`. */
export type WizardStep =
  | 'idle' // name+city entered, not yet looked up
  | 'picking' // candidates rendered; owner choosing (or "none of these")
  | 'verified' // a gstin_verified confirm landed — create-account is now unlocked
  | 'reject' // graceful terminus — not a GST-registered business
  | 'retry' // transient vendor failure on confirm — show retry affordance

/** The verified entity carried into the create payload (the orchestrator anchors it at create). */
export interface VerifiedEntity {
  gstin: string
  /** The AUTHORITATIVE registry name (Sandbox), never the candidate's web/LLM trade name. */
  name: string | null
}

/**
 * Step 1 — fetch UNVERIFIED candidates via the server-side proxy route. The route holds the
 * INTERNAL_API_SECRET and forwards to the orchestrator; the browser only ever talks to /api/team.
 * Fail-CLOSED to an empty list (the not-listed path always exists — lookup never blocks signup).
 */
export async function fetchCandidates(
  businessName: string,
  city: string,
  f: Fetch = fetch,
): Promise<EntityCandidatesResult> {
  try {
    const res = await f('/api/team/onboard/entity-candidates', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ business_name: businessName, city }),
    })
    if (!res.ok) return { ok: false, candidates: [], reason: `http_${res.status}` }
    const data = (await res.json().catch(() => ({}))) as { candidates?: EntityCandidate[] }
    return { ok: true, candidates: data.candidates ?? [], reason: 'ok' }
  } catch {
    return { ok: false, candidates: [], reason: 'error' }
  }
}

/**
 * Step 2 — confirm the picked candidate's GSTIN via the server-side proxy route. Returns the verify
 * envelope verbatim (the classifier below turns it into an outcome). Fail-CLOSED on transport: any
 * throw → {ok:false, reason:'error'} (the classifier maps that to `retry`, never a false verified).
 */
export async function confirmCandidate(
  gstin: string,
  f: Fetch = fetch,
): Promise<EntityConfirmResult> {
  try {
    const res = await f('/api/team/onboard/entity-confirm', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ gstin }),
    })
    if (!res.ok) return { ok: false, reason: `http_${res.status}` }
    const data = (await res.json().catch(() => ({}))) as {
      ok?: boolean
      status?: string
      reason?: string
      name?: string | null
    }
    return {
      ok: Boolean(data.ok),
      status: data.status ?? undefined,
      reason: data.reason ?? undefined,
      name: data.name ?? null,
    }
  } catch {
    return { ok: false, reason: 'error' }
  }
}

/**
 * Classify a confirm envelope into a terminal outcome. The ONLY path to `verified` is
 * status === 'gstin_verified'. A `vendor_down` (or any transport failure: timeout / error /
 * http_5xx) is `retry`, NOT a reject — a vendor being down is not the owner's fault and must not
 * read as "you're not GST-registered". Everything else (invalid_gstin, invalid_gstin_format, any
 * non-verified status) collapses to a generic `reject` — NO reason is carried forward (no
 * inactive-vs-not-found enumeration oracle).
 *
 * `pickedGstin` is the GSTIN the owner picked (threaded from the candidate) — the verified entity is
 * anchored to the GSTIN we SENT, never one parsed back from the vendor body (defence-in-depth: the
 * confirm body's `gstin` echo is advisory, the picked value is authoritative).
 */
export function classifyConfirm(r: EntityConfirmResult, pickedGstin: string): ConfirmOutcome {
  if (r.ok && r.status === 'gstin_verified') {
    return { kind: 'verified', gstin: pickedGstin, name: r.name ?? null }
  }
  // Retryable transient failures — vendor down / network — never a reject.
  const retryReasons = new Set(['vendor_down', 'timeout', 'error'])
  if (r.reason && (retryReasons.has(r.reason) || /^http_5\d\d$/.test(r.reason))) {
    return { kind: 'retry' }
  }
  // invalid_gstin / invalid_gstin_format / any other non-verified result → generic reject.
  return { kind: 'reject' }
}

/**
 * The create-account gate (the VT-408 UI invariant): account-creation is offered ONLY when a
 * server-recorded verified entity is held. A candidate being "found" (web/GBP) is NOT enough — the
 * gate is the verified entity, never the pick. Pure predicate so the component + the test agree.
 */
export function canCreateAccount(verified: VerifiedEntity | null): boolean {
  return verified != null && Boolean(verified.gstin)
}

/** True iff this candidate can be confirmed at all (a GBP candidate with no GSTIN cannot be verified —
 *  the only path forward for it is the not-listed/reject terminus, since verify needs a registry id). */
export function isConfirmable(c: EntityCandidate): boolean {
  return Boolean(c.candidate_gstin && c.candidate_gstin.trim())
}
