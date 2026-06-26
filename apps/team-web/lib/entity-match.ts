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
  GstinsByPanResult,
} from '@/lib/orchestrator-client'

type Fetch = typeof fetch

export type { EntityCandidate, EntityCandidatesResult, EntityConfirmResult, GstinsByPanResult }

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
  | 'pan_entry' // VT-448 PRIMARY: owner enters their 10-char PAN; we IDENTIFY their GSTIN(s)
  | 'pan_pick' // VT-448 PRIMARY: the PAN's GSTIN(s) listed; owner taps one to verify
  | 'picking' // candidates rendered; owner choosing (or "none of these")
  | 'manual_gstin' // VT-448: owner enters their GSTIN directly (FALLBACK — "don't have your PAN?")
  | 'verified' // a gstin_verified confirm landed — create-account is now unlocked
  | 'reject' // graceful terminus — not a GST-registered business
  | 'retry' // transient vendor failure on confirm — show retry affordance

/** The verified entity carried into the create payload (the orchestrator anchors it at create). */
export interface VerifiedEntity {
  gstin: string
  /** The AUTHORITATIVE registry name (Sandbox), never the candidate's web/LLM trade name. */
  name: string | null
  /** VT-411 — the DISCOVERED public business number from the picked candidate (GBP only; null
   *  otherwise). The ownership step OTPs THIS number; null → the owner enters it on that step. */
  phone?: string | null
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

/** True iff this candidate can be confirmed via its own id. A GBP candidate with no GSTIN cannot be
 *  one-tap confirmed — but the owner can still verify it by entering their GSTIN (VT-448 manual path). */
export function isConfirmable(c: EntityCandidate): boolean {
  return Boolean(c.candidate_gstin && c.candidate_gstin.trim())
}

/**
 * VT-448 — client-side GSTIN FORMAT pre-check for the manual-entry path: 2 state digits + PAN
 * (5 letters + 4 digits + 1 letter) + 1 entity char + 'Z' + 1 checksum char = 15 chars. This is a
 * format gate ONLY (lets the owner fix a typo before we round-trip) — it is NOT verification; the
 * authoritative gate stays the Sandbox confirm (status === 'gstin_verified'). Mirrors the
 * orchestrator's _GSTIN_RE, anchored. Input is normalized upper + trimmed before the test.
 */
export function isValidGstinFormat(gstin: string): boolean {
  return /^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$/.test((gstin || '').trim().toUpperCase())
}

/**
 * VT-448 — client-side PAN FORMAT pre-check for the PRIMARY identify path: 5 letters + 4 digits +
 * 1 letter = 10 chars (the standard Indian PAN shape). This is a format gate ONLY (lets the owner
 * fix a typo before we round-trip the lookup) — it is NOT verification. Input is normalized upper +
 * trimmed before the test.
 */
export function isValidPanFormat(pan: string): boolean {
  return /^[A-Z]{5}\d{4}[A-Z]$/.test((pan || '').trim().toUpperCase())
}

/**
 * VT-448 — map a city to its GST state code (the first 2 digits of any GSTIN registered in that
 * state). Used to scope the PAN→GSTIN lookup. Case-insensitive, trimmed. Returns null for an unknown
 * city — the component then asks the owner for a small state hint rather than guessing a wrong code.
 * Deliberately small (the launch cities + their states); extend as coverage grows.
 */
const _CITY_TO_STATE_CODE: Record<string, string> = {
  // Maharashtra — 27
  mumbai: '27',
  pune: '27',
  nagpur: '27',
  nashik: '27',
  thane: '27',
  maharashtra: '27',
  // Delhi — 07
  delhi: '07',
  'new delhi': '07',
  // Karnataka — 29
  bengaluru: '29',
  bangalore: '29',
  mysuru: '29',
  mysore: '29',
  karnataka: '29',
  // Tamil Nadu — 33
  chennai: '33',
  coimbatore: '33',
  madurai: '33',
  'tamil nadu': '33',
  tamilnadu: '33',
  // West Bengal — 19
  kolkata: '19',
  'west bengal': '19',
  // Telangana — 36
  hyderabad: '36',
  telangana: '36',
}

export function cityToStateCode(city: string): string | null {
  const key = (city || '').trim().toLowerCase()
  return _CITY_TO_STATE_CODE[key] ?? null
}

/**
 * VT-448 PRIMARY identify — fetch the GSTIN(s) registered against the owner's PAN via the server-side
 * proxy route. The route holds the INTERNAL_API_SECRET and forwards to the orchestrator; the browser
 * only ever talks to /api/team. Fail-CLOSED to an empty list (the manual-GSTIN fallback always
 * exists — a lookup failure never blocks signup). The returned GSTIN(s) are IDENTIFIED, not verified:
 * the owner picks one and the existing confirm spine (status gstin_verified) is the sole verify gate.
 */
export async function fetchGstinsByPan(
  pan: string,
  stateCode: string,
  f: Fetch = fetch,
): Promise<GstinsByPanResult> {
  try {
    const res = await f('/api/team/onboard/gstins-by-pan', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ pan, state_code: stateCode }),
    })
    if (!res.ok) return { ok: false, gstins: [], reason: `http_${res.status}` }
    const data = (await res.json().catch(() => ({}))) as { gstins?: string[] }
    return { ok: true, gstins: data.gstins ?? [], reason: 'ok' }
  } catch {
    return { ok: false, gstins: [], reason: 'error' }
  }
}
