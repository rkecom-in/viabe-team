/**
 * VT-517 — ownership format gates (lib/ownership-verify.ts).
 *
 * VT-517 KILLED the self-serve ownership-OTP/DIN flow: ownership is no longer proven by an automated
 * channel-OTP — a Viabe human decides it (the VTR Ops Console ownership-review surface). The
 * browser→proxy fetch helpers (startOwnershipOtp / confirmOwnershipOtp / verifyOwnerViaDin) and their
 * dead proxy routes were removed with the orchestrator endpoints. Only the pure FORMAT gates remain:
 * they are self-contained predicates with no network dependency (kept so their unit tests stay green).
 */

/**
 * Client-side DIN FORMAT pre-check: a Director Identification Number is exactly 8 digits. A pure
 * format gate (no verification) — input is trimmed before the test.
 */
export function isValidDinFormat(din: string): boolean {
  return /^\d{8}$/.test((din || '').trim())
}

/**
 * Client-side public-phone FORMAT pre-check: a +91 mobile (+91 then 6-9 then 9 digits). A pure
 * format gate (no verification) — input is trimmed before the test.
 */
export function isValidPublicPhoneFormat(phone: string): boolean {
  return /^\+91[6-9]\d{9}$/.test((phone || '').trim())
}
